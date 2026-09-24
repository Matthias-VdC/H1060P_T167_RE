#!/usr/bin/env python3
"""Huion H1060P (board T167, firmware 190325) firmware patcher.

Takes your own stock firmware image and produces a patched image with a
higher USB tablet report rate (~500 reports/second instead of the stock
~231) for lower input latency.

  python3 patch_fw.py dec_T167_190325.bin            # recommended build
  python3 patch_fw.py dec_T167_190325.bin --raw      # unfiltered pen input
  python3 patch_fw.py --list                         # all builds explained

The tablet's built-in input smoothing is left ON by default, so the
build behaves like the original firmware apart from the report rate.
Add --raw to disable the smoothing filters for completely unfiltered
pen input (what competitive players usually want).

This tool contains no Huion firmware bytes, only patch descriptions
(file offsets and replacement values). It refuses to touch any input
that is not the exact stock image, asserts every patch location against
the expected stock bytes before writing, and verifies the finished
image against the known-good reference build before saving it.

Keep the patched image for personal use; do not redistribute it.
"""

import argparse
import hashlib
import sys

STOCK_MD5 = "c918ce0d59b472c6db0d967baedf774d"
STOCK_SIZE = 25304


# ---------------------------------------------------------------------------
# Patches
#
# Each entry: (file_offset, expected_stock_hex, replacement_hex, description)
#
# The file is mapped at 0x08000000 in flash; the file offset equals the
# flash address minus 0x08000000. "Ticks" below are timer ticks of
# 22 CPU clocks each (~0.46 microseconds at 48 MHz).
# ---------------------------------------------------------------------------
PATCHES = {
    # --- sensor settle delays ---------------------------------------------
    # The firmware waits for the analog circuitry to settle around every
    # sensor reading. The stock values are generous; the replacements are
    # the shortest values that passed extended live testing. The
    # "conservative" builds use the milder values below instead.
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

    # --- input smoothing filters (--raw only) -----------------------------
    # Three on-board filters average pen positions over time. Replacing
    # each filter's first instructions with an immediate return turns the
    # filter into a pass-through, so positions reach the PC unmodified.
    "bypass_ema_filter": (
        0x1B34, "10b50346", "7047",
        "exponential moving average filter -> pass-through"),
    "bypass_short_moving_average": (
        0x54F4, "ffb583b0", "08467047",
        "3-sample coordinate average -> pass-through"),
    "bypass_long_moving_average": (
        0x3BE0, "ffb583b0", "08467047",
        "15-sample coordinate average -> pass-through"),

    # --- tilt calculation --------------------------------------------------
    # Four extra sensor readings per cycle feed a tilt value that occupies
    # two HID report bytes which no driver on this tablet consumes.
    # Replacing each entry with an immediate return removes the readings.
    "remove_tilt_x": (
        0x3FA0, "70b50546", "70470546",
        "X tilt readings -> skip (4 sensor readings per cycle saved)"),
    "remove_tilt_y": (
        0x3EA8, "70b50546", "70470546",
        "Y tilt readings -> skip (4 sensor readings per cycle saved)"),

    # --- redundant re-measurements ------------------------------------------
    # While building each report, the firmware re-runs part of the pen
    # position scan a second time in the same cycle, although a fresh
    # result from the same cycle already exists. Replacing the call with
    # two no-ops makes the report use the already-measured values.
    "remove_remeasure_normal_pressure": (
        0x322E, "00f051ff", "00bf00bf",
        "report-time re-scan (normal pressure range) -> skip"),
    "remove_remeasure_light_touch": (
        0x32A6, "00f015ff", "00bf00bf",
        "report-time re-scan (light touch range) -> skip"),

    # --- position window scan ----------------------------------------------
    # To locate the pen along each axis, the firmware reads 6 adjacent
    # coils per cycle; the outermost one is almost never needed. These
    # patches read 5 instead and explicitly zero the unused slot.
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
    # No-op patches: they only assert that a nearby constant still points
    # at the same memory location, so a drifted build aborts safely.
    "x_window_data_location_check": (
        0x4314, "78060020", "78060020",
        "X axis scan buffer location check (assert only)"),
    "y_window_data_location_check": (
        0x428C, "98060020", "98060020",
        "Y axis scan buffer location check (assert only)"),

    # --- math runtime ---------------------------------------------------------
    # The compiler emitted the pen pressure arithmetic as slow generic
    # 64-bit multiply/divide loops that call a helper subroutine on every
    # iteration. These replacements are hand-written equivalents, proven
    # bit-exact against the originals over thousands of random and
    # boundary test vectors in a Thumb-1 simulator before flashing.
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

# Patches that only apply with --raw (smoothing OFF).
RAW_PATCHES = [
    "bypass_ema_filter",
    "bypass_short_moving_average",
    "bypass_long_moving_average",
]

# ---------------------------------------------------------------------------
# Builds
#
# The recommended build includes every improvement. Each conservative
# build steps one back toward stock behavior, for troubleshooting.
# "md5" is the reference checksum with smoothing ON (default);
# "md5_raw" is the reference checksum with --raw.
# ---------------------------------------------------------------------------
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
            "remove_remeasure_normal_pressure", "remove_remeasure_light_touch",
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
    with open(path, "rb") as f:
        stock = f.read()
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
    print("\nUse: python3 patch_fw.py <stock-image> "
          "[--variant <name>] [--raw]")


def main():
    parser = argparse.ArgumentParser(
        description="Patch your stock H1060P (T167, fw 190325) firmware "
                    "for a higher USB report rate.")
    parser.add_argument(
        "stock", nargs="?",
        help="path to your stock image (dec_T167_190325.bin, 25304 bytes)")
    parser.add_argument(
        "-o", "--output",
        help="output path (default: ./dec_T167_190325_<variant>[-raw].bin)")
    parser.add_argument(
        "--variant", default="release", choices=sorted(BUILDS),
        help="which build to make (default: release). If release ever "
             "misbehaves, step down: conservative-470, then -420, -380.")
    parser.add_argument(
        "--raw", action="store_true",
        help="disable the hardware input smoothing (the stock EMA/SMA "
             "filters) for unfiltered pen input. Default: smoothing "
             "stays ON, like the original firmware.")
    parser.add_argument(
        "--list", action="store_true",
        help="list the available builds and what each one is")
    args = parser.parse_args()

    if args.list:
        print_build_list()
        return
    if not args.stock:
        parser.error("provide the stock image path (or --list)")

    build = BUILDS[args.variant]
    stock = load_stock(args.stock)

    patches = list(build["patches"])
    if args.raw:
        patches += RAW_PATCHES

    image = apply_patches(stock, patches)

    expected_md5 = build["md5_raw"] if args.raw else build["md5"]
    image_md5 = hashlib.md5(image).hexdigest()
    if image_md5 != expected_md5:
        sys.exit(f"ABORT: built image md5 {image_md5} does not match the "
                 f"reference build ({expected_md5}). Nothing written — "
                 f"report this bug.")

    suffix = "-raw" if args.raw else ""
    output_path = args.output or f"dec_T167_190325_{args.variant}{suffix}.bin"
    with open(output_path, "wb") as f:
        f.write(image)

    print(f"built {args.variant} "
          f"(hardware smoothing {'OFF' if args.raw else 'ON'}): "
          f"{output_path}")
    print(f"  {len(patches)} patches applied, output md5 {image_md5} "
          f"(matches the reference build)")
    print(f"  flash with: sudo python3 flasher.py {output_path}")


if __name__ == "__main__":
    main()
