# H1060P / T167 report-rate unlock

Patches for the Huion H1060P (board **T167**, firmware **190325**) that raise
the tablet's USB report rate from the stock **~231 Hz** to **~470 Hz** (an
experimental **~500 Hz** build is also available) and optionally disable the
built-in input smoothing — for lower input latency in
games like osu!.

Everything here is your own risk, for your own device. The tools contain no
Huion firmware bytes; they describe modifications to the official firmware
image *you* download. Do not redistribute patched images.

**Only for H1060P tablets with the T167 board.** Never flash a T167 image to
a T205 board (2021+ tablets, different silicon) or any other model — step 1
below checks this.

**AI DISCLOSURE** I made use of AI heavily during the reverse engineering and writing of scripts.

## Requirements

- Python 3 and libusb (`libusb-1.0`, in every distro's repos)
- A USB cable, and the willingness to reflash. The flash is reversible: the
  stock firmware can always be flashed back the same way.

**Windows / macOS:** `decrypt` and `patch` work as-is (they only read and
write files). `identify` and `flash` additionally need libusb installed
(Windows: the libusb-1.0.dll from <https://libusb.info>, placed next to
the script or on PATH) and the tablet's USB devices bound to WinUSB with
[Zadig](https://zadig.akeo.ie) — once for the tablet
(`256C:006D`) and once for the bootloader (`0416:3F00`, which appears
while replugging during the flash). This path is untested; Linux is the
reference platform.

## Quick start

### 1. Identify your tablet

```bash
python3 h1060p.py identify
```

Must print `HUION_T167_190325`. Anything else (e.g. `HUION_T205_…`) — stop,
this project is not for your tablet. Reading the string needs USB access;
run as root if it reports no tablet.

### 2. Download the official firmware

Huion's own update server hosts the stock image:

> **H1060P / T167 / 190325**
> <http://zyz.huion.cn/api/upload/2019-07-27/701dffc6a15af0230da9783654c98f81.bin>
> md5 `4cc61c5ce98cd73695c48fe617e23c6`

(Same file the Huion Firmware Update tool fetches.) Save it as
`H1060P_HUION_T167_190325.bin`.

### 3. Decrypt it

Huion distributes the image with a byte-substitution cipher:

```bash
python3 h1060p.py decrypt H1060P_HUION_T167_190325.bin
```

This writes `dec_H1060P_HUION_T167_190325.bin` (md5
`c918ce0d59b472c6db0d967baedf774d`). The patcher refuses any input that is
not exactly this file.

### 4. Patch it

```bash
python3 h1060p.py patch --list     # see all builds
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin            # recommended, smoothing on
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --raw      # recommended, smoothing off
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --variant 500hz --raw   # experimental ~500Hz
```

The tablet's built-in smoothing (EMA/SMA filters) is **on** by default,
like stock; `--raw` turns it off for completely unfiltered pen input.
Both are plain options — pick whichever you prefer.

| Build | Rate | What it is |
|---|---|---|
| `470hz` | ~470Hz | recommended and default — the highest rate with no observed issues |
| `500hz` | ~500Hz | experimental — every improvement incl. the shortest sensor waits; has shown stuttering in live use |
| `420hz` | ~420Hz | additionally stock math routines |
| `380hz` | ~380Hz | additionally stock coil scan — smallest change from stock |

The default (and what the plain commands above build) is `470hz`.
`500hz` is the experimental fastest build — try it with `--variant 500hz`
and reflash the default if it stutters. The 420hz and 380hz builds step
further toward stock behavior, for troubleshooting.
The patcher verifies every patch location against the expected stock bytes
and checks the finished image against the known-good reference build before
saving — it cannot silently produce something else.

### 5. Flash it

```bash
sudo python3 h1060p.py flash dec_T167_190325_470hz-raw.bin
```

The flash command reboots the tablet into the NuMicro LDROM bootloader over
USB and writes the image. Follow its prompts: it asks you to **unplug and
replug the tablet** once to enter the bootloader, and once more afterwards
to boot the new firmware. Takes about two seconds.

### 6. Verify

```bash
python3 h1060p.py identify   # still HUION_T167_190325
```

Check the report rate in OpenTabletDriver (tablet → troubleshooting →
rate) or any tablet rate tool: ~470 Hz with the pen hovering
(~500 Hz on the experimental build).

## Rollback

Reflash the stock image the same way — you already have it from step 3:

```bash
sudo python3 h1060p.py flash dec_H1060P_HUION_T167_190325.bin
```

## Documentation

- `docs/HARDWARE.md` — how the tablet works: memory map, peripherals,
  sensing architecture, report pipeline, performance physics
- `docs/FLASHING.md` — the bootloader flashing protocol, packet by
  packet, and why flashing is always recoverable
- `docs/PATCHES.md` — every patch with its firmware context, the exact
  change, and the evidence that it is safe

## What the patches do (short version)

Each report cycle the tablet locates the pen with a resonant scan: it
excites coils, waits for the pen's resonant circuit to respond, and reads
the signal back — 21 sensor readings per cycle on stock firmware. The rate
is purely the time that scan takes. The patches remove only what live
testing proved safe to remove:

- **shorter, still-safe settle waits** — the firmware waits generous fixed
  times for its analog circuitry between readings; the patcher uses the
  shortest values that passed extensive live testing
- **removed tilt calculation** — four extra readings per cycle feeding two
  HID bytes no driver on this tablet consumes
- **removed redundant re-measures** — the report builder re-ran part of the
  scan mid-report even though a fresh result from the same cycle existed
- **5-coil window instead of 6** — the outermost coil of each axis scan is
  almost never needed
- **rewritten math routines** — the pressure math used slow generic 64-bit
  multiply/divide loops; the replacements are bit-exact, proven against the
  originals over thousands of test vectors

What is deliberately *not* touched: the resonant pulse timing (the pen's
physics), and — unless you pass `--raw` — the input smoothing filters.

## FAQ

**Is this safe?** The flasher only writes the writable APROM through the
manufacturer's own bootloader protocol and verifies every byte it sends
(plus a final checksum ACK from the tablet). The bootloader itself is never
modified and stays available at every replug — the same path the official
updater uses. Worst case, reflash the stock image.

**Why not 1000 Hz?** The scan is real physics: ~15 resonant measurements per
report, each a burst of pulses plus settling time. ~470 Hz is the highest
rate with no observed issues (the experimental build reaches ~500 Hz);
the absolute ceiling of this sensing architecture is around ~700 Hz with
signal-quality trade-offs.

**Does the driver matter?** Use OpenTabletDriver for the best result. The
stock smoothing removal (`--raw`) means positions reach the PC exactly as
measured — a good driver-side filter can then do better than the tablet's
fixed one.

## License

The tools in this repository are MIT licensed (see `LICENSE`).

This covers the tools and documentation **only**. Patched firmware images
are derivatives of Huion's proprietary firmware and are **not** covered by
any license here — keep them for personal use and do not redistribute them.

Not affiliated with Huion. For use with your own device, at your own risk.
