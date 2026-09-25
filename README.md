# H1060P / T167 report-rate unlock

Patches for the Huion H1060P (board **T167**, firmware **190325**) that raise
the tablet's USB report rate from the stock **~231 Hz** to **~730 Hz**, for
lower input latency in games like osu!. Slower builds (~470 Hz and below)
are available, the built-in input smoothing can be switched off, and the
pen can be made to never register clicks.

Everything here is your own risk, for your own device. The tools contain no
Huion firmware bytes; they describe modifications to the official firmware
image *you* download. Do not redistribute patched images.

**Only for H1060P tablets with the T167 board.** Never flash a T167 image to
a T205 board (2021+ tablets, different silicon) or any other model — step 1
below checks this.

**AI DISCLOSURE** I made use of AI heavily during the reverse engineering and writing of scripts.

## Requirements

- Python 3, plus libusb on Linux (`libusb-1.0`, in every distro's repos)
- A USB cable, and the willingness to reflash. The flash is reversible: the
  stock firmware can always be flashed back the same way.

**Windows:** nothing else to install. The tool talks to the tablet through
Windows' built-in HID driver, the same way Huion's own updater does: no
libusb, no Zadig, no administrator rights. Run the commands below as
`python h1060p.py …` (without `sudo`). If you followed older instructions
and bound the tablet or its bootloader to WinUSB with Zadig, restore the
original driver first (Device Manager → the device → Uninstall device,
deleting its driver, then replug) — the tool cannot see a WinUSB-bound
device. The Windows path is new: it has been checked under Wine and
against a simulated bootloader, but not yet on a real Windows machine.
Linux is the reference platform.

**macOS:** untested (it would go through libusb, like Linux).

## Quick start

### 1. Identify your tablet

```bash
python3 h1060p.py identify
```

Must print `HUION_T167_190325`. Anything else (e.g. `HUION_T205_…`) — stop,
this project is not for your tablet. On Linux, reading the string needs
USB access; run as root if it reports no tablet.

### 2. Download the official firmware

Huion's own update server hosts the stock image:

> **H1060P / T167 / 190325**
> <http://zyz.huion.cn/api/upload/2019-07-27/701dffc6a15af0230da9783654c98f81.bin>
> md5 `4cc61c5ce98cd73695c48fe617e23c77`

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
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin             # default: 730hz
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --raw      # + smoothing off
python3 h1060p.py patch dec_H1060P_HUION_T167_190325.bin --raw --no-taps   # + pen never clicks
```

Smoothing is **on** by default, like stock; `--raw` turns it off for
unfiltered pen input. With the default build, `--no-taps` makes the pen
never register a click. All plain options — pick what you prefer.

| Build | Rate | What it is |
|---|---|---|
| `730hz` | ~730Hz | default, fastest — pen pressure updates every 15th report (~49/s), fine if you click with a keyboard |
| `470hz` | ~470Hz | pressure updates every report and taps register instantly — pick this if you click with the pen |
| `500hz` | ~560Hz | experimental — shortest sensor waits, may stutter |
| `420hz` | ~420Hz | additionally stock math routines |
| `380hz` | ~380Hz | additionally stock coil scan — smallest change from stock |

In every build the position scan runs every report. Only the separate
pressure/touch tracker is slowed in the 730hz build, so pen clicks can
arrive up to ~20 ms late — the reason it is not the right build for pen
clickers. The patcher verifies every patch location against the expected
stock bytes and the finished image against the known-good reference
build before saving — it cannot silently produce something else.

### 5. Flash it

```bash
sudo python3 h1060p.py flash dec_T167_190325_730hz-raw.bin
```

The flash command reboots the tablet into the NuMicro LDROM bootloader over
USB and writes the image. Follow its prompts: it asks you to **unplug and
replug the tablet** once to enter the bootloader, and once more afterwards
to boot the new firmware. Takes about two seconds.

On Windows the command is `python h1060p.py flash …`, no admin needed. The
very first time, Windows may still be installing the bootloader's driver
when its ~2.5 s window closes and the tablet just starts up normally —
replug again while the tool is still waiting.

### 6. Verify

```bash
python3 h1060p.py identify   # still HUION_T167_190325
```

Check the report rate in OpenTabletDriver (tablet → troubleshooting →
rate) or any tablet rate tool: ~730 Hz with the pen hovering
(~470 Hz on the 470hz build, ~560 Hz on the experimental one).

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
- **1 ms USB polling** — stock firmware tells the PC to collect a report
  only every 2 ms (adding ~1 ms average latency and capping delivery at
  500 reports/s, which also caused the fastest build's stutter); patched
  builds ask for the full-speed norm of every 1 ms

What is deliberately *not* touched: the resonant pulse timing (the pen's
physics), and — unless you pass `--raw` — the input smoothing filters.

## FAQ

**Is this safe?** The flasher only writes the writable APROM through the
manufacturer's own bootloader protocol and verifies every byte it sends
(plus a final checksum ACK from the tablet). The bootloader itself is never
modified and stays available at every replug — the same path the official
updater uses. Worst case, reflash the stock image.

**Why not 1000 Hz?** Every report needs ~10-15 resonant coil
measurements, each a burst of pulses plus settling time — the absolute
ceiling of this hardware is ~750 Hz, with ~730 Hz reached in live play.
USB used to add its own limit: stock firmware asks the PC to collect a
report only every 2 ms (500 reports/s max); every build here changes
that to the full-speed norm of 1 ms.

**Does the driver matter?** Use OpenTabletDriver for the best result. The
stock smoothing removal (`--raw`) means positions reach the PC exactly as
measured — a good driver-side filter can then do better than the tablet's
fixed one.

**Why does my cursor jitter or vibrate with `--raw`?** Each report is the
raw per-cycle sensor estimate, which varies by a few counts even under a
perfectly still pen. At default area sizes that is sub-pixel and
invisible, but a small active area maps fewer tablet counts to each
screen pixel and magnifies it. The default (smoothing on) build damps
it, and a driver-side low-pass filter in OpenTabletDriver does the same
job with tunable strength — both are plain options.

## License

The tools in this repository are MIT licensed (see `LICENSE`).

This covers the tools and documentation **only**. Patched firmware images
are derivatives of Huion's proprietary firmware and are **not** covered by
any license here — keep them for personal use and do not redistribute them.

Not affiliated with Huion. For use with your own device, at your own risk.
