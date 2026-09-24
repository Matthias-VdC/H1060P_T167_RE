#!/usr/bin/env python3
"""Huion H1060P (board T167) model identifier.

Reads the tablet's USB string descriptor 201 and prints the model and
firmware identifier, for example:

    HUION_T167_190325

This is the same string the Linux hid-uclogic driver reads at every
boot, and it is the safe way to tell which firmware family a tablet
belongs to before flashing: a T167 image must never be flashed to a
T205 board (different silicon) or vice versa.

Reading string descriptors is a normal, non-destructive operation; any
state it may cause resets when the tablet is unplugged and replugged.

Usage:
    python3 model.py                # prints e.g. HUION_T167_190325
    from model import get_model     # returns the string, or None

Requires no third-party packages (libusb is loaded via ctypes).
Exits non-zero with a message if no tablet is found.
"""

import ctypes
import sys

# Huion / UC-Logic vendor and product id of the tablet.
VENDOR_ID = 0x256C
PRODUCT_ID = 0x006D

# String descriptor index 201 carries the model/firmware identifier.
MODEL_STRING_INDEX = 0xC9

# Language id for string descriptors (en-US).
LANGID_US_ENGLISH = 0x0409

# USB control-transfer constants.
GET_DESCRIPTOR = 0x06
STRING_DESCRIPTOR_TYPE = 0x03
DEVICE_TO_HOST = 0x80
TRANSFER_TIMEOUT_MS = 1000

# libusb error code for "stalled" — returned when a descriptor index
# does not exist on the device.
LIBUSB_ERROR_PIPE = -9


def _load_libusb():
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
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    return lib


def _read_string_descriptor(lib, device, index, max_length=255):
    """Read one USB string descriptor. Returns its raw bytes, or b''
    if the device does not have a descriptor at that index."""
    buffer = (ctypes.c_ubyte * max_length)()
    transferred = lib.libusb_control_transfer(
        device,
        DEVICE_TO_HOST,
        GET_DESCRIPTOR,
        (STRING_DESCRIPTOR_TYPE << 8) | index,
        LANGID_US_ENGLISH,
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        max_length,
        TRANSFER_TIMEOUT_MS)
    if transferred == LIBUSB_ERROR_PIPE:
        return b""
    if transferred < 0:
        raise RuntimeError(
            f"reading string descriptor {index} failed "
            f"(libusb error {transferred})")
    return bytes(buffer[:transferred])


def _decode_utf16(raw):
    """Decode a string descriptor body (UTF-16LE) to text."""
    body = raw[2:]  # skip the bLength / bDescriptorType header
    if len(body) % 2:
        body = body[:-1]
    return body.decode("utf-16-le", errors="replace")


def get_model():
    """Return the tablet's model string (e.g. "HUION_T167_190325").

    Returns None if no tablet is found or it has no model descriptor.
    Raises RuntimeError if the USB layer itself fails.
    """
    lib = _load_libusb()
    if lib.libusb_init(None) != 0:
        raise RuntimeError("libusb_init failed")
    try:
        device = lib.libusb_open_device_with_vid_pid(
            None, VENDOR_ID, PRODUCT_ID)
        if not device:
            return None
        try:
            raw = _read_string_descriptor(lib, device, MODEL_STRING_INDEX)
            if len(raw) < 4:
                return None  # empty or missing model descriptor
            return _decode_utf16(raw) or None
        finally:
            lib.libusb_close(device)
    finally:
        # libusb_exit(None) on a shared context is safe here; the default
        # context is reference-counted per init.
        lib.libusb_exit(None)


def main():
    try:
        model = get_model()
    except RuntimeError as error:
        sys.exit(str(error))
    if model is None:
        sys.exit(f"no tablet found at {VENDOR_ID:04x}:{PRODUCT_ID:04x} — "
                 f"is it plugged in? (reading descriptors may need root)")
    print(model)


if __name__ == "__main__":
    main()
