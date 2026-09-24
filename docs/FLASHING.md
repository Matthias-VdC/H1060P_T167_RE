# T167 flashing — the NuMicro LDROM protocol

How the H1060P (board T167) is flashed, and why it is recoverable.
This documents the protocol the vendor's own update tool uses; the
flash command in this repo replays it exactly (verified byte-identical
against a captured official session, 459/459 packets).

## Two USB identities

| State | USB id | What it is |
|---|---|---|
| Running tablet | 256C:006D | normal HID pen tablet |
| **Bootloader (LDROM)** | **0416:3F00** | Nuvoton ISP mode, appears ~2.5 s after power-up |

The MCU's APROM (the part we flash) sits at 0x08000000; the LDROM
bootloader lives in a separate boot region and runs first after every
power-up. If a valid APROM is present it hands over after a short
window — during which it enumerates as 0416:3F00 and accepts ISP
commands. The LDROM is never written by anything we do, so it is
always there: flash, unplug, replug, flash again.

**Practical consequence:** there is no brick state from a bad image.
The worst case is a partially-written or wrong APROM, which the
bootloader still lets you replace at the next replug.

## Transport

HID class device, interface 0, interrupt endpoints: 0x02 (OUT) and
0x81 (IN), 64-byte packets. On Linux the kernel HID driver must be
detached from the interface (the tool does this); on Windows the
device must be bound to WinUSB instead (see README).

## Packet format

All packets are 64 bytes, little-endian 32-bit fields:

| Offset | Field |
|---|---|
| 0–3 | command / echo value |
| 4–7 | packet number |
| 8–63 | command payload |

Every OUT packet is answered with exactly one IN packet (the ACK).
The ACK's packet number at [4:8] is how the exchange is validated.

## Session

### 1. Handshake

The vendor tool opens with a fixed seven-packet exchange (commands
AE, A4, A6, B1, A2, A1, then A4 again — the tool restarts its packet
counter there, so the last handshake packet reuses packet number 1).
The A1 packet carries a device-capabilities payload. The bootloader
ACKs each with predictable packet numbers:

```
OUT pkt:  1  1  3  5  7  9  1(resync)
ACK pkt: 2  2  4  6  8 10  2
```

### 2. Update APROM — begin

One packet starts the transfer:

| Offset | Value |
|---|---|
| 0–3 | 0xA0 (UPDATE_APROM) |
| 4–7 | 3 (packet number) |
| 8–11 | 0 |
| 12–15 | image size in bytes (25304) |
| 16–63 | first 48 bytes of the image |

### 3. Image data

The rest of the image streams in 56-byte chunks at offset 8, with
packet numbers counting odd: 5, 7, 9, … (each data packet uses the
next odd number). For the 25304-byte image that is 451 data packets.

Packet accounting for a full session: 7 handshake + 1 begin + 451
data = **459 packets**, each ACK-checked.

### 4. Final ACK

After the last data packet the final ACK carries the bootloader's
verification of the whole image:

- bytes 8–9: **16-bit little-endian byte-sum of the image**
- bytes 10–11: the constant `1F F8`

If the byte-sum does not match the image you sent, the write was not
confirmed — treat it as a failed flash and reflash.

## Timing and flow of a real flash

1. Start the tool; it waits for 0416:3F00 to appear.
2. **Unplug and replug the tablet** — the bootloader enumerates for
   its ~2.5 s window.
3. The tool sends all 459 packets, validating every ACK (a desync
   aborts the session immediately).
4. ~1.3–2 s later the final ACK confirms the byte-sum.
5. **Unplug and replug once more** to boot the new firmware.

## Failure handling

- **ACK desync mid-session:** the transfer aborts; the APROM may be
  partially written. Replug and flash the stock image — the bootloader
  does not care that the APROM is broken.
- **Final ACK mismatch:** the image was written but not confirmed.
  Reflash.
- **Wrong image size:** refused before anything is sent (the APROM
  layout expects exactly 25304 bytes).

## Scope

This describes the protocol as observed from the device we own; the
command values are published as protocol facts. No vendor tool code
is included or required.
