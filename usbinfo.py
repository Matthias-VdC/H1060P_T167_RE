#!/usr/bin/env python3
"""usbinfo.py - collect the USB identifiers of any Huion (VID 256C) HID
device, read-only: VID/PID, bcdDevice, and the string descriptors that
are known-safe to read (manufacturer, product, serial, and index 201 -
the model string, e.g. HUION_T167_190325).

At most four string reads happen: the three indices the device
descriptor declares (manufacturer, product, serial - the serial one is
often undeclared, so fewer in practice), plus index 201. Other indices
are NOT read on purpose: on Huion tablets some string reads are
commands (index 200 switches the tablet into its vendor mode) and on
unknown boards other indices may do anything at all.

Works on Windows (built-in HID, no admin) and Linux (libusb). On
Windows only HID devices can be listed; a non-HID device such as a
DFU bootloader must be spotted with Device Manager or USBDeview.
On Linux any USB device can be listed, e.g.:
    python3 usbinfo.py 28e9 0189
"""

import ctypes
import sys

WINDOWS = sys.platform == "win32"
TABLET_VID = 0x256C

# Which string index holds the manufacturer/product/serial string is
# chosen per device in its device descriptor, so those are read via the
# descriptor-declared indices (Linux) or the HidD_Get*String calls that
# resolve them (Windows). Index 201 is the Huion model string (read the
# same way by h1060p.py's identify). No other index is read: on Huion
# tablets some string reads are commands (index 200 switches the tablet
# into its vendor mode) and on unknown boards other indices may do
# anything at all.

# ---------------------------------------------------------------------------
# Windows: built-in HID stack
# ---------------------------------------------------------------------------

if WINDOWS:
    CR_SUCCESS = 0
    CR_BUFFER_SMALL = 0x1A
    CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0x0
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    FILE_SHARE_READ_WRITE = 3

    class GUID(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32),
                    ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16),
                    ("data4", ctypes.c_ubyte * 8)]

    class HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32),
                    ("vendor_id", ctypes.c_uint16),
                    ("product_id", ctypes.c_uint16),
                    ("version", ctypes.c_uint16)]  # bcdDevice

    def run(vid, pid):
        hid = ctypes.WinDLL("hid.dll", use_last_error=True)
        cfgmgr32 = ctypes.WinDLL("cfgmgr32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        hid.HidD_GetAttributes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
        hid.HidD_GetIndexedString.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        hid.HidD_GetManufacturerString.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        hid.HidD_GetProductString.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        hid.HidD_GetSerialNumberString.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]

        guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(guid))
        while True:
            length = ctypes.c_ulong()
            if cfgmgr32.CM_Get_Device_Interface_List_SizeW(
                    ctypes.byref(length), ctypes.byref(guid), None,
                    CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != CR_SUCCESS:
                sys.exit("CM_Get_Device_Interface_List_SizeW failed")
            paths = ctypes.create_unicode_buffer(length.value)
            result = cfgmgr32.CM_Get_Device_Interface_ListW(
                ctypes.byref(guid), None, paths, length.value,
                CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
            if result == CR_BUFFER_SMALL:
                continue  # a device arrived meanwhile: resize and retry
            break
        if result != CR_SUCCESS:
            sys.exit("CM_Get_Device_Interface_ListW failed")

        ids = f"vid_{vid:04x}"
        if pid is not None:
            ids += f"&pid_{pid:04x}"
        for path in paths[:].split("\0"):
            if not path or ids not in path.lower():
                continue
            print(path)
            handle = kernel32.CreateFileW(path, 0, FILE_SHARE_READ_WRITE,
                                          None, OPEN_EXISTING, 0, None)
            if handle in (None, INVALID_HANDLE_VALUE):
                print("  (could not open)")
                continue
            try:
                attrs = HIDD_ATTRIBUTES(size=ctypes.sizeof(HIDD_ATTRIBUTES))
                if hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                    print(f"  USB {attrs.vendor_id:04x}:"
                          f"{attrs.product_id:04x}  bcdDevice = "
                          f"{attrs.version >> 8:02x}."
                          f"{attrs.version & 0xFF:02x}")
                # The OS resolves the descriptor-declared indices for
                # these; no raw index guessing.
                buf = ctypes.create_unicode_buffer(256)
                for label, getter in (
                        ("manufacturer", hid.HidD_GetManufacturerString),
                        ("product", hid.HidD_GetProductString),
                        ("serial", hid.HidD_GetSerialNumberString)):
                    if getter(handle, buf, ctypes.sizeof(buf)):
                        text = buf.value.strip()
                        if text:
                            print(f"  {label} = {text!r}")
                if hid.HidD_GetIndexedString(handle, 201, buf,
                                              ctypes.sizeof(buf)):
                    text = buf.value.strip()
                    if text:
                        print(f"  string[201] = {text!r}")
            finally:
                kernel32.CloseHandle(handle)
            print()

# ---------------------------------------------------------------------------
# Linux: libusb (sees all USB devices, including DFU bootloaders)
# ---------------------------------------------------------------------------

def load_libusb():
    for name in ("libusb-1.0.so.0", "libusb-1.0.so", "libusb-1.0.0.dylib"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    sys.exit("libusb-1.0 not found (install your distro's libusb package)")


def run_linux(vid, pid):
    lib = load_libusb()
    lib.libusb_init(None)
    head = ctypes.POINTER(ctypes.c_void_p)()
    count = lib.libusb_get_device_list(None, ctypes.byref(head))
    if count < 0:
        sys.exit("libusb enumeration failed")
    devices = ctypes.cast(head, ctypes.POINTER(ctypes.c_void_p * count))
    try:
        for i in range(count):
            device = ctypes.cast(devices.contents[i], ctypes.c_void_p)
            desc = ctypes.create_string_buffer(18)
            if lib.libusb_get_device_descriptor(device, desc) != 0:
                continue
            dvid = int.from_bytes(desc[8:10], "little")
            if dvid != vid:
                continue
            dpid = int.from_bytes(desc[10:12], "little")
            if pid is not None and dpid != pid:
                continue
            bcd = int.from_bytes(desc[12:14], "little")
            print(f"USB {dvid:04x}:{dpid:04x}  bcdDevice = "
                  f"{bcd >> 8:02x}.{bcd & 0xFF:02x}")
            # descriptor-declared string indices: iManufacturer,
            # iProduct, iSerialNumber (0 = none) - plus the model 201
            indices = []
            for off in (14, 15, 16):
                index = desc.raw[off]  # desc[i] is bytes; .raw[i] is int
                if index:
                    indices.append(index)
            indices.append(201)
            handle = ctypes.c_void_p()
            if lib.libusb_open(device, ctypes.byref(handle)) != 0:
                print("  (could not open; try sudo)")
                print()
                continue
            try:
                buf = ctypes.create_string_buffer(256)
                for index in indices:
                    n = lib.libusb_get_string_descriptor_ascii(
                        handle, index, buf, ctypes.sizeof(buf))
                    if n > 0:
                        text = buf[:n].decode("utf-8", "replace").strip()
                        if text:
                            print(f"  string[{index:3}] = {text!r}")
            finally:
                lib.libusb_close(handle)
            print()
    finally:
        lib.libusb_free_device_list(head, 1)
        lib.libusb_exit(None)


def main():
    vid = TABLET_VID
    pid = None
    if len(sys.argv) == 3:
        vid = int(sys.argv[1], 16)
        pid = int(sys.argv[2], 16)
    elif len(sys.argv) == 2:
        vid = int(sys.argv[1], 16)
    if WINDOWS:
        run(vid, pid)
    else:
        run_linux(vid, pid)


if __name__ == "__main__":
    main()
