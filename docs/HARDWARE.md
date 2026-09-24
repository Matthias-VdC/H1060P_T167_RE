# H1060P / T167 hardware notes

Reverse-engineering findings for the Huion H1060P (board T167, firmware
190325). Everything here is factual documentation of how the device
works, obtained from a tablet we own; no firmware code is included.
Addresses are flash (0x0800xxxx) or RAM (0x2000xxxx).

## System overview

| Item | Value |
|---|---|
| MCU | Nuvoton NuMicro NUC100/120 family — ARM Cortex-M0 @ 48 MHz |
| Flash (APROM) | mapped at 0x08000000, image 25304 bytes |
| Bootloader (LDROM) | NuMicro ISP at 0x00100000, appears as USB 0416:3F00 while flashing |
| RAM | ~3.1 KB used (0x20000000–0x20000C70); initial SP = 0x20000C70 |
| USB | full-speed device, VID 256C PID 006D; model string "HUION_T167_190325" on string descriptor 201 |
| Toolchain (vendor) | Keil/ARMCC, C library scatter-loading |
| Sensor | EMR resonant digitizer (MCU drives coils, measures the pen's resonant response) |

**Board check:** 2021+ H1060P units are board T205 (GD32F350-class,
72 MHz, different everything). Read the model string first
(`python3 h1060p.py identify`); never mix images between boards.

## Boot chain

Reset entry 0x080000D4 → C entry 0x080000C0 → `__main` at 0x0800096C,
which is a scatter-loader: it copies the initialized-data block from flash
0x5D7C to RAM 0x20000000 (0x55C bytes), then zero-fills 0x2000055C–0x20000C70.
The zero-fill region ends exactly at the vector table's initial stack
pointer — a tidy consistency proof of the layout.

## RAM map

| Address | Contents |
|---|---|
| 0x20000004 | SysTick tick scale (22 HCLK ticks per delay unit) |
| 0x200002F0 | band calibration table: 12 entries `{pad, u32}`; entries 1–10 hold the band drive frequencies 452,830–545,455 Hz, entries 0 and 11 are zero sentinels |
| 0x200005CC | pen tracking state struct (below) |
| 0x20000678 | X-axis scan buffer (5 u32 window values) |
| 0x20000698 | Y-axis scan buffer |

Tracking state struct at 0x200005CC (offsets):

| Offset | Meaning |
|---|---|
| +0x01 | current Y coil |
| +0x03 | current X coil |
| +0x24–0x34 | X snapshot (last few window values, for interpolation) |
| +0x44 | final X coordinate (raw, before scaling) |
| +0x48 | final Y coordinate |
| +0x4C–0x5C | band window (10 slots used by the pressure tracker) |
| +0x70 | pressure band index |
| +0x74 | sub-band / fine pressure value |
| +0x78 | signal magnitude |
| +0x7D | pen-valid flag |
| +0x80/+0x84 | tilt values (consumed by nothing on this tablet) |

## Peripheral map

| Peripheral | Base | Notes |
|---|---|---|
| ADC | 0x400E0000 | 12-bit SAR; control +0x20, data +0x08, status +0x30, channel enables +0x24; clocked at 22.1184 MHz / 2 ≈ 11 MHz (CLKDIV0[23:16] = 1) |
| GPIO | 0x50004000 | coil/mux select lines (pin muxing around every measurement) |
| Clock controller | 0x50000200 | CLKSEL1 +0x14, CLKDIV0 +0x18, APBCLK +0x08 |
| SysTick | 0xE000E010 | the firmware's only software-delay mechanism |

**Timing/delay system:** all firmware delays go through one function that
arms SysTick (reload = N × 22 HCLK ticks, source = HCLK) and busy-waits
for the count flag. One unit = 0.458 µs at 48 MHz. There are only six
call sites in the whole image; the three hot ones are the sensor settle
waits the patch tool adjusts.

## Sensing architecture

The tablet locates the pen by a resonant scan, repeated in a self-timed
loop — there is no report timer; report rate = 1 / scan-cycle-time.

Per cycle (patched firmware), 15 measurements:

1. **Band tracker** (function at 0x080040D4) — excites 5 consecutive
   pressure bands around the current one, argmax-picks the peak, and
   guards the band index to the calibrated range 1–10.
2. **X window** (0x08004290) — reads 5 adjacent X coils at the tracked band.
3. **Y window** (0x08004214) — same for the Y axis.

Each measurement: select mux lines → excite the coil (a burst of 29
resonant drive pulses, the pulse *timing* sets the drive frequency —
nop-chained, the loop period is the physics) → read the ADC (power-up,
settle, convert ~1.7 µs, power-down, optional ring-down wait before the
next band excitation).

Stock firmware additionally spent 4 measurements per cycle on a tilt
value nobody reads, and re-ran parts of the scan inside the report
builder; the patch tool removes both.

**Pressure fine value:** once per cycle the firmware interpolates the
peak position inside the band table with a rational estimator over the
two window values and the two adjacent table entries: with window
differences B and A and table deltas C and D, it computes
P = B·C − A·D and Q = B·C² − A·D², then
`fine = table[band] + (Q/P)/2`, clamped to the neighbor entries. The
stock implementation used ~7500 executed instructions of 64-bit
soft-math library loops per call; the patch tool replaces them with
bit-exact equivalents (~1200).

## Report pipeline

The report builder (0x08003158) emits a 12-byte HID report: bytes 2–3 X,
4–5 Y, 6–7 pressure, 8–9 high coordinate bits, 10–11 tilt.

Before output, stock firmware smooths in three stages — all optional
via the patch tool:

- an EMA on coordinates (0x08001B34)
- a 3-entry moving average on coordinates (0x080054F4)
- a 15-entry moving average on the raw coil window values (0x08003BE0)

The coil-window average is the one that damps the known EMR artifact
where the cursor jitters exactly between two coils (the window argmax
flips between two nearly-equal peaks); removing it makes that jitter
visible, keeping it while removing the other two leaves output positions
unfiltered but the flip damped.

## USB and flashing

While running: HID tablet, VID 256C:006D. To flash: the NuMicro LDROM
bootloader enumerates for ~2.5 s after replugging as 0416:3F00 and
speaks the vendor's ISP protocol over 64-byte HID packets — a fixed
handshake, an UPDATE-APROM command carrying the image, and a final ACK
containing a byte-sum of the image. The flash tool in this repo replays
exactly that session and checks every ACK. The bootloader itself is
never modified, so recovery is always possible: power-cycle, replug,
flash the stock image.

## Performance physics

Rate is bounded by real analog work, in this order:

- **excitation bursts** — 75–80% of every cycle (29 pulses ≈ 4 µs each,
  per measurement); the burst length is the only large remaining lever
- **settle waits** — the analog front-end genuinely needs them
  (independently confirmed by custom-firmware authors landing in the
  same µs range); pushing below ~1–7 µs produced visible stutter here
- ADC conversion ~1.7 µs per read, math and report logic ~0.1 ms

Stock: 21 measurements ≈ 4.33 ms ≈ 231 Hz. Patched: 15 measurements ≈
2.1 ms ≈ 470 Hz (a ~500 Hz variant exists). The stock-code ceiling is
roughly 700–800 Hz (needs a burst cut); USB full-speed itself caps at
1000 reports/s (one per 1 ms frame) — reaching that requires a
purpose-built acquisition cycle, not patching.

## Provenance and scope

All of the above was derived by static and dynamic analysis of the
firmware image the tablet itself serves through the vendor's own update
mechanism, plus live measurement on our own device. This file documents
behavior; it contains no firmware code and no vendor-owned content.
