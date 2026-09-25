# What the patches change — firmware side

Every patch in this project, with the firmware context around it, the
exact change, and why it is safe. Patch sites are file offsets in the
25304-byte APROM image (mapped at 0x08000000). The companion tool
(`h1060p.py`) applies exactly these changes and verifies each location
against the expected stock bytes before writing.

Background on the scan architecture is in `HARDWARE.md`. Short
instruction excerpts are shown only at the patch sites — that is what a
diff inherently is; the surrounding firmware is described, not
reproduced.

## Overview

| Group | Sites | Effect |
|---|---|---|
| sensor settle delays | 3 | shorter (still safe) waits per measurement |
| input smoothing | 3 | optional: filters become pass-through (`--raw`) |
| tilt calculation | 2 | 4 fewer measurements per cycle |
| redundant re-measures | 2 | 5 fewer measurements per touch report |
| position window | 4 (+2 asserts) | one fewer coil read per axis per cycle |
| math runtime | 4 | ~6x faster pressure interpolation, bit-exact |
| USB report polling | 2 | host collects every 1 ms instead of every 2 ms |

Total footprint vs stock: about 210 bytes changed, most of it inside the
four replaced math routines. Everything is reversible by reflashing the
stock image.

---

## 1. Sensor settle delays

**Firmware context.** Every sensor measurement funnels through one ADC
read routine. Around the actual conversion it arms a SysTick timer and
busy-waits three times: after powering the ADC up (site A), after
switching the analog input mux (site B), and — only when the next
excitation uses a different pressure band — a ring-down wait after the
measurement (site C). Each wait is a single instruction loading the
tick count into the call:

```asm
0x21FA:  movs r0, #0x0F    ; site A — ADC wake-up settle (15 ticks)
0x220C:  movs r0, #0x46    ; site B — mux settle (70 ticks)
0x22A0:  movs r0, #0x96    ; site C — band ring-down (150 ticks)
```

One tick is 22 CPU clocks ≈ 0.458 µs, so stock waits ~6.9 µs / ~32 µs /
~68.7 µs respectively.

**Change.** Only the immediate values change:

| Site | Stock | 470hz (default) | 500hz (experimental) |
|---|---|---|---|
| A (0x21FA) | 15 ticks | 6 | 4 |
| B (0x220C) | 70 | 32 | 16 |
| C (0x22A0) | 150 | 40 | 25 |

**Why it is safe.** The stock values are generous one-size waits; the
patched values were found by live testing at every stage of the
project, checking hover range, line straightness, edge behavior, and
pressure stability. An independent custom-firmware author on similar
hardware landed in the same order of magnitude, confirming these are
physical settling times, not arbitrary numbers. The 500hz column is
marked experimental because on one unit it produced visible stutter —
the 470hz column has the long clean history and is the default.

## 2. Input smoothing (only with `--raw`)

**Firmware context.** Between the scan and the USB report the firmware
smooths the position twice: a speed-adaptive exponential moving average
on the coordinates (near-zero lag while the pen moves, strong damping
while it is still), then a 3-sample moving average on the coordinates.
A third filter, a 15-sample moving average, smooths only the tilt
estimate — a value that lands in report bytes 10–11, which tablet
drivers ignore on this model. It has no effect on the cursor.

**Change.** Each filter's first instruction is replaced so the
function returns immediately, passing its input through unchanged:

```asm
0x1B34:  push {r4, lr}      ->  bx lr            ; EMA on coordinates
0x54F4:  push {r0-r7, lr}   ->  mov r0, r1; bx lr ; 3-sample coord SMA
0x3BE0:  push {r0-r7, lr}   ->  mov r0, r1; bx lr ; 15-sample tilt SMA
```

**Why it is safe.** These are pure output transforms; removing them
changes no measurement or tracking logic. Note for users: with `--raw`
the reported position is the raw per-cycle sensor estimate, which
jitters a few counts even under a perfectly still pen. That is normal
and invisible at default area sizes, but a small active area (fewer
tablet counts per screen pixel) magnifies it into visible cursor
vibration. The stock coordinate filters damp exactly this; a
driver-side filter can do the same job.

## 3. Tilt calculation

**Firmware context.** Once per cycle, beyond the position scan, the
firmware measures four extra coils to compute pen tilt, stored in two
HID report bytes. No driver on this tablet reads them.

**Change.** The two tilt measurement functions return immediately at
entry (same instruction replacement as the filters above):

```asm
0x3FA0:  push {r4-r6, lr}   ->  bx lr            ; X tilt readings
0x3EA8:  push {r4-r6, lr}   ->  bx lr            ; Y tilt readings
```

**Why it is safe.** The tilt outputs are never consumed downstream on
this device; the report bytes simply stay zero.

## 4. Redundant re-measures in the report builder

**Firmware context.** While assembling each USB report, the firmware
re-runs the 5-band pressure scan *a second time* in the same cycle —
in two separate code blocks, one for the normal pressure range and one
for the light-touch range — although the scan loop's own run from the
same cycle already produced fresh values a moment earlier.

**Change.** Each call site is replaced with two no-operations; the
report builder keeps using the record the same-cycle scan wrote:

```asm
0x322E:  bl band_scan   ->  nop; nop   ; normal-pressure re-run
0x32A6:  bl band_scan   ->  nop; nop   ; light-touch re-run
```

**Why it is safe.** The values used afterwards were measured in the
same cycle, milliseconds newer than stock's "fresh" re-run was
intended to be. Live testing showed identical feel; hover rate is
unchanged (hover never used these blocks), and touch reports stop
carrying five extra measurements.

## 5. Position window shrink

**Firmware context.** To locate the pen along each axis, the firmware
scans a sliding window of 6 adjacent coils per cycle and peak-picks.
The scan loop starts at slot 0 and skips one guard branch at the top.

**Change.** The loop starts one slot later (5 coils instead of 6), and
the now-skipped first slot is explicitly zeroed so every downstream
consumer (peak-pick, classification, snapshot) sees the same window
shape it was built for:

```asm
0x4292:  movs r4, #0      ->  movs r4, #1      ; X window: start at slot 1
0x4294:  (loop preamble)  ->  str 0, [x_window] ; X: slot 0 := 0
0x4216:  movs r4, #0      ->  movs r4, #1      ; Y window: start at slot 1
0x4218:  (loop preamble)  ->  str 0, [y_window] ; Y: slot 0 := 0
```

Two further sites assert unchanged constants (the window buffer
addresses) purely as build-time safety checks.

**Why it is safe.** The dropped coil is the outermost one of the
window — it is only relevant when the pen moves more than one coil
per cycle, which at these report rates it cannot. Coordinate output
proved identical in live testing.

## 6. Math runtime (the pressure interpolation)

**Firmware context.** Once per cycle the firmware computes the
pressure "fine value" from the band window with a rational estimator
over two window values (A, B) and two adjacent calibration-table
entries (C, D):

```
P = B*C - A*D        Q = B*C^2 - A*D^2
fine = table[band] + (Q/P) / 2          (clamped to the neighbors)
```

The stock implementation routes this through compiler-emitted 64-bit
soft-math library routines: a multiply that calls a helper subroutine
per partial product, a 64-iteration divide that calls shift helpers
twice per iteration, a 32-bit divide built from three variable shifts
per iteration, and — the worst one — a full signed 64-bit divide used
only to halve a number. Measured cost: ~7500 executed instructions per
cycle.

**Change.** The four library routines are replaced by hand-written
equivalents at the same entry points (multiply: 3-multiply carry
chain; 64-bit divide: shift-subtract; 32-bit divide: shift-subtract
with a divide-by-zero guard; the halving: an 8-instruction shift
implementing `trunc(a/2) = (a + sign(a)) >> 1`). The full replacement
bytes are in `h1060p.py` under `fast_64bit_multiply`,
`fast_64bit_divide`, `fast_32bit_divide`, `fast_64bit_halve`.

**Why it is safe.** Every replacement was proven **bit-exact** against
the stock routine over thousands of random and boundary test vectors
in a Thumb-1 simulator (including the divide-by-zero contract and the
65-bit wrap path), and re-proven at whole-function level against a
simulated stock run with the real calibration table — 612 test cases,
zero mismatches. Result: ~1200 executed instructions, and the same
numbers the stock firmware would have produced, every cycle.

## 7. USB report polling

**Firmware context.** The pen endpoint's descriptor tells the host how
often to collect a report. Stock declares an interrupt endpoint with
bInterval = 2 — the host polls only every 2 ms. Two consequences:

- every report waits 0–2 ms (average ~1 ms) before the PC reads it,
  and the 2 ms scan period beats against the 2 ms poll period, so
  roughly every 16th report waits a further 2 ms;
- delivery is capped at 500 reports/s, and the firmware's report
  sender never blocks — when the previous report is still uncollected
  the new one is silently dropped. A scan producing faster than
  500 Hz therefore makes the PC periodically receive a stale report:
  the periodic hitch seen on the fastest build.

**Change.** One byte in each endpoint descriptor:

```asm
0x59A3:  bInterval 02 -> 01    ; pen endpoint (EP 0x81)
0x59BC:  bInterval 02 -> 01    ; default-mode endpoint (EP 0x82)
```

**Why it is safe.** Nothing in the tablet changes: the scan timing,
the reports, and the protocol are untouched — only how often the host
asks for them (1 ms is the full-speed norm; bInterval 2 buys the
vendor nothing but battery on the host side). Delivery latency halves
on average, worst case drops from 2 ms to 1 ms, and no report below
1000 Hz can be dropped anymore. Applied in every build.

---

## History

The groups above were built and validated in this order, each live-
tested before the next: settle delays (231→330 Hz) → smoothing
options → tilt removal (→380 Hz) → re-measure removal → window shrink
(→420 Hz) → math runtime (→470 Hz) → deeper settle tuning (→500 Hz,
experimental). Later additions: the light-touch re-measure removal
joined the default build (no more ~350 Hz dip while tapping), and the
USB 1 ms polling fix landed in every build after the 2 ms cap was
identified as the cause of the fastest build's stutter.
