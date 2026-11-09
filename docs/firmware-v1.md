# B21 Pro "tune v1" firmware (3.10, ported to 3.09-3.13)

Tune v1 combines the full-heat patch, the gray curve and two new controls that the app sets per print job:
the gray load and the 1-bit heat. `tools/make_firmware.py` builds it from an official image.
On 3.10 the result is byte-identical to the image printed in the paper tests
(sha256 `b90a299f2b9ad40a62c0308d9875f0f5575a528ac9852b12ddb8c814db75a65c`). **None of this is a NIIMBOT release.**

## Build
```sh
python3 tools/make_firmware.py                          # guided: file, then choose patches or "all"
python3 tools/make_firmware.py B21_Pro_3.10.bin --all   # -> B21_Pro_3.10_tune-v1.bin
python3 tools/make_firmware.py B21_Pro_3.10.bin --patches unlock,curve,load
```
- Standard library only, no capstone, no assembler. The script holds the byte patches and checks every original
  byte before writing. The header stays unchanged and the CRC word is solved (see README).
- Input must be an official image of 3.09 to 3.13. The version is recognised by SHA-256 (hashes in the README).
- Patches: `unlock` (rolls without a tag print), `curve` (graycum), `load` (gray load %), `heat` (1-bit heat %).
  `load` and `heat` bring the print-start cave along.
- Results equal to the existing tools on 3.10: `unlock` = `b21patch.py` (noprime10), `unlock,curve` =
  `b21graypatch.py --profile graycum --load 1` (graycum). Other combinations use the same bytes but were not printed as such.
- Flash as described in the README. Keep the official image so you can flash back.

## What it changes (3.10 addresses)
| Part | Effect | Where | Acts on |
|---|---|---|---|
| unlock (noprime10) | Rolls without a readable RFID tag print. Tag check forced to success, `roll+9 = 1`; default density `roll+0x67 = 0x10` if empty | gate `0x800e024`, cave0 `0x801b868` via `0x800dfd6` | all modes |
| curve (graycum, RFID-aware) | Gray increments c1 = 160, c2..c14 = 34 (linear darkness) for rolls whose tag type is not 2. A genuine gray roll keeps the stock curve | cave1 `0x801b87c` via `0x800e014`, cave2 `0x801d020` via `0x800bad6` | gray |
| gray load % | Load compensation × data[8] % (planes 1..15) and × data[9] % (plane 0) | `cave_load` `0x801b894`, called from `0x801259a` and `0x80137c0` instead of the load-term function | gray |
| 1-bit heat % | Every 1-bit strobe pulse × data[10] %, clamped 1..1999 µs | `cave_arr` `0x801bb84`, called instead of the timer-reload function at six sites | 1-bit (colour 0 and 2) |
| print-start bytes | data[8], data[9], data[10] stored per job, 0 if the frame is shorter | `cave_ps` `0x801bf10`, called from both print-start parse paths `0x80063fa` and `0x8006496` | all |

Unchanged: header, row period, motor drive, temperature and voltage terms, red/black (colour 1), protection.

The caves use three RAM bytes that stock firmware never references and that are rewritten at every print
start: `0x20000749` data[8], `0x20000747` data[9], `0x2000074d` data[10].

## Print start (command `0x01`)
`00 01 00 00 00 00 <mode> <d7> <load> <load0> <heat1bit>` (11 data bytes)

| Byte | Gray (mode 3) | 1-bit (mode 0 or 2) |
|---|---|---|
| data[6] mode | 3 | 0 (2: other paper table) |
| data[7] | no effect in gray; send 1 | send 1. With 0 every row is struck twice, half a step apart, and dots smear |
| data[8] load | planes 1..15 load in % of stock | ignored |
| data[9] load0 | plane 0 load in % of stock | ignored |
| data[10] heat1bit | ignored | every pulse in % of stock |

0 or a missing byte means 100 % (stock). The official app's 9-byte start therefore prints like stock firmware with the
new curve. Density (command `0x21`, D1..D5) still works and multiplies with heat1bit.

Known settings:
| Paper | Mode | Setting | Result |
|---|---|---|---|
| 40×40 no-name, no tag | gray | load 25, load0 50 | correct and balanced (by eye) |
| 50×30 gloss | gray | load 200, load0 100 (= `graycum_load200`) | best balance in the October tests; a 2-row grid remains |
| 40×40 no-name | 1-bit | D1, heat1bit to be calibrated | stock D1 is too hot (all black from about 45 % coverage) |

Stock 3.10 reads only data[6] and data[7] of the print start and has no length check, so an app can send the
11-byte form to any printer (read in code, not tested on an unflashed printer). An app should still use the
official 9-byte form `00 01 00 00 00 00 <mode> <d7> 00` for printers that are not patched.

## Rolls and modes
| Roll | Tag type | Gray curve (3.09-3.11) | 1-bit base heat |
|---|---|---|---|
| Genuine NIIMBOT gray roll | 2 | stock | from the tag |
| Other genuine roll | 1 etc. | graycum | from the tag |
| No-name without tag | 0 | graycum | default heat (density 0x10), T and V terms |
| No-name with a foreign tag | that tag's | by type | by tag |

## Verification
- Gray load emulated against the fixed `load50`/`load150`/`load200` builds: 192 half-planes × 16 planes, max 1 µs difference.
- 1-bit sites emulated: 5 pulse values × 7 percentages each; gray and red/black sites unchanged.
- Print start emulated with frame lengths 7 to 11 on both parse paths.
- Paper (3.10): gray load per job works. 1-bit heat calibration per paper is still open.

## Limits
- 1-bit: above about 140 % the longest pulses hit the 1999 µs clamp.
- Gray: the 2-row grid on 50×30 gloss and vertical lines in gray are not solved by v1.
- Only 3.10 was printed on paper.

## Other versions
`make_firmware.py` holds one patch table per version. **Only 3.10 is paper-tested. The others were checked
in emulation and never printed.**

| Version | Print-start sites | Load sites | 1-bit sites | Caves load/ps/arr | RAM data[9]/[8]/[10] | `all` sha256 |
|---|---|---|---|---|---|---|
| 3.09 | `0x8006436`, `0x80064d2` | `0x80125da`, `0x8013800` | 6 | `0x801b8d4`, `0x801bf50`, `0x801bbc4` | `0x20000747/749/74d` | `2b73527bad5a63fd…` |
| 3.10 | `0x80063fa`, `0x8006496` | `0x801259a`, `0x80137c0` | 6 | `0x801b894`, `0x801bf10`, `0x801bb84` | `0x20000747/749/74d` | `b90a299f2b9ad40a…` |
| 3.11 | `0x80064d2`, `0x800656e` | `0x80126de`, `0x801395a` | 6 | `0x801b8f8`, `0x801bf74`, `0x801bbe8` | `0x20000747/749/74d` | `85a9af30412a11b3…` |
| 3.12 | `0x80065be`, `0x8006664` | `0x80123ee`, `0x8013370` | 3 | `0x801b2ee`, `0x801b97e`, `0x801b5f2` | `0x2000074f/751/755` | `1fcc1988c92ec0b6…` |
| 3.13 | see `make_firmware.py` | `0x80123d6`, `0x8013358` | 3 | `0x801b40a`, `0x801ba9a`, `0x801b70e` | `0x20000757/759/75d` | `03b15d2841072c73…` |

What differs in 3.12 and 3.13:
- **curve:** there is too little contiguous free space for the roll check, so the curve is written into the
  constants and applies to **all rolls, genuine gray rolls too**. Genuine gray paper then no longer prints
  with the stock curve.
- **unlock:** the gate is `cbnz r5` → `nop` instead of `cbz` → `b`.
- The print-start handler keeps the job struct in a different register; the cave is assembled for it.
- One of the 1-bit strobe callbacks does not exist, and colour 0 and 2 share one row start, so there are
  3 instead of 6 1-bit sites.

Checked per version in emulation: `unlock` and `unlock,curve` byte-identical to `b21patch.py` and
`b21graypatch.py`; header and CRC; gray load against that version's `load50`/`load150`/`load200` builds
(max 1 µs); print-start lengths 7 to 11 at both sites; every 1-bit site × 5 pulse values × 7 percentages.
Not checked outside 3.10: paper, a full print job, and indexed access to the three RAM bytes (there are no
direct references in any version).
