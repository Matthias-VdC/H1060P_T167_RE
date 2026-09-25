#!/usr/bin/env python3
"""Huion H1060P (board T167, firmware 190325) unlock tool.

One script for the whole workflow: identify the tablet, decrypt the
official firmware image, patch it for a higher report rate (~730 reports
per second instead of the stock ~231; ~470Hz and ~560Hz builds are
available too), and flash it.

Typical end-to-end run:

    python3 h1060p.py identify
    python3 h1060p.py decrypt H1060P_HUION_T167_190325.bin
    python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --raw
    sudo python3 h1060p.py flash dec_T167_190325_730hz-raw.bin

On Windows, run the same steps as "python h1060p.py ..." (no sudo).

The stock firmware can be flashed back the same way at any time (see
https://github.com/Matthias-VdC/H1060P_T167_RE for the download link).

The tablet's built-in input smoothing stays ON in every build, like the
original firmware; add --raw to the patch step for completely
unfiltered pen input. With the default 730hz build, --no-taps
additionally makes the pen never register a click (keyboard players).

This tool contains no Huion firmware bytes, only patch descriptions.
Requires Python 3, plus libusb on Linux; on Windows it talks to the
tablet through the built-in HID driver, so nothing else is needed. Use
at your own risk, on your own device.
"""

import argparse
import ctypes
import hashlib
import os
import sys
import time

# Windows reaches the tablet through its HID driver; elsewhere, libusb.
WINDOWS = sys.platform == "win32"

STOCK_MD5 = "c918ce0d59b472c6db0d967baedf774d"
STOCK_SIZE = 25304

# USB ids: the running tablet, and the bootloader it exposes while flashing.
TABLET_VENDOR_ID = 0x256C
TABLET_PRODUCT_ID = 0x006D
BOOTLOADER_VENDOR_ID = 0x0416
BOOTLOADER_PRODUCT_ID = 0x3F00

# USB string descriptor index 201 carries the model/firmware id.
MODEL_STRING_INDEX = 0xC9
LANGID_US_ENGLISH = 0x0409
GET_DESCRIPTOR = 0x06
STRING_DESCRIPTOR_TYPE = 0x03
DEVICE_TO_HOST = 0x80
TRANSFER_TIMEOUT_MS = 1000
LIBUSB_ERROR_PIPE = -9  # descriptor index does not exist on the device


# ===========================================================================
# USB plumbing: libusb (Linux)
# ===========================================================================
def load_libusb():
    """Return a ctypes handle to libusb with the prototypes we need."""
    lib = None
    last_error = None
    for name in ("libusb-1.0.so.0", "libusb-1.0.so", "libusb-1.0.0.dylib"):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as error:
            last_error = error
    if lib is None:
        raise RuntimeError(
            "libusb-1.0 not found — install it from your distribution's "
            f"packages ({last_error})")
    lib.libusb_init.argtypes = [ctypes.c_void_p]
    lib.libusb_init.restype = ctypes.c_int
    lib.libusb_open_device_with_vid_pid.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
    lib.libusb_control_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
        ctypes.c_uint16, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16,
        ctypes.c_uint]
    lib.libusb_control_transfer.restype = ctypes.c_int
    lib.libusb_kernel_driver_active.argtypes = [
        ctypes.c_void_p, ctypes.c_int]
    lib.libusb_detach_kernel_driver.argtypes = [
        ctypes.c_void_p, ctypes.c_int]
    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_interrupt_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_uint16, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
    lib.libusb_interrupt_transfer.restype = ctypes.c_int
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    return lib


def init_libusb():
    """load_libusb(), with libusb_init() done (exits if it fails)."""
    lib = load_libusb()
    if lib.libusb_init(None) != 0:
        sys.exit("libusb_init failed")
    return lib


def open_device(lib, vendor_id, product_id, detach_kernel_driver=False):
    """Open the first USB device with the given ids. Returns a handle
    or None. With detach_kernel_driver, claims interface 0 as well."""
    handle = lib.libusb_open_device_with_vid_pid(
        None, vendor_id, product_id)
    if not handle:
        return None
    handle = ctypes.c_void_p(handle)
    if detach_kernel_driver:
        try:
            # The kernel's HID driver owns the interface; unbind it so
            # we can claim it.
            if lib.libusb_kernel_driver_active(handle, 0) == 1:
                lib.libusb_detach_kernel_driver(handle, 0)
            if lib.libusb_claim_interface(handle, 0) != 0:
                lib.libusb_close(handle)
                return None
        except Exception:
            lib.libusb_close(handle)
            return None
    return handle


# ===========================================================================
# USB plumbing: Windows
# ===========================================================================
# Both USB identities stay on Windows' built-in HID driver, as with
# Huion's own updater: no libusb, no Zadig, no administrator rights.
# Everything here is ctypes on DLLs that ship with Windows.
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ_WRITE = 0x3
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
CR_SUCCESS = 0x00
CR_BUFFER_SMALL = 0x1A
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0x0
HIDP_STATUS_SUCCESS = 0x00110000


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_uint8 * 8)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                ("hEvent", ctypes.c_void_p)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_uint16), ("UsagePage", ctypes.c_uint16),
                ("InputReportByteLength", ctypes.c_uint16),
                ("OutputReportByteLength", ctypes.c_uint16),
                ("FeatureReportByteLength", ctypes.c_uint16),
                ("Reserved", ctypes.c_uint16 * 17),
                ("NumberCounts", ctypes.c_uint16 * 10)]  # unused here


def load_windows_hid():
    """Return (kernel32, hid, cfgmgr32) with the prototypes we need."""
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    handle, dword, ulong = wintypes.HANDLE, wintypes.DWORD, wintypes.ULONG
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, dword, dword, ctypes.c_void_p, dword, dword,
        handle]
    kernel32.CreateFileW.restype = handle
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = handle
    for function in (kernel32.ReadFile, kernel32.WriteFile):
        function.argtypes = [handle, ctypes.c_void_p, dword,
                             ctypes.POINTER(dword),
                             ctypes.POINTER(OVERLAPPED)]
        function.restype = wintypes.BOOL
    kernel32.GetOverlappedResult.argtypes = [
        handle, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(dword),
        wintypes.BOOL]
    kernel32.GetOverlappedResult.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [handle, dword]
    kernel32.WaitForSingleObject.restype = dword
    kernel32.CancelIoEx.argtypes = [handle, ctypes.POINTER(OVERLAPPED)]
    kernel32.CancelIoEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = wintypes.BOOL
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetHidGuid.restype = None
    hid.HidD_GetIndexedString.argtypes = [
        handle, ulong, ctypes.c_void_p, ulong]
    hid.HidD_GetIndexedString.restype = wintypes.BOOLEAN
    hid.HidD_GetPreparsedData.argtypes = [
        handle, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidD_FreePreparsedData.restype = wintypes.BOOLEAN
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    hid.HidP_GetCaps.restype = ctypes.c_long  # NTSTATUS
    cfgmgr32.CM_Get_Device_Interface_List_SizeW.argtypes = [
        ctypes.POINTER(ulong), ctypes.POINTER(GUID), wintypes.LPCWSTR, ulong]
    cfgmgr32.CM_Get_Device_Interface_List_SizeW.restype = dword
    cfgmgr32.CM_Get_Device_Interface_ListW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, ctypes.c_wchar_p, ulong,
        ulong]
    cfgmgr32.CM_Get_Device_Interface_ListW.restype = dword
    return kernel32, hid, cfgmgr32


def windows_hid_paths(hid, cfgmgr32, vendor_id, product_id):
    """Device paths of the present HID collections (one per interface
    or top-level collection) of the USB device with the given ids."""
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    while True:
        length = ctypes.c_ulong()
        if cfgmgr32.CM_Get_Device_Interface_List_SizeW(
                ctypes.byref(length), ctypes.byref(guid), None,
                CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != CR_SUCCESS:
            return []
        paths = ctypes.create_unicode_buffer(length.value)
        result = cfgmgr32.CM_Get_Device_Interface_ListW(
            ctypes.byref(guid), None, paths, length.value,
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
        if result != CR_BUFFER_SMALL:  # a device arrived meanwhile: retry
            break
    if result != CR_SUCCESS:
        return []
    ids = f"vid_{vendor_id:04x}&pid_{product_id:04x}"
    return [path for path in paths[:].split("\0") if ids in path.lower()]


def windows_open(kernel32, path, access, flags=0):
    """Open a HID collection by device path. Returns a handle or None."""
    handle = kernel32.CreateFileW(path, access, FILE_SHARE_READ_WRITE, None,
                                  OPEN_EXISTING, flags, None)
    return None if handle in (None, INVALID_HANDLE_VALUE) else handle


# ===========================================================================
# identify
# ===========================================================================
def read_string_descriptor(lib, device, index, max_length=255):
    """Read one USB string descriptor. Returns its raw bytes, or b'' if
    the device has no descriptor at that index."""
    buffer = (ctypes.c_ubyte * max_length)()
    transferred = lib.libusb_control_transfer(
        device, DEVICE_TO_HOST, GET_DESCRIPTOR,
        (STRING_DESCRIPTOR_TYPE << 8) | index, LANGID_US_ENGLISH,
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        max_length, TRANSFER_TIMEOUT_MS)
    if transferred == LIBUSB_ERROR_PIPE:
        return b""
    if transferred < 0:
        raise RuntimeError(
            f"reading string descriptor {index} failed "
            f"(libusb error {transferred})")
    return bytes(buffer[:transferred])


def read_model_libusb():
    """The tablet's model string via libusb: None if no tablet is
    present, "" if it has no model descriptor."""
    lib = init_libusb()
    device = open_device(lib, TABLET_VENDOR_ID, TABLET_PRODUCT_ID)
    if not device:
        return None
    try:
        raw = read_string_descriptor(lib, device, MODEL_STRING_INDEX)
    finally:
        lib.libusb_close(device)
    body = raw[2:]  # skip bLength / bDescriptorType
    if len(body) % 2:
        body = body[:-1]
    return body.decode("utf-16-le", errors="replace")


def read_model_windows():
    """The tablet's model string via the Windows HID driver: None if no
    tablet is present, "" if it has no model descriptor."""
    kernel32, hid, cfgmgr32 = load_windows_hid()
    paths = windows_hid_paths(hid, cfgmgr32, TABLET_VENDOR_ID,
                              TABLET_PRODUCT_ID)
    if not paths:
        return None
    for path in paths:
        # Access 0 is all a string request needs, and is granted even on
        # collections the system holds open (the pen, the keys).
        handle = windows_open(kernel32, path, 0)
        if handle is None:
            continue
        try:
            model = ctypes.create_unicode_buffer(64)  # the id is 17 chars
            if hid.HidD_GetIndexedString(handle, MODEL_STRING_INDEX, model,
                                         ctypes.sizeof(model)):
                return model.value
        finally:
            kernel32.CloseHandle(handle)
    return ""


def identify():
    """Print the tablet's model/firmware id (e.g. HUION_T167_190325)."""
    model = read_model_windows() if WINDOWS else read_model_libusb()
    if model is None:
        sys.exit(f"no tablet found at {TABLET_VENDOR_ID:04x}:"
                 f"{TABLET_PRODUCT_ID:04x} — is it plugged in?"
                 + ("" if WINDOWS else " (reading descriptors may need root)"))
    if not model:
        sys.exit("tablet found, but it reports no model descriptor")
    print(model)
    if "T167" not in model:
        print(f"\nWARNING: this is not a T167 board. These patches are "
              f"for H1060P/T167 only — do NOT flash a T167 image "
              f"to this tablet.", file=sys.stderr)


# ===========================================================================
# decrypt
# ===========================================================================
# Huion distributes firmware images with a byte-substitution cipher.
# DECRYPT_MAP maps each cipher byte back to its plaintext value.
CIPHER = [
    0x00, 0x08, 0x02, 0x05, 0x09, 0x03, 0x06, 0x04, 0x01, 0x07, 0x78, 0x79,
    0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F, 0x80, 0x81, 0x12, 0x10, 0x0C, 0x0D,
    0x0E, 0x0F, 0x0B, 0x11, 0x0A, 0x13, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5,
    0xF6, 0xF7, 0xF8, 0xF9, 0x17, 0x15, 0x1B, 0x14, 0x1C, 0x1D, 0x1A, 0x16,
    0x18, 0x19, 0xC4, 0xC0, 0xC1, 0xC2, 0xBE, 0xBF, 0xC3, 0xC5, 0xC6, 0xC7,
    0x64, 0x65, 0x69, 0x6A, 0x6C, 0x6D, 0x6B, 0x66, 0x67, 0x68, 0x1E, 0x1F,
    0x23, 0x24, 0x25, 0x26, 0x27, 0x20, 0x21, 0x22, 0xD2, 0xD3, 0xD4, 0xD9,
    0xDA, 0xDB, 0xD5, 0xD6, 0xD7, 0xD8, 0x96, 0x97, 0x98, 0x9E, 0x9F, 0x99,
    0x9A, 0x9B, 0x9C, 0x9D, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x28, 0x29, 0x2A,
    0x2B, 0x31, 0xDC, 0xDD, 0xDE, 0xE3, 0xE4, 0xE5, 0xDF, 0xE0, 0xE1, 0xE2,
    0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x3C, 0x3D,
    0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0xCD, 0xCE, 0xCF, 0xD0,
    0xD1, 0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF,
    0xB0, 0xB1, 0xB2, 0xB3, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x50,
    0x51, 0x52, 0x8C, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x92, 0x93, 0x94, 0x95,
    0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x60, 0x61, 0x62, 0x63, 0xEE, 0xE6,
    0xE7, 0xEC, 0xE8, 0xEA, 0xED, 0xEF, 0xEB, 0xE9, 0x70, 0x71, 0x76, 0x72,
    0x73, 0x74, 0x77, 0x6F, 0x6E, 0x75, 0x4B, 0x47, 0x48, 0x49, 0x4C, 0x4D,
    0x46, 0x4E, 0x4F, 0x4A, 0xB7, 0xB8, 0xB4, 0xB5, 0xB6, 0xB9, 0xBA, 0xBB,
    0xBC, 0xBD, 0x37, 0x38, 0x39, 0x32, 0x33, 0x34, 0x35, 0x36, 0x3A, 0x3B,
    0xA0, 0xA6, 0xA7, 0xA8, 0xA9, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xFE, 0xFA,
    0xFC, 0xFB, 0xFD, 0xFF,
]
DECRYPT_MAP = {cipher_byte: plain_byte
               for plain_byte, cipher_byte in enumerate(CIPHER)}


def decrypt(path):
    """Decrypt an official (CDN) firmware image in place of the flow:
    writes dec_<name>.bin next to it and reports its checksum."""
    data = open(path, "rb").read()
    if len(data) != STOCK_SIZE:
        sys.exit(f"ABORT: {path}: expected {STOCK_SIZE} bytes, "
                 f"got {len(data)}")
    plaintext = bytes(DECRYPT_MAP.get(b, 0) for b in data)
    out_path = "dec_" + os.path.basename(path)
    with open(out_path, "wb") as f:
        f.write(plaintext)
    md5 = hashlib.md5(plaintext).hexdigest()
    print(f"decrypted {path} -> {out_path}  (md5 {md5})")
    if md5 == STOCK_MD5:
        print("  this is the stock T167 190325 image the patcher expects")
    else:
        print(f"  WARNING: md5 does not match the stock T167 190325 image "
              f"({STOCK_MD5}); the patcher will refuse it. Is this a "
              f"different firmware version?")
    return out_path


# ===========================================================================
# patch
# ===========================================================================
# Patch entries: (file_offset, expected_stock_hex, replacement_hex, description)
#
# The file is mapped at 0x08000000 in flash; file offset = flash address
# minus 0x08000000. "Ticks" are timer ticks of 22 CPU clocks each
# (~0.46 microseconds at 48 MHz).
PATCHES = {
    # --- sensor settle delays ---------------------------------------------
    # The firmware waits for its analog circuitry to settle around every
    # sensor reading. The stock values are generous; replacements are the
    # shortest values that passed extended live testing.
    "settle_adc_wake_4": (
        0x21FA, "0f20", "0420",
        "ADC wake-up settle: 15 -> 4 ticks"),
    "settle_adc_wake_6": (
        0x21FA, "0f20", "0620",
        "ADC wake-up settle: 15 -> 6 ticks"),
    "settle_mux_16": (
        0x220C, "4620", "1020",
        "input mux settle: 70 -> 16 ticks"),
    "settle_mux_32": (
        0x220C, "4620", "2020",
        "input mux settle: 70 -> 32 ticks"),
    "settle_ringdown_25": (
        0x22A0, "9620", "1920",
        "resonance ring-down between frequency scans: 150 -> 25 ticks"),
    "settle_ringdown_40": (
        0x22A0, "9620", "2820",
        "resonance ring-down between frequency scans: 150 -> 40 ticks"),

    # --- input smoothing filters (--raw only) ------------------------------
    # Three on-board filters average pen positions over time. Replacing
    # each filter's first instructions with an immediate return turns it
    # into a pass-through, so positions reach the PC unmodified.
    "bypass_ema_filter": (
        0x1B34, "10b50346", "7047",
        "exponential moving average filter -> pass-through"),
    "bypass_short_moving_average": (
        0x54F4, "ffb583b0", "08467047",
        "3-sample coordinate average -> pass-through"),
    "bypass_long_moving_average": (
        0x3BE0, "ffb583b0", "08467047",
        "15-sample coordinate average -> pass-through"),

    # --- tilt calculation -----------------------------------------------------
    # Four extra sensor readings per cycle feed a tilt value that occupies
    # two HID report bytes no driver on this tablet consumes.
    "remove_tilt_x": (
        0x3FA0, "70b50546", "70470546",
        "X tilt readings -> skip (4 sensor readings per cycle saved)"),
    "remove_tilt_y": (
        0x3EA8, "70b50546", "70470546",
        "Y tilt readings -> skip (4 sensor readings per cycle saved)"),

    # --- redundant re-measurements ---------------------------------------------
    # While building each report, the firmware re-runs part of the pen
    # position scan although a fresh result from the same cycle already
    # exists. Replacing the call with two no-ops makes the report use the
    # already-measured values.
    "remove_remeasure_normal_pressure": (
        0x322E, "00f051ff", "00bf00bf",
        "report-time re-scan (normal pressure range) -> skip"),
    "remove_remeasure_light_touch": (
        0x32A6, "00f015ff", "00bf00bf",
        "report-time re-scan (light touch range) -> skip"),

    # --- position window scan ---------------------------------------------------
    # To locate the pen along each axis the firmware reads 6 adjacent
    # coils per cycle; the outermost one is almost never needed.
    "x_window_read_five_coils": (
        0x4292, "0024", "0124",
        "X axis: start the scan one coil later (6 -> 5 coils)"),
    "x_window_clear_unused_slot": (
        0x4294, "002500bf37e0", "1f4a00201060",
        "X axis: store 0 into the skipped slot"),
    "y_window_read_five_coils": (
        0x4216, "0024", "0124",
        "Y axis: start the scan one coil later (6 -> 5 coils)"),
    "y_window_clear_unused_slot": (
        0x4218, "002500bf31e0", "1c4a00201060",
        "Y axis: store 0 into the skipped slot"),
    # Assert-only patches: abort safely if a nearby constant moved.
    "x_window_data_location_check": (
        0x4314, "78060020", "78060020",
        "X axis scan buffer location check (assert only)"),
    "y_window_data_location_check": (
        0x428C, "98060020", "98060020",
        "Y axis scan buffer location check (assert only)"),

    # --- math runtime --------------------------------------------------------------
    # The pressure math used slow generic 64-bit multiply/divide loops.
    # These hand-written replacements were proven bit-exact against the
    # originals over thousands of random and boundary test vectors in a
    # Thumb-1 simulator before ever being flashed.
    "fast_64bit_multiply": (
        0x01C0,
        "f0b51fb486b000200090019002900698",
        "f0b504465c430d4655432c4495b21646"
        "360c0746bfb2000c0146694370432044"
        "7d437e43324633461b0c12040e463604"
        "0f463f0c32447b412a4400277b410344"
        "10461946f0bd",
        "64-bit multiply: call-per-step loop -> carry chain (70 bytes)"),
    "fast_64bit_divide": (
        0x0764,
        "f0b51fb40646002082b0054640240191",
        "f0b517461f4315d04026002400254000"
        "494164416d4104d29d4206d301d19442"
        "03d3a41a9d4101273843013eefd12246"
        "2b46f0bd021c0b1c0020013800210139"
        "f0bd",
        "64-bit divide: 64-iteration loop -> shift-subtract (66 bytes)"),
    "fast_32bit_divide": (
        0x0120,
        "30b50b46014600202022012409e00d46",
        "00b5002903d101460020013800bd0022"
        "20234000524101d28a4201d3521a0130"
        "013bf6d1114600bd",
        "32-bit divide: variable-shift loop -> shift-subtract (40 bytes)"),
    "fast_64bit_halve": (
        0x3E2A,
        "06460f460222002330463946fcf79df9",
        "ca0f0023801859414008cb0718434910",
        "signed 64-bit halving: full divide routine -> 8-instruction "
        "shift (16 bytes)"),

    # --- USB report polling ---------------------------------------------------
    # The pen report endpoint tells the host to collect a report only
    # every 2 ms (bInterval = 2 in its endpoint descriptor), delaying
    # every report by 0-2 ms and capping delivery at 500 reports/s.
    # The firmware's report sender never waits: if the previous report
    # is still uncollected, the new one is silently dropped — so a scan
    # producing faster than 500 reports/s makes the PC periodically
    # receive a stale report (the cause of the stutter seen on the
    # fastest build). bInterval 1 — the full-speed norm — makes the
    # host collect every 1 ms frame instead. Scan timing is untouched.
    "usb_poll_every_1ms": (
        0x59A3, "02", "01",
        "pen endpoint (EP 0x81) host polling: every 2 ms -> every 1 ms"),
    "usb_poll_every_1ms_aux": (
        0x59BC, "02", "01",
        "default-mode endpoint (EP 0x82) host polling: 2 ms -> 1 ms"),

    # --- pressure tracker period (730hz build) ---------------------------
    # Besides the position scan, every cycle re-runs a 5-reading scan
    # that tracks the pen's resonance band — this is what produces
    # pressure and detects pen contact. The stock firmware already
    # counts cycles in a flag that wraps every 2 cycles; raising the
    # wrap constant makes the band tracker run every Nth cycle instead.
    # Position still updates every cycle (position is measured by a
    # separate coil scan); pressure/contact refresh at rate/N and pen
    # clicks gain up to N-1 cycles (~20 ms at 730Hz, period 15) of
    # extra latency — aimed at players who click with a keyboard.
    # The band state also selects the excitation used by the position
    # scan, so the period must stay short enough to follow hover-height
    # changes; 15 was verified usable, 100 degraded menu interaction.
    "tracker_period_15": (
        0x1F10, "0128", "0e28",
        "band tracker wrap: every 2nd cycle -> every 15th cycle"),
    # The scan step's call to the band tracker is redirected through a
    # short flag check placed in a dead code area (the tilt calculation
    # this tool removes): run the tracker only when the cycle counter
    # wrapped to 0, otherwise return immediately.
    "tracker_call_redirect": (
        0x1F1E, "02f0d9f8", "01f0c7ff",
        "band tracker call -> routed through the skip check below"),
    "tracker_skip_check": (
        0x3EB0, "384800683849886036484068", "034b1b78002b00d070470be1",
        "skip check: run the band tracker only when the counter is 0"),
    "tracker_skip_check_flag_address": (
        0x3EC0, "80680861", "e4020020",
        "skip check: point at the stock cycle counter"),

    # --- pen contact disabled (--no-taps, 730hz build) --------------------
    # The report builder clamps pressure to a maximum stored in flash at
    # 0x34BC (0x1FFF), on both pressure paths (normal and light touch).
    # Zeroing that constant makes pressure report 0 unconditionally, so
    # pen contact never registers as a click — useful to make accidental
    # touches harmless. Firmware-internal pressure tracking still runs
    # (the band state must stay fresh for the position scan); this is
    # an output-only change with no effect on rate or position.
    "pen_contact_disabled": (
        0x34BC, "ff1f0000", "00000000",
        "pressure clamp maximum: 0x1FFF -> 0 (pen never reports a click)"),
}

# Applied only with --raw (hardware smoothing OFF).
RAW_PATCHES = [
    "bypass_ema_filter",
    "bypass_short_moving_average",
    "bypass_long_moving_average",
]

# Builds are named by their report rate. 730hz is the default (the
# fastest build on the proven-safe sensor waits, with the pressure
# tracker slowed to every 15th cycle — for keyboard-clicking players);
# 470hz is the fallback with no pen-input trade-offs; 500hz is the
# experimental shortest-waits build; 420hz and 380hz step back toward
# stock behavior, for troubleshooting. "md5" is the reference checksum
# with smoothing ON (default); "md5_raw" with --raw; the "notap"
# checksums additionally disable pen contact (--no-taps).
BUILDS = {
    "730hz": {
        "rate": "~730Hz",
        "desc": "DEFAULT. The fastest build on the proven-safe sensor "
                "waits; the pressure tracker runs every 15th cycle, so "
                "pen clicks gain up to ~20 ms latency and pressure "
                "updates ~49 times/s — for players who click with a "
                "keyboard. Pen-clickers should use 470hz instead.",
        "md5": "2aa4b30c435d9b2d7c7aaec183b7f234",
        "md5_raw": "3f5827e0d43d50ebb19dbc5c2f360526",
        "md5_notap": "78d44c82e00c38da2bcfb8ea717650ea",
        "md5_notap_raw": "bb5a10e2d09af4ee15b812bacf21c59d",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "remove_remeasure_light_touch",
            "x_window_read_five_coils", "x_window_clear_unused_slot",
            "y_window_read_five_coils", "y_window_clear_unused_slot",
            "x_window_data_location_check", "y_window_data_location_check",
            "fast_64bit_multiply", "fast_64bit_divide",
            "fast_32bit_divide", "fast_64bit_halve",
            "usb_poll_every_1ms", "usb_poll_every_1ms_aux",
            "tracker_period_15", "tracker_call_redirect",
            "tracker_skip_check", "tracker_skip_check_flag_address",
        ],
    },
    "470hz": {
        "rate": "~470Hz",
        "desc": "The fallback with no pen-input trade-offs: pressure "
                "tracking every cycle, immediate tap detection — pick "
                "this if you click with the pen. Long clean live "
                "history, and no rate dip while tapping.",
        "md5": "44490905fc8bc3f8266c49e5afa80c29",
        "md5_raw": "a1cc3b72402d490cc755de4f7571b39a",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "remove_remeasure_light_touch",
            "x_window_read_five_coils", "x_window_clear_unused_slot",
            "y_window_read_five_coils", "y_window_clear_unused_slot",
            "x_window_data_location_check", "y_window_data_location_check",
            "fast_64bit_multiply", "fast_64bit_divide",
            "fast_32bit_divide", "fast_64bit_halve",
            "usb_poll_every_1ms", "usb_poll_every_1ms_aux",
        ],
    },
    "500hz": {
        "rate": "~560Hz",
        "desc": "EXPERIMENTAL. Every improvement including the shortest "
                "sensor waits — the fastest build. Its earlier stutter "
                "traced to the USB 2 ms polling cap (reports produced "
                "faster than the host collected them), now fixed in all "
                "builds; it stays experimental until re-tested. If it "
                "misbehaves, flash 470hz.",
        "md5": "070f28519df8c8c7d9abda9767069286",
        "md5_raw": "19ae41187e29a28927997228c5ac3ab7",
        "patches": [
            "settle_adc_wake_4", "settle_mux_16", "settle_ringdown_25",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "remove_remeasure_light_touch",
            "x_window_read_five_coils", "x_window_clear_unused_slot",
            "y_window_read_five_coils", "y_window_clear_unused_slot",
            "x_window_data_location_check", "y_window_data_location_check",
            "fast_64bit_multiply", "fast_64bit_divide",
            "fast_32bit_divide", "fast_64bit_halve",
            "usb_poll_every_1ms", "usb_poll_every_1ms_aux",
        ],
    },
    "420hz": {
        "rate": "~420Hz",
        "desc": "Additionally keeps the original (slow) math routines.",
        "md5": "85963a862fcfaa3e325204dd34036f5a",
        "md5_raw": "b3dd7f3da834f4273e954de898e1cf0c",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "x_window_read_five_coils", "x_window_clear_unused_slot",
            "y_window_read_five_coils", "y_window_clear_unused_slot",
            "x_window_data_location_check", "y_window_data_location_check",
            "usb_poll_every_1ms", "usb_poll_every_1ms_aux",
        ],
    },
    "380hz": {
        "rate": "~380Hz",
        "desc": "Additionally keeps the original 6-coil window scan — "
                "the smallest step away from stock behavior.",
        "md5": "2f17be81cc2d9812b3ab45182bbdd1c9",
        "md5_raw": "1024ce2c4266cbbd4bba112511db4ca5",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "usb_poll_every_1ms", "usb_poll_every_1ms_aux",
        ],
    },
}


def load_stock(path):
    """Read the stock image and verify it is exactly the right one."""
    stock = open(path, "rb").read()
    if len(stock) != STOCK_SIZE:
        sys.exit(f"ABORT: {path}: expected {STOCK_SIZE} bytes, "
                 f"got {len(stock)}")
    md5 = hashlib.md5(stock).hexdigest()
    if md5 != STOCK_MD5:
        sys.exit(f"ABORT: {path}: md5 {md5} is not the stock T167 190325 "
                 f"image (expected {STOCK_MD5}). Nothing done.")
    return stock


def apply_patches(stock, patch_names):
    """Apply the named patches; abort (before writing anything) if any
    location does not contain the expected stock bytes."""
    image = bytearray(stock)
    for name in patch_names:
        offset, expected_hex, replacement_hex, _ = PATCHES[name]
        expected = bytes.fromhex(expected_hex)
        replacement = bytes.fromhex(replacement_hex)
        found = bytes(image[offset: offset + len(expected)])
        if found != expected:
            sys.exit(
                f"ABORT: patch '{name}' at file offset 0x{offset:04X}: "
                f"expected stock bytes {expected.hex()} but found "
                f"{found.hex()}.\n"
                f"This is not the stock T167 190325 image this tool is "
                f"written for. Nothing written.")
        image[offset: offset + len(replacement)] = replacement
    return bytes(image)


def print_build_list():
    print("Available builds (default: 730hz). "
          "Hardware input smoothing\nis ON by default in every build; add "
          "--raw to disable it (unfiltered pen input).\n"
          "With 730hz only, --no-taps additionally disables pen contact "
          "(never\nregister a click):\n")
    for name in ("730hz", "470hz", "500hz", "420hz", "380hz"):
        build = BUILDS[name]
        print(f"  {name:18} {build['rate']:8} {build['desc']}")


def patch(path, variant, raw, no_taps, output):
    build = BUILDS[variant]
    if no_taps and "md5_notap" not in build:
        sys.exit("ABORT: --no-taps is only available with the 730hz build.")
    stock = load_stock(path)

    patches = list(build["patches"])
    if raw:
        patches += RAW_PATCHES
    if no_taps:
        patches.append("pen_contact_disabled")
    image = apply_patches(stock, patches)

    if no_taps:
        key = "md5_notap_raw" if raw else "md5_notap"
    else:
        key = "md5_raw" if raw else "md5"
    expected_md5 = build[key]
    image_md5 = hashlib.md5(image).hexdigest()
    if image_md5 != expected_md5:
        sys.exit(f"ABORT: built image md5 {image_md5} does not match the "
                 f"reference build ({expected_md5}). Nothing written — "
                 f"report this bug.")

    suffix = "-raw" if raw else ""
    suffix += "-notap" if no_taps else ""
    output = output or f"dec_T167_190325_{variant}{suffix}.bin"
    with open(output, "wb") as f:
        f.write(image)

    print(f"built {variant} "
          f"(hardware smoothing {'OFF' if raw else 'ON'}): {output}")
    print(f"  {len(patches)} patches applied, output md5 {image_md5} "
          f"(matches the reference build)")
    run = "python" if WINDOWS else "sudo python3"
    print(f"  flash with: {run} {os.path.basename(sys.argv[0])} "
          f"flash {output}")


# ===========================================================================
# flash
# ===========================================================================
# Speaks the NuMicro LDROM HID protocol over USB: replays the same
# command sequence the official Huion update tool sends, then streams
# the image. Every packet is ACK-checked; the final ACK carries a byte
# sum of the image, verified after the last write.
ENDPOINT_OUT, ENDPOINT_IN = 0x02, 0x81
INTERRUPT_TIMEOUT_MS = 2000

# Captured handshake (2026-09-23, successful session): exact bytes.
HANDSHAKE = [
    "ae000000" "12000000" + "00" * 56,
    "a4000000" "01000000" "01000000" + "00" * 52,
    "a6000000" "03000000" + "00" * 56,
    "b1000000" "05000000" + "00" * 56,
    "a2000000" "07000000" + "00" * 56,
    "a1000000" "09000000" "7dff1ff800f00100" + "00" * 48,
    # resync (the official tool restarts its packet counter here)
    "a4000000" "01000000" "01000000" + "00" * 52,
]
HANDSHAKE_ACK_PACKETS = [2, 2, 4, 6, 8, 10, 2]
APROM_ACK_PACKET = 4


def build_packets(image):
    """Return the list of 64-byte OUT packets for an image (mirrors the
    captured official update session)."""
    packets = [bytes.fromhex(h) for h in HANDSHAKE]
    # CMD_UPDATE_APROM: [8:12]=0, [12:16]=size, [16:64]=image[0:48]
    p = bytearray(64)
    p[0:4] = (0xA0).to_bytes(4, "little")
    p[4:8] = (3).to_bytes(4, "little")
    p[8:12] = (0).to_bytes(4, "little")
    p[12:16] = len(image).to_bytes(4, "little")
    p[16:64] = image[0:48]
    packets.append(bytes(p))
    n = (len(image) - 48 + 55) // 56
    for i in range(n):
        p = bytearray(64)
        p[0:4] = (0).to_bytes(4, "little")
        p[4:8] = (5 + 2 * i).to_bytes(4, "little")
        chunk = image[48 + 56 * i: 48 + 56 * (i + 1)]
        p[8:8 + len(chunk)] = chunk
        packets.append(bytes(p))
    return packets


def expected_ack(packet_out):
    """ACK packet number the bootloader returns for a given OUT packet."""
    for handshake, ack in zip(HANDSHAKE, HANDSHAKE_ACK_PACKETS):
        if packet_out == int.from_bytes(bytes.fromhex(handshake)[4:8],
                                        "little"):
            return ack
    return packet_out + 1


class BootloaderDevice:
    """The tablet's LDROM bootloader (0416:3f00). Each platform's
    subclass below supplies open/send/receive/close."""

    def wait_and_open(self, wait_seconds):
        """Wait for the bootloader to appear (it shows for ~2.5s after
        replugging the tablet), then claim it."""
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self.open():
                return True
            time.sleep(0.01)
        return False


class LibusbBootloader(BootloaderDevice):
    """Linux: libusb, on the interrupt endpoints directly."""

    def __init__(self):
        self.lib = init_libusb()
        self.handle = None

    def open(self):
        self.handle = open_device(self.lib, BOOTLOADER_VENDOR_ID,
                                  BOOTLOADER_PRODUCT_ID,
                                  detach_kernel_driver=True)
        return bool(self.handle)

    def send(self, data):
        sent = ctypes.c_int()
        result = self.lib.libusb_interrupt_transfer(
            self.handle, ENDPOINT_OUT,
            (ctypes.c_ubyte * len(data)).from_buffer_copy(data),
            len(data), ctypes.byref(sent), INTERRUPT_TIMEOUT_MS)
        if result != 0 or sent.value != len(data):
            raise RuntimeError(
                f"OUT transfer failed: {result} sent={sent.value}")

    def receive(self):
        buffer = (ctypes.c_ubyte * 64)()
        received = ctypes.c_int()
        result = self.lib.libusb_interrupt_transfer(
            self.handle, ENDPOINT_IN, buffer, 64,
            ctypes.byref(received), INTERRUPT_TIMEOUT_MS)
        if result != 0 or received.value != 64:
            raise RuntimeError(
                f"IN transfer failed: {result} got={received.value}")
        return bytes(buffer)

    def close(self):
        if self.handle:
            self.lib.libusb_close(self.handle)
            self.handle = None


class WindowsHidBootloader(BootloaderDevice):
    """Windows: the HID driver. Each 64-byte packet is one HID report,
    framed with report id 0 (the bootloader declares none); Windows
    carries it over the same interrupt endpoints."""

    REPORT_LENGTH = 65  # report id + 64-byte packet

    def __init__(self):
        self.kernel32, self.hid, self.cfgmgr32 = load_windows_hid()
        self.handle = None
        # Manual-reset, as Microsoft recommends for OVERLAPPED events:
        # ReadFile/WriteFile reset it as each transfer starts, and it
        # stays set once the transfer completes, so the closing
        # GetOverlappedResult(bWait=TRUE) in transfer() cannot hang.
        self.event = self.kernel32.CreateEventW(None, True, False, None)
        if not self.event:
            sys.exit(f"CreateEventW failed (Windows error "
                     f"{ctypes.get_last_error()})")

    def open(self):
        for path in windows_hid_paths(self.hid, self.cfgmgr32,
                                      BOOTLOADER_VENDOR_ID,
                                      BOOTLOADER_PRODUCT_ID):
            handle = windows_open(self.kernel32, path,
                                  GENERIC_READ | GENERIC_WRITE,
                                  FILE_FLAG_OVERLAPPED)
            if handle is None:
                continue  # still starting up; the next poll retries
            lengths = self.report_lengths(handle)
            if lengths == (self.REPORT_LENGTH, self.REPORT_LENGTH):
                self.handle = handle
                return True
            self.kernel32.CloseHandle(handle)
            if lengths:
                sys.exit(f"refusing: the device at 0416:3f00 has "
                         f"{lengths[0]}/{lengths[1]}-byte HID reports "
                         f"(in/out), expected {self.REPORT_LENGTH}/"
                         f"{self.REPORT_LENGTH}. Nothing sent.")
        return False

    def report_lengths(self, handle):
        """(input, output) HID report lengths including the report id
        byte, or None if they cannot be read."""
        preparsed = ctypes.c_void_p()
        if not self.hid.HidD_GetPreparsedData(handle,
                                              ctypes.byref(preparsed)):
            return None
        try:
            caps = HIDP_CAPS()
            if self.hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) \
                    != HIDP_STATUS_SUCCESS:
                return None
            return caps.InputReportByteLength, caps.OutputReportByteLength
        finally:
            self.hid.HidD_FreePreparsedData(preparsed)

    def send(self, data):
        report = (ctypes.c_ubyte * self.REPORT_LENGTH).from_buffer_copy(
            bytes(1) + data)
        sent = self.transfer(self.kernel32.WriteFile, report, "OUT")
        if sent != self.REPORT_LENGTH:
            raise RuntimeError(f"OUT transfer failed: sent={sent}")

    def receive(self):
        report = (ctypes.c_ubyte * self.REPORT_LENGTH)()
        received = self.transfer(self.kernel32.ReadFile, report, "IN")
        if received != self.REPORT_LENGTH:
            raise RuntimeError(f"IN transfer failed: got={received}")
        return bytes(report[1:])

    def transfer(self, function, report, direction):
        """One overlapped ReadFile/WriteFile of a whole report, with the
        same timeout as the libusb transfers. Returns the byte count."""
        overlapped = OVERLAPPED(hEvent=self.event)
        timed_out = False
        if not function(self.handle, report, len(report), None,
                        ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error != ERROR_IO_PENDING:
                raise RuntimeError(f"{direction} transfer failed: "
                                   f"Windows error {error}")
            if self.kernel32.WaitForSingleObject(
                    self.event, INTERRUPT_TIMEOUT_MS) != WAIT_OBJECT_0:
                timed_out = True
                self.kernel32.CancelIoEx(self.handle, ctypes.byref(overlapped))
        # Always collect the result, cancelled or not: the driver must be
        # done with report and overlapped before they are freed, and a
        # transfer that completed just as the timeout hit still counts.
        count = ctypes.c_ulong()
        if not self.kernel32.GetOverlappedResult(
                self.handle, ctypes.byref(overlapped), ctypes.byref(count),
                True):
            if timed_out:
                raise RuntimeError(f"{direction} transfer timed out")
            raise RuntimeError(f"{direction} transfer failed: "
                               f"Windows error {ctypes.get_last_error()}")
        return count.value

    def close(self):
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.event:
            self.kernel32.CloseHandle(self.event)
            self.event = None


def flash(path):
    image = open(path, "rb").read()
    if len(image) != STOCK_SIZE:
        sys.exit(f"refusing: image is {len(image)} bytes, expected "
                 f"{STOCK_SIZE} (untested size)")
    md5 = hashlib.md5(image).hexdigest()
    print(f"image: {path}  size={len(image)}  md5={md5}")
    if md5 == STOCK_MD5:
        print("  (= known stock 190325 - zero risk)")
    packets = build_packets(image)
    print(f"{len(packets)} packets to send "
          f"({len(packets) - len(HANDSHAKE) - 1} data)")

    device = WindowsHidBootloader() if WINDOWS else LibusbBootloader()
    print("waiting for LDROM 0416:3f00 ... UNPLUG AND REPLUG THE TABLET NOW")
    if WINDOWS:
        print("  (if the tablet just starts up normally, replug it again: "
              "the first time,\n  Windows may still be installing the "
              "bootloader's driver)")
    if not device.wait_and_open(60):
        sys.exit("no bootloader appeared within 60s - aborting, "
                 "nothing flashed")
    print("got it - flashing")
    started = time.time()
    try:
        for i, packet in enumerate(packets):
            device.send(packet)
            ack = device.receive()
            packet_out = int.from_bytes(packet[4:8], "little")
            packet_ack = int.from_bytes(ack[4:8], "little")
            if packet_ack != expected_ack(packet_out):
                raise RuntimeError(
                    f"ACK desync at packet {i}: sent pkt {packet_out}, "
                    f"ACK pkt {packet_ack} (expected "
                    f"{expected_ack(packet_out)}) ack={ack.hex()}")
            if i == len(packets) - 1:
                # Final ACK: [8:10] = 16-bit byte-sum of the image (LE),
                # [10:12] = 1ff8 constant. Verified across five live
                # flashing sessions.
                checksum = sum(image) & 0xFFFF
                expected = bytes([checksum & 0xFF, checksum >> 8,
                                  0x1F, 0xF8])
                if ack[8:12] != expected:
                    print(f"WARNING: final ACK signature "
                          f"{ack[8:12].hex()} != expected {expected.hex()}")
                    print(f"full final ACK: {ack.hex()}")
                    return 2
            if i and i % 50 == 0:
                print(f"  {i}/{len(packets)}")
    except RuntimeError as error:
        print(f"ABORTED mid-session: {error}")
        print("APROM may be partially written; re-run with the stock image.")
        return 1
    finally:
        device.close()
    print(f"DONE in {time.time() - started:.1f}s - success ACK confirmed")
    print("UNPLUG AND REPLUG THE TABLET once more to boot the new firmware")
    return 0


# ===========================================================================
# command line
# ===========================================================================
def main():
    if WINDOWS and hasattr(sys.stdout, "reconfigure"):
        # Redirected output uses the ANSI code page, which may lack
        # characters like "—" (e.g. cp932): print "?" rather than crash.
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Typical order: identify -> decrypt -> patch -> flash. "
               "Rollback: re-flash the stock image with 'flash'.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("identify",
                   help="read the tablet's model id (e.g. HUION_T167_190325)")

    p_decrypt = sub.add_parser(
        "decrypt", help="decrypt an official firmware image "
                        "(writes dec_<name>.bin)")
    p_decrypt.add_argument("image", help="the downloaded .bin")

    p_patch = sub.add_parser(
        "patch", help="patch a decrypted stock image "
                      "(smoothing ON by default)")
    p_patch.add_argument("image", nargs="?",
                         help="the decrypted stock image")
    p_patch.add_argument("-o", "--output",
                         help="output path (default: "
                              "dec_T167_190325_<variant>[-raw].bin)")
    p_patch.add_argument("--variant", default="730hz",
                         choices=sorted(BUILDS),
                         help="which build to make (default: 730hz, the "
                              "fastest for keyboard-clicking players; "
                              "'470hz' keeps full pen-pressure tracking; "
                              "'500hz' is the experimental shortest-waits "
                              "build)")
    p_patch.add_argument("--raw", action="store_true",
                         help="disable the hardware input smoothing for "
                              "unfiltered pen input")
    p_patch.add_argument("--no-taps", action="store_true",
                         help="additionally disable pen contact: pressure "
                              "always reports 0, so the pen never "
                              "registers a click (730hz build only)")
    p_patch.add_argument("--list", action="store_true",
                         help="list the available builds")

    p_flash = sub.add_parser("flash",
                             help="flash an image (needs root on Linux; "
                                  "replug the tablet when told)")
    p_flash.add_argument("image", help="image file to flash")

    args = parser.parse_args()

    if args.command == "identify":
        identify()
    elif args.command == "decrypt":
        decrypt(args.image)
    elif args.command == "patch":
        if args.list:
            print_build_list()
        elif not args.image:
            p_patch.error("provide the stock image path (or --list)")
        else:
            patch(args.image, args.variant, args.raw, args.no_taps,
                  args.output)
    elif args.command == "flash":
        sys.exit(flash(args.image))


if __name__ == "__main__":
    main()
