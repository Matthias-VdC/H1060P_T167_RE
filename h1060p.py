#!/usr/bin/env python3
"""Huion H1060P (board T167, firmware 190325) unlock tool.

One script for the whole workflow: identify the tablet, decrypt the
official firmware image, patch it for a higher report rate (~500 reports
per second instead of the stock ~231), and flash it.

Typical end-to-end run:

    python3 h1060p.py identify
    python3 h1060p.py decrypt H1060P_HUION_T167_190325.bin
    python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --raw
    sudo python3 h1060p.py flash dec_T167_190325_release-raw.bin

The stock firmware can be flashed back the same way at any time (see
https://github.com/Matthias-VdC/H1060P_T167_RE for the download link).

The tablet's built-in input smoothing stays ON in every build, like the
original firmware; add --raw to the patch step for completely
unfiltered pen input.

This tool contains no Huion firmware bytes, only patch descriptions.
Requires Python 3 and libusb. Use at your own risk, on your own device.
"""

import argparse
import ctypes
import hashlib
import os
import sys
import time

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
# USB plumbing (shared)
# ===========================================================================
def load_libusb():
    """Return a ctypes handle to libusb with the prototypes we need."""
    lib = ctypes.CDLL("libusb-1.0.so.0")
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


def identify():
    """Print the tablet's model/firmware id (e.g. HUION_T167_190325)."""
    lib = load_libusb()
    if lib.libusb_init(None) != 0:
        sys.exit("libusb_init failed")
    device = open_device(lib, TABLET_VENDOR_ID, TABLET_PRODUCT_ID)
    if not device:
        sys.exit(f"no tablet found at {TABLET_VENDOR_ID:04x}:"
                 f"{TABLET_PRODUCT_ID:04x} — is it plugged in? "
                 f"(reading descriptors may need root)")
    try:
        raw = read_string_descriptor(lib, device, MODEL_STRING_INDEX)
        if len(raw) < 4:
            sys.exit("tablet found, but it reports no model descriptor")
        body = raw[2:]  # skip bLength / bDescriptorType
        if len(body) % 2:
            body = body[:-1]
        model = body.decode("utf-16-le", errors="replace")
        print(model)
        if "T167" not in model:
            print(f"\nWARNING: this is not a T167 board. These patches are "
                  f"for H1060P/T167 only — do NOT flash a T167 image "
                  f"to this tablet.", file=sys.stderr)
    finally:
        lib.libusb_close(device)


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
}

# Applied only with --raw (hardware smoothing OFF).
RAW_PATCHES = [
    "bypass_ema_filter",
    "bypass_short_moving_average",
    "bypass_long_moving_average",
]

# The recommended build includes every improvement; each conservative
# build steps one back toward stock behavior, for troubleshooting.
# "md5" is the reference checksum with smoothing ON (default);
# "md5_raw" is the reference checksum with --raw.
BUILDS = {
    "release": {
        "rate": "~500Hz",
        "desc": "RECOMMENDED. Every improvement: tilt calculation removed, "
                "redundant re-measures removed, 5-coil window scan, "
                "rewritten math, shortest proven-safe sensor waits.",
        "md5": "07e3834605f7f57a5f593f4b789f8c92",
        "md5_raw": "229be243cf5fbf069c8c17ff57102e91",
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
        ],
    },
    "conservative-470": {
        "rate": "~470Hz",
        "desc": "The same feature set but with more conservative sensor "
                "wait times. Try this if 'release' ever misbehaves.",
        "md5": "6176bde35f533c146915f31cb5381362",
        "md5_raw": "8b2231dff89596200adccba8254d1d3b",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "x_window_read_five_coils", "x_window_clear_unused_slot",
            "y_window_read_five_coils", "y_window_clear_unused_slot",
            "x_window_data_location_check", "y_window_data_location_check",
            "fast_64bit_multiply", "fast_64bit_divide",
            "fast_32bit_divide", "fast_64bit_halve",
        ],
    },
    "conservative-420": {
        "rate": "~420Hz",
        "desc": "Additionally keeps the original (slow) math routines.",
        "md5": "7bbb51e0e25895a9ca973526c5ae6d3d",
        "md5_raw": "7f6e4c104dbcfbe3a32c03dd92a3247c",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
            "x_window_read_five_coils", "x_window_clear_unused_slot",
            "y_window_read_five_coils", "y_window_clear_unused_slot",
            "x_window_data_location_check", "y_window_data_location_check",
        ],
    },
    "conservative-380": {
        "rate": "~380Hz",
        "desc": "Additionally keeps the original 6-coil window scan — "
                "the smallest step away from stock behavior.",
        "md5": "73225573e8b4cc10696d97af23820e16",
        "md5_raw": "97900462e38efb75b20cdcd6a66ae7de",
        "patches": [
            "settle_adc_wake_6", "settle_mux_32", "settle_ringdown_40",
            "remove_tilt_x", "remove_tilt_y",
            "remove_remeasure_normal_pressure",
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
    print("Available builds (fastest first). Hardware input smoothing "
          "is ON by default in every build;\nadd --raw to disable it "
          "(unfiltered pen input):\n")
    for name in ("release", "conservative-470",
                 "conservative-420", "conservative-380"):
        build = BUILDS[name]
        print(f"  {name:18} {build['rate']:8} {build['desc']}")


def patch(path, variant, raw, output):
    build = BUILDS[variant]
    stock = load_stock(path)

    patches = list(build["patches"])
    if raw:
        patches += RAW_PATCHES
    image = apply_patches(stock, patches)

    expected_md5 = build["md5_raw"] if raw else build["md5"]
    image_md5 = hashlib.md5(image).hexdigest()
    if image_md5 != expected_md5:
        sys.exit(f"ABORT: built image md5 {image_md5} does not match the "
                 f"reference build ({expected_md5}). Nothing written — "
                 f"report this bug.")

    suffix = "-raw" if raw else ""
    output = output or f"dec_T167_190325_{variant}{suffix}.bin"
    with open(output, "wb") as f:
        f.write(image)

    print(f"built {variant} "
          f"(hardware smoothing {'OFF' if raw else 'ON'}): {output}")
    print(f"  {len(patches)} patches applied, output md5 {image_md5} "
          f"(matches the reference build)")
    print(f"  flash with: sudo python3 {os.path.basename(sys.argv[0])} "
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
    """The tablet's LDROM bootloader (0416:3f00), via libusb."""

    def __init__(self, lib):
        self.lib = lib
        self.handle = None

    def wait_and_open(self, wait_seconds):
        """Wait for the bootloader to appear (it shows for ~2.5s after
        replugging the tablet), then claim it."""
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            self.handle = open_device(self.lib, BOOTLOADER_VENDOR_ID,
                                      BOOTLOADER_PRODUCT_ID,
                                      detach_kernel_driver=True)
            if self.handle:
                return True
            time.sleep(0.01)
        return False

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

    lib = load_libusb()
    if lib.libusb_init(None) != 0:
        sys.exit("libusb_init failed")
    device = BootloaderDevice(lib)
    print("waiting for LDROM 0416:3f00 ... UNPLUG AND REPLUG THE TABLET NOW")
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
    p_patch.add_argument("image", help="the decrypted stock image")
    p_patch.add_argument("-o", "--output",
                         help="output path (default: "
                              "dec_T167_190325_<variant>[-raw].bin)")
    p_patch.add_argument("--variant", default="release",
                         choices=sorted(BUILDS),
                         help="which build to make (default: release)")
    p_patch.add_argument("--raw", action="store_true",
                         help="disable the hardware input smoothing for "
                              "unfiltered pen input")
    p_patch.add_argument("--list", action="store_true",
                         help="list the available builds")

    p_flash = sub.add_parser("flash",
                             help="flash an image (needs root; replug the "
                                  "tablet when told)")
    p_flash.add_argument("image", help="image file to flash")

    args = parser.parse_args()

    if args.command == "identify":
        identify()
    elif args.command == "decrypt":
        decrypt(args.image)
    elif args.command == "patch":
        if args.list:
            print_build_list()
        else:
            patch(args.image, args.variant, args.raw, args.output)
    elif args.command == "flash":
        sys.exit(flash(args.image))


if __name__ == "__main__":
    main()
