# NIIMBOT B21 Pro: full heat on non-RFID labels

The B21 Pro reads an RFID tag on every label roll. If the tag is missing or unknown, the firmware
falls back to a minimal heat value and prints are barely visible. This repository contains a
firmware patch that removes that restriction, so third-party rolls print as dark as original ones.

Status: working, tested on a B21 Pro running patched 3.10 with original and no-name rolls.
The patcher supports firmware 3.09, 3.10, 3.11, 3.12 and 3.13.

## Quick start

1. Get an official image `B21_Pro_<version>.bin` (e.g. from the niim.blue firmware mirror,
   fw.niim.blue) and check it against the hashes below.
2. Patch it:
   ```sh
   python3 tools/b21patch.py B21_Pro_3.10.bin
   # -> B21_Pro_3.10_noprime10_crcfix.bin
   ```
3. Flash it with the firmware updater on [niim.blue](https://niim.blue) (USB or Bluetooth), or
   with the [B21Pro app](https://github.com/doctormord/B21Pro-Simple-Printing-and-Photobooth-App)
   over Bluetooth (long-press the title to open the debug console). Enter the version that matches
   the file. Keep the printer charged and connected until it reboots.

The patcher needs Python 3 and nothing else. `--verify` additionally disassembles the patched code
(requires `pip install capstone`).

## Gray mode: better 16-level grayscale

The B21 Pro can print 16 gray levels, but the stock heat curve is bunched up, so on third-party
paper only a handful of shades show. `tools/b21graypatch.py` rewrites the per-level heat
increments and the load compensation of the gray path. The 1-bit path is not touched.

```sh
pip install capstone
python3 tools/b21graypatch.py B21_Pro_3.10_noprime10_crcfix.bin --load 2
# -> B21_Pro_3.10_noprime10_crcfix_graycum_load200_crcfix.bin
```

- On 3.09 to 3.11 the default mode is RFID-aware: a genuine gray roll keeps the stock curve,
  a roll without a readable tag gets the new one. On 3.12 and 3.13 (no room for the code caves)
  the new curve applies to every roll.
- `--profile graycum` (default) gives a linear darkness ramp, `--load 2` is the setting that
  worked best on no-name paper. Details and the measurements behind them are in
  [docs/gray-mode.md](docs/gray-mode.md).
- `tools/b21_gray_editor.html` does the same in a browser, with a live chart of the curve:
  open the file locally, load an image, download the patched one. Nothing is uploaded.
- Both produce byte-identical files; the output keeps the 28-byte header and restores the CRC.

This repository never contains firmware images, neither official nor patched. Download the
official image yourself and patch it locally.

## How the throttle works

The relevant state lives in a roll structure in RAM (`0x2000176c` on 3.10):

| Field      | Meaning                                                  |
|------------|----------------------------------------------------------|
| `roll+9`   | "label valid", set to 1 only after a successful tag read |
| `roll+0x67`| heat density, copied from the tag (tag offset `0x53`)    |

The RFID handler (`FUN_0800e004`) reads the tag in a loop. On success it calls a normalizer
(`FUN_0800df8c`) that sets `roll+9 = 1` and fills in defaults. The print path then computes the
strobe energy as roughly `base × density × 11.81`, bounded by a cap derived from the printer's own
calibration. If `roll+9` is not 1, the energy is set to 0 and the strobe routine falls back to a
fixed minimum (`0xe`), which is the faint output you see on no-name rolls.

## The patch

Three changes, all located by byte signature so the same tool works across versions:

1. **Always valid.** The success check in the RFID handler is forced, so the normalizer runs for
   every roll. This is exactly what happens with a genuine tag, so the rest of the firmware sees
   a normal, valid roll.
   - 3.09–3.11: `cbz r5, <success>` → `b <success>`
   - 3.12–3.13: `cbnz r5, <fail>` → `nop`
2. **Default density.** Without a tag nothing writes `roll+0x67`. The call at the end of the
   normalizer is redirected through a 20-byte code cave in zero padding:
   
```asm
   ldrb.w r0, [r4, #0x67]
   cbnz   r0, 1f            ; tag provided a density: keep it
   movs   r0, #0x10         ; otherwise use the default
   strb.w r0, [r4, #0x67]
1: bl     <original callee>
   b.w    <back>
```

   Energy still goes through the stock formula and cap, so strobe timing stays consistent.
3. **CRC-neutral image.** See below.

| Version | Gate       | Detour     | Cave       | Body CRC32 |
|---------|------------|------------|------------|------------|
| 3.09    | `0800e064` | `0800e016` | `0801b8a8` | `3eac7a46` |
| 3.10    | `0800e024` | `0800dfd6` | `0801b868` | `28df9c6b` |
| 3.11    | `0800e004` | `0800dfb6` | `0801b8cc` | `81ace986` |
| 3.12    | `0800dc84` | `0800dd12` | `0801b2d8` | `bc2fc806` |
| 3.13    | `0800dc6c` | `0800dcfa` | `0801b3f4` | `103cf2bd` |

### Tuning

`--density` sets the default for rolls without a tag (default `0x10`). Higher is darker. The
result is still limited by the firmware's energy cap; values above it fall back to the faint
minimum instead of overheating. `0x10` gives results comparable to original rolls.

## Getting a modified image accepted

The `.bin` starts with a 28-byte little-endian header, followed by the image that is written to
`0x08000000`:

| Offset    | Content                                   |
|-----------|-------------------------------------------|
| `0x00`    | hash, custom algorithm                    |
| `0x04`    | body length (file size − 28)              |
| `0x08`    | `crc32(body)` (standard zlib CRC-32)      |
| `0x0c`    | zero                                      |
| `0x14`    | version                                   |
| `0x18`    | hash, custom algorithm                    |

The printer checks the image twice:

- right after receiving the header, a hash over the header that also covers `0x08`. Changing the
  CRC field gets the transfer rejected after the first chunk.
- after the last chunk, `crc32(received body) == header[0x08]`. A mismatch rejects the update
  (`0x9d 00`).

So a patched body needs a new CRC, but a new CRC breaks the header hash, and the two hash fields
did not match any standard CRC variant. Instead of cracking them, the patch keeps the header
byte-identical and makes the body's CRC unchanged. CRC-32 is affine over GF(2): flipping bits at a
fixed position changes the CRC by a fixed XOR pattern. With 4 free bytes, a 32×32 linear system
gives the exact values that restore the original CRC. The free bytes are the reserved Cortex-M
vector slot at `0x0800001c`, which is covered by the CRC but never used.

## Safety and recovery

The update is written to a staging area and only committed after both checks pass. A rejected
or interrupted transfer leaves the old firmware running. The only real risk is losing power
during the commit, so charge the printer first.

- **Back to stock:** flash the official image for the same or any other version. The printer also
  accepts downgrades.
- **Bricked:** the MCU is an STM32F103-compatible part, so it can be recovered over SWD
  (ST-Link, pads SWDIO/SWCLK/GND/3V3 on the board) or the ROM bootloader (BOOT0 high, UART on
  PA9/PA10). Strip the 28-byte header (`tail -c +29 B21_Pro_3.10.bin > body.bin`) and write the
  body to `0x08000000`. If readout protection is set, a full chip erase is needed first.

## Things that did not work

Documented so nobody has to repeat them:

- **Raising `movs r7, #0x1e`.** An earlier guess at the throttle. It is an overcurrent guard;
  higher values print the same or nothing at all.
- **Injecting energy or density directly** into the strobe structure (`0x2000c5e8 + 0x1c0`) or
  raising the `0xe` floor. Strobe timing is tightly coupled; single values give blank labels or
  a print that stops at 50 %.
- **Cloning tags.** The printer keeps roll records in its own flash (`0x0803f000`, `0x0803f800`)
  and matches serials.
- **Debug text commands over BLE** (`test get sensor`, `#10001`). The firmware has them, but they
  are not reachable on the BLE characteristic.
- **Reading the calibration.** The `0x55` protocol (USB or BLE) has `GET_RFID` (`0x1a`) and
  `GET_INFO` (`0x40`) but no memory read; the heat calibration is only accessible over SWD.

## B4: RFID enforcement off (experimental, untested on hardware)

`tools/b4patch.py` patches B4 firmware 1.16, 1.18 and 1.20. The B4 does not throttle heat;
instead a persistent config byte (`cfg+0x2f`, loaded from `0x0803f000`) makes it refuse to print
(status `0xfd`) when the label tag is missing or the roll's print counter is used up. It defaults to
on (erased flash reads as `0xff`, which the loader turns into 1) and the app can set it with
command `0x2f`. The patch makes the loader always store 0 and the `0x2f` handler never store 1.
Header and both section CRCs stay identical (CRC-neutral bytes in reserved vector slot 7 of the
main image).

```sh
python3 tools/b4patch.py B4_1.20.bin --verify   # -> B4_1.20_norfid_crcfix.bin
```

### Fake-RFID: making the official app accept non-genuine 100×150 paper

The `--norfid` patch only stops the *printer* refusing. Clients (the official NIIMBOT app and
niim.blue) do their own RFID handling, through two separate firmware queries, so `--fake-rfid`
replays a genuine 100×150 roll's reply on both no-tag branches:

- `0x1a` ("get RFID") — read for **recognition**; without it the app shows "Label paper not
  recognized". The fake replays the genuine 42-byte tag payload.
- `0x59` ("get RFID print params") — the **print** engine gates on this; a no-tag reply makes the
  print preflight fail with error `0x14` (niimbluelib `WriteRfidFail`). The fake replays the genuine
  18-byte params reply.

A third change suppresses one firmware error code: the print preflight (`cmd 0x01`) maps the
"RFID not valid" fault bit to error `0x14`; `--fake-rfid` makes that one mapping return OK so printing
proceeds. All of this — the two replies, the error code, and the fact that there is no RFID
write-back on the wire — was read from a `usbmon` capture of a real 100×150 print diffed against a
failing one. Genuine rolls are untouched (their valid flag is 1, so neither no-tag branch is taken).
`--fake-rfid` implies `--norfid`.

**You supply the tag identity.** The `0x1a` reply contains a roll's UID, barcode and serial, which
identify one physical consumable, so none is shipped here. Load a genuine 100×150 roll, capture its
replies, and pass them in:

```sh
pip install pyserial
python3 tools/b4rfid_read.py                 # genuine 100×150 roll loaded -> prints --rfid-1a <hex>
python3 tools/b4patch.py B4_1.20.bin --fake-rfid --rfid-1a <hex> --verify
#   -> B4_1.20_fakerfid_crcfix.bin
python3 tools/b4patch.py B4_1.20.bin --fake-rfid --rfid-1a <hex> --rfid-count 999   # more labels
```

The `0x59` print-params reply ships as a generic 100×150 profile (it has no UID/serial/barcode);
override it with `--rfid-59 <hex>` if your roll's geometry differs.

Caveats: the cave bytes are pre-assembled and position-specific, so this mode is keyed by the
main-image SHA-256 and currently only has the **1.20** cave. The app tracks label usage per tag UID,
so a fixed identity may eventually hit the app's own label-count limit (`--rfid-count` sets the
reported total). Status: **experimental.** Confirmed on hardware to make the printer report the roll
and to print from niim.blue; the remaining open issue is gap registration — a transmissive gap
sensor cannot see the gap on paper with an opaque liner, so die-cut third-party rolls may need a
continuous-mode / fixed-length workflow instead. The `--norfid` patch above has no such caveat.

Differences from the B21 Pro: the main image is linked at `0x08002000` behind an 8 KB bootloader,
the file carries a second (aux) image with its own CRC, the header CRC at `0x18` is
`crc32({0x18,0,0,0} + header[4:0x18])`, and the updater refuses versions older than the running
one, so recovery means flashing the official image of the same version (or newer).

| Version | SHA-256 (official)                                                 | Loader     | Setter     |
|---------|--------------------------------------------------------------------|------------|------------|
| 1.16    | `6afb975a5bc0acbd493542b452b28f812fd1b5e7c0966e237430d0e5845b0f10` | `08020682` | `08017516` |
| 1.18    | `ed077ece62f7129ece1a97674b8fa4e891a83124d40f292c5253fdac51739f9a` | `080206a2` | `0801753a` |
| 1.20    | `18926fc9476cd70f7aa0da64c40aabebdd64b2a384b30bacc69d8176c5b823e5` | `08020896` | `0801758e` |

## Firmware hashes (SHA-256)

Official:

```
01be2c2885a2e214fb1c63fdb417a896a9aa6969c822a99e2c0c4978ec724a50  B21_Pro_3.09.bin
0e44405a72138ee21897df624dd93e03d2617ef1d94af58cb0889c44f4d10e2e  B21_Pro_3.10.bin
bf2405c96f0e0c137ef01f9b469309076792410e17e1ed82b88efd751ee61c0f  B21_Pro_3.11.bin
5a9bd9ca70bf38561d26a9daca986f5e4b9e2c5ce16d8ee24a4baf5e22945cb5  B21_Pro_3.12.bin
b0f4c40f4c0071a0addead4d862b4fc542121ce76fabf9f15e55258e792de1b6  B21_Pro_3.13.bin
```

## Repository

| Path                 | Content                                                        |
|----------------------|----------------------------------------------------------------|
| `tools/b21patch.py`  | the full-heat patcher (non-RFID rolls)                         |
| `tools/b21graypatch.py` | gray-mode patcher (curve, load term, RFID-aware caves)      |
| `tools/b21_gray_editor.html` | the same as a browser tool with a live curve chart    |
| `tools/b4patch.py`   | B4: disables RFID enforcement (`--norfid`) and optional fake-RFID for the app (`--fake-rfid`) |
| `tools/b4rfid_read.py` | B4: read a genuine roll's RFID replies over USB (for `--fake-rfid`) |
| `docs/gray-mode.md`  | how gray works in the firmware, variants, measurements        |
| `history.md`         | chronological log of the analysis, including the dead ends    |
| `rea.md`             | reverse-engineering setup: Ghidra on Apple Silicon, headless import, tooling |

## Credits

- [niimbluelib / niim.blue](https://github.com/MultiMote/niimbluelib) by MultiMote for the
  documented NIIMBOT protocol and the firmware updater.
- Ghidra and capstone.

## Disclaimer

For printers you own. Flashing modified firmware voids the warranty and can damage the device.
Printing with more heat on unknown label stock wears the print head faster. Not affiliated with
NIIMBOT.
