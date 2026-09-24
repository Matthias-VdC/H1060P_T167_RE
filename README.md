# H1060P / T167 report-rate unlock

Patches for the Huion H1060P (board **T167**, firmware **190325**) that raise
the tablet's USB report rate from the stock **~231 Hz** to **~500 Hz** and
optionally disable the built-in input smoothing — for lower input latency in
games like osu!.

Everything here is your own risk, for your own device. The tools contain no
Huion firmware bytes; they describe modifications to the official firmware
image *you* download. Do not redistribute patched images.

**Only for H1060P tablets with the T167 board.** Never flash a T167 image to
a T205 board (2021+ tablets, different silicon) or any other model — step 1
below checks this.

## Requirements

- Linux, Python 3, and `libusb` (`libusb-1.0`, in every distro's repos)
- A USB cable, and the willingness to reflash. The flash is reversible: the
  stock firmware can always be flashed back the same way.

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
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin            # smoothing on
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --raw      # smoothing off
```

The tablet's built-in smoothing (EMA/SMA filters) is **on** by default,
like stock; `--raw` turns it off for completely unfiltered pen input.
Both are plain options — pick whichever you prefer.

| Build | Rate | What it is |
|---|---|---|
| `release` | ~500Hz | every improvement (default) |
| `conservative-470` | ~470Hz | gentler sensor wait times |
| `conservative-420` | ~420Hz | additionally stock math routines |
| `conservative-380` | ~380Hz | additionally stock coil scan — smallest change from stock |

If `release` ever misbehaves, step down through the conservative builds.
The patcher verifies every patch location against the expected stock bytes
and checks the finished image against the known-good reference build before
saving — it cannot silently produce something else.

### 5. Flash it

```bash
sudo python3 h1060p.py flash dec_T167_190325_release-raw.bin
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
rate) or any tablet rate tool: ~500 Hz with the pen hovering.

## Rollback

Reflash the stock image the same way — you already have it from step 3:

```bash
sudo python3 h1060p.py flash dec_H1060P_HUION_T167_190325.bin
```

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
report, each a burst of pulses plus settling time. ~500 Hz is the highest
rate with no measurable quality loss; the absolute ceiling of this sensing
architecture is around ~700 Hz with signal-quality trade-offs.

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
