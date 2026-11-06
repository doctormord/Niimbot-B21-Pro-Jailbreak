#!/usr/bin/env python3
"""Patch official NIIMBOT B4 firmware so the printer never enforces RFID (genuine-label) checks.

Usage:
    python3 b4patch.py B4_1.20.bin                 # writes B4_1.20_norfid_crcfix.bin
    python3 b4patch.py B4_1.20.bin -o out.bin
    python3 b4patch.py B4_1.20.bin --verify        # also disassembles the patch (needs capstone)
    python3 b4patch.py B4_1.20.bin --check         # only validate the image, write nothing

Tested on 1.16, 1.18 and 1.20. Standard library only.

How the check works (addresses for 1.20, other versions are found by byte signature):

The firmware keeps a persistent config block in RAM at 0x2000176b, loaded at boot from
internal flash 0x0803f000. Byte +0x2f of that block is an "enforce RFID" flag:

  - boot loader 0x080206e4: if the stored byte is 0xff (erased flash) it becomes 1, i.e.
    enforcement is ON by default;
  - protocol command 0x2f (handler 0x08017548) sets it to payload[0] (0 or 1),
    command 0x57 reads it back;
  - ten readers all test `flag == 1`. When set, printing is refused with status 0xfd if the
    label tag is missing/unreadable, or if the roll's print counter exceeds its limit; the
    ribbon counter and one status error bit are gated the same way.

With the flag at 0 the stock firmware simply skips those checks - the code path the
printer already uses when the config says "off". This patch forces that state:

1. Loader: `if (cfg[0x2f] == 0xff) cfg[0x2f] = 1`  ->  `cfg[0x2f] = 0`  (every boot)
2. Setter: payload 1 stores 0 instead of 1, so the app cannot switch it back on.
3. CRC-neutral: the header is left byte-identical and 4 bytes in the unused reserved
   Cortex-M vector slot 7 of the main image (0x0800201C) are solved so crc32(main) stays
   equal to the header CRC. The second (aux) image is not touched.

Nothing related to heat or label geometry is changed. Without a tag the stock firmware
falls back to its gap sensor and default label length, exactly as it does today.

Image layout (28-byte little-endian header, then main image, then aux image):
  0x00  u8 header length (0x18) + 3 bytes not covered by the header CRC
  0x04  main image length          0x08  crc32(main)
  0x0c  aux image length           0x10  crc32(aux)
  0x14  u16 version (0x78 = 1.20)  0x16  u16 model (0x1a00 = B4)
  0x18  crc32({0x18,0,0,0} + header[0x04:0x18])
The main image is linked at 0x08002000 (an 8 KB bootloader sits in front of it).
"""
import argparse
import struct
import sys
import zlib

HEADER = 0x1C
BASE = 0x08002000            # main image load address
MODEL = 0x1A00
COMPENSATE_AT = 0x1C         # reserved vector slot 7 of the main image, never executed

# Loader: ldrb.w r0,[r4,#0x2f] ; cmp r0,#0xff ; bne +2 ; strb.w r8,[r4,#0x2f] ; pop.w {r4-r10,pc}
LOADER_SIG = bytes.fromhex("94f82f00ff2801d184f82f80bde8f087")
LOADER_NEW = bytes.fromhex("0020" "84f82f00" "00bf00bf00bf")   # movs r0,#0 ; strb.w r0,[r4,#0x2f] ; nop x3

# Setter (cmd 0x2f): ... strb.w r4,[r0,#0x2f] ; b ; strb.w r3,[r0,#0x2f] ; b
SETTER_SIG = bytes.fromhex("3f238df8033001238df80530")              # reply-buffer setup, unique
SETTER_TAIL = bytes.fromhex("80f82f40f2e780f82f30efe7")
SETTER_FROM, SETTER_TO = bytes.fromhex("80f82f30"), bytes.fromhex("80f82f40")  # strb r3 -> strb r4 (r4 == 0)

KNOWN = {
    "6afb975a5bc0acbd493542b452b28f812fd1b5e7c0966e237430d0e5845b0f10": "1.16",
    "ed077ece62f7129ece1a97674b8fa4e891a83124d40f292c5253fdac51739f9a": "1.18",
    "18926fc9476cd70f7aa0da64c40aabebdd64b2a384b30bacc69d8176c5b823e5": "1.20",
}

# --- Fake-RFID mode -------------------------------------------------------
# Two firmware queries matter when there is no tag:
#   0x1a ("get RFID") -> normally "0x1b 00" (no roll). The official NIIMBOT app
#        reads this for recognition; without it the app shows "Label paper not
#        recognized".
#   0x59 ("get RFID print params") -> normally the 4-byte no-tag reply
#        "0x69 00 18 00 18". The PRINT engine gates on this; a no-tag reply makes
#        the print preflight fail with error 0x14 (niimbluelib "WriteRfidFail").
# A USB capture of a genuine 100x150 print shows both: 0x1a returns the 42-byte
# tag payload and 0x59 returns an 18-byte "0x69" params reply; there is no extra
# RFID write-back on the wire. So this mode redirects BOTH no-tag branches into
# code caves that replay the genuine roll's exact replies. Genuine rolls are
# untouched (their valid flag is 1, so neither branch is taken).
#
# Cave bytes are pre-assembled (Thumb) and position-specific, so this mode is
# tied to one exact main-image build, keyed by the main-image SHA-256. Each cave:
#   movs r0,#N ; strb.w r0,[sp,#4] ; add r0,sp,#5 ; adr r1,blob ; movs r2,#N
#   loop: ldrb r3,[r1],#1 ; strb r3,[r0],#1 ; subs r2,#1 ; bne loop ; b.w <send>
#   blob: <reply payload>   (N = len(blob); byte 0 and byte 12 of cave_code hold N)
#
# NO tag identity is embedded here. The 0x1a reply carries a roll's UID, barcode
# and serial, which identify one physical consumable; shipping someone's would let
# it be traced or cloned. So YOU supply your own genuine 100x150 roll's 0x1a reply
# (--rfid-1a, captured with tools/b4rfid_read.py). The 0x59 blob below is only
# label geometry/counts for a 100x150 roll (no UID/serial/barcode), so it ships as
# a default and can be overridden with --rfid-59.
FAKE_RFID = {
    # main-image sha256 (body[0x1c : 0x1c+len_a]) -> patch record
    "289ab412a4dcf5aa2e18b2e23811000e191861a66162dac8a49033d3c47b9469": dict(
        ver="1.20",
        lenpos=(0, 12),   # byte offsets in cave_code of the two "movs #N" length immediates
        # 0x1a get-RFID no-tag branch (0x8013b88) -> cave @0x803b652, send via 0x8013b72.
        # The 0x1a reply (your --rfid-1a) is written at blob1a; up to blob1a_max bytes.
        hook1a=0x11B88, hook1a_orig=bytes.fromhex("01208df8"), hook1a_new=bytes.fromhex("27f063bd"),
        cave1a=0x39652,
        cave1a_code=bytes.fromhex("2a208df804000df1050004a12a2211f8013b00f8013b521ef9d1d8f781ba"),
        blob1a=0x39670, blob1a_max=50,
        # 0x59 print-params no-tag branch (0x8020c70) -> cave @0x803b6a2, send via 0x8020c82
        hook59=0x1EC70, hook59_orig=bytes.fromhex("18208df8"), hook59_new=bytes.fromhex("1af017bd"),
        cave59=0x396A2,
        cave59_code=bytes.fromhex("12208df804000df1050004a1122211f8013b00f8013b521ef9d1e5f7e1ba"),
        blob59_default=bytes.fromhex("003004e001003c0618032003e80000000000"),  # generic 100x150 params
        # Error-code map (0x8020f8c): bit 19 of the fault word = "RFID not valid" -> code 0x14
        # (niimbluelib "WriteRfidFail", which blocks the print preflight at cmd 0x01). Make that
        # one mapping return 0 (OK) instead of 0x14; every other fault keeps its own code.
        errmap=0x1F038, errmap_orig=bytes.fromhex("1420"), errmap_new=bytes.fromhex("0020"),
    ),
}


def find_one(buf, sig, what):
    i = buf.find(sig)
    if i < 0 or buf.find(sig, i + 1) >= 0:
        sys.exit(f"{what}: signature {'not found' if i < 0 else 'not unique'} - unsupported firmware?")
    return i


def header_crc(hdr):
    return zlib.crc32(bytes([hdr[0], 0, 0, 0]) + hdr[4:0x18])


def parse(fw):
    if len(fw) < HEADER:
        sys.exit("file too small")
    hdr = fw[:HEADER]
    _, len_a, crc_a, len_b, crc_b, ver, model, hcrc = struct.unpack_from("<IIIIIHHI", hdr)
    if hdr[0] != 0x18 or model != MODEL:
        sys.exit(f"not a B4 image (header byte {hdr[0]:#x}, model {model:#x})")
    if HEADER + len_a + len_b != len(fw):
        sys.exit("header lengths do not match the file size")
    if header_crc(hdr) != hcrc:
        sys.exit("header CRC mismatch - corrupt image?")
    main = fw[HEADER:HEADER + len_a]
    aux = fw[HEADER + len_a:]
    if zlib.crc32(main) != crc_a:
        sys.exit("main image CRC does not match header - already patched or corrupt?")
    if zlib.crc32(aux) != crc_b:
        sys.exit("aux image CRC does not match header - corrupt image?")
    return hdr, bytearray(main), aux, crc_a, ver


def crc_compensate(buf, at, want):
    """Choose buf[at:at+4] so that crc32(buf) == want. CRC32 is affine over GF(2)."""
    buf[at:at + 4] = b"\0\0\0\0"
    base = zlib.crc32(buf)
    cols = []
    for bit in range(32):
        buf[at + bit // 8] = 1 << (bit % 8)
        cols.append(zlib.crc32(buf) ^ base)
        buf[at + bit // 8] = 0
    pivots = {}
    for i in range(32):
        v, m = cols[i], 1 << i
        for p in sorted(pivots, reverse=True):
            if v >> p & 1:
                v ^= pivots[p][0]
                m ^= pivots[p][1]
        if v:
            pivots[v.bit_length() - 1] = (v, m)
    target, sol = want ^ base, 0
    for p in sorted(pivots, reverse=True):
        if target >> p & 1:
            target ^= pivots[p][0]
            sol ^= pivots[p][1]
    if target:
        sys.exit("CRC compensation failed")
    buf[at:at + 4] = sol.to_bytes(4, "little")
    assert zlib.crc32(buf) == want


def patch(fw, fake_rfid=False, rfid_count=None, rfid_1a=None, rfid_59=None):
    import hashlib
    hdr, main, aux, crc_a, ver = parse(fw)

    # Sanity: reset handler must be the Keil stub at 0x08002374, i.e. the main image really
    # is linked at 0x08002000 and slot 7 is unused.
    sp, reset = struct.unpack_from("<II", main, 0)
    if reset != 0x08002375:
        sys.exit("unexpected reset vector - unsupported firmware?")
    if main[COMPENSATE_AT:COMPENSATE_AT + 16] != b"\0" * 16:
        sys.exit("reserved vector slots are not empty - already patched?")

    main_sha = hashlib.sha256(main).hexdigest()   # pristine, before any edit

    loader = find_one(main, LOADER_SIG, "config loader")
    main[loader:loader + len(LOADER_NEW)] = LOADER_NEW

    s = find_one(main, SETTER_SIG, "cmd 0x2f handler")
    tail = main.find(SETTER_TAIL, s, s + 0x60)
    if tail < 0:
        sys.exit("cmd 0x2f handler: unexpected store sequence")
    setter = tail + 6
    assert main[setter:setter + 4] == SETTER_FROM
    main[setter:setter + 4] = SETTER_TO

    info = dict(ver=ver, loader=loader, setter=setter, fake=None)

    if fake_rfid:
        rec = FAKE_RFID.get(main_sha)
        if rec is None:
            sys.exit("--fake-rfid: no pre-assembled cave for this exact firmware build "
                     f"(main sha256 {main_sha[:16]}…). Only tested builds are supported.")
        if rfid_1a is None:
            sys.exit("--fake-rfid needs YOUR genuine 100x150 roll's 0x1a reply via --rfid-1a <hex>.\n"
                     "Capture it with:  python3 tools/b4rfid_read.py   (genuine roll loaded)")
        blob1 = bytearray(rfid_1a)
        lo, hi = 20, rec["blob1a_max"]
        if not lo <= len(blob1) <= hi:
            sys.exit(f"--rfid-1a must be {lo}..{hi} bytes (got {len(blob1)})")
        if rfid_count is not None:
            if not 1 <= rfid_count <= 0xFFFF:
                sys.exit("--rfid-count must be 1..65535")
            blob1[-2:] = struct.pack(">H", rfid_count)   # 0x1a [0x68] label count, big-endian
        blob59 = bytes(rfid_59) if rfid_59 is not None else rec["blob59_default"]
        if len(blob59) != 18:
            sys.exit("--rfid-59 must be exactly 18 bytes")
        p0, p1 = rec["lenpos"]
        c1 = bytearray(rec["cave1a_code"]); c1[p0] = c1[p1] = len(blob1)   # movs #N immediates
        # Each inject: (name, hook, hook_orig, hook_new, [(offset, bytes), ...] writes)
        injects = [
            ("0x1a", rec["hook1a"], rec["hook1a_orig"], rec["hook1a_new"],
             [(rec["cave1a"], bytes(c1)), (rec["blob1a"], bytes(blob1))]),
            ("0x59", rec["hook59"], rec["hook59_orig"], rec["hook59_new"],
             [(rec["cave59"], rec["cave59_code"]), (rec["cave59"] + len(rec["cave59_code"]), blob59)]),
        ]
        for name, hk, orig, new, writes in injects:
            if main[hk:hk + 4] != orig:
                sys.exit(f"--fake-rfid: unexpected bytes at the {name} no-tag branch")
            for off, data in writes:
                if any(main[off:off + len(data)]):
                    sys.exit(f"--fake-rfid: {name} code cave is not empty - already patched?")
            main[hk:hk + 4] = new
            for off, data in writes:
                main[off:off + len(data)] = data
        # Suppress the RFID "error 20" so the print preflight (cmd 0x01) proceeds.
        em = rec["errmap"]
        if main[em:em + 2] != rec["errmap_orig"]:
            sys.exit("--fake-rfid: unexpected bytes at the error-code map")
        main[em:em + 2] = rec["errmap_new"]
        info["fake"] = dict(hook=rec["hook1a"], cave=rec["cave1a"], hook59=rec["hook59"], cave59=rec["cave59"],
                            errmap=em, count=struct.unpack(">H", blob1[-2:])[0])

    crc_compensate(main, COMPENSATE_AT, crc_a)
    out = hdr + bytes(main) + aux
    # re-validate exactly like the printer does (header CRC, both section CRCs)
    parse(out)
    info["comp"] = main[COMPENSATE_AT:COMPENSATE_AT + 4].hex()
    return out, info


def disasm(buf, at, n):
    try:
        import capstone
    except ImportError:
        print("    (install capstone to disassemble: pip install capstone)")
        return
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    for ins in md.disasm(bytes(buf[at:at + n]), BASE + at):
        print(f"    {ins.address:08x}  {ins.bytes.hex():<10} {ins.mnemonic} {ins.op_str}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("firmware", help="official B4_<ver>.bin")
    ap.add_argument("-o", "--output")
    ap.add_argument("--fake-rfid", action="store_true",
                    help="also make the printer report a 100x150 roll when no tag is present, so the "
                         "official app recognises non-genuine 100x150 paper (implies the norfid patch; "
                         "experimental, 1.20 only; needs --rfid-1a)")
    hexbytes = lambda s: bytes.fromhex(s.replace(" ", "").replace(":", ""))
    ap.add_argument("--rfid-1a", type=hexbytes, default=None,
                    help="with --fake-rfid: hex of YOUR genuine 100x150 roll's 0x1a reply data "
                         "(capture with tools/b4rfid_read.py). Not shipped - it identifies a physical roll.")
    ap.add_argument("--rfid-59", type=hexbytes, default=None,
                    help="with --fake-rfid: hex of the 18-byte 0x59 print-params reply (default: a generic "
                         "100x150 profile; override if your roll's geometry differs)")
    ap.add_argument("--rfid-count", type=lambda s: int(s, 0), default=None,
                    help="with --fake-rfid: label count reported to the app (default: whatever --rfid-1a carries)")
    ap.add_argument("--verify", action="store_true", help="disassemble patched code (needs capstone)")
    ap.add_argument("--check", action="store_true", help="only validate the input image")
    a = ap.parse_args()
    if (a.rfid_count is not None or a.rfid_1a is not None or a.rfid_59 is not None) and not a.fake_rfid:
        sys.exit("--rfid-1a/--rfid-59/--rfid-count only apply with --fake-rfid")

    fw = open(a.firmware, "rb").read()
    import hashlib
    sha = hashlib.sha256(fw).hexdigest()
    known = KNOWN.get(sha)
    print(f"input   {a.firmware}  sha256 {sha[:16]}…  {'official ' + known if known else 'UNKNOWN image (not a tested official release)'}")
    if a.check:
        _, _, _, _, ver = parse(fw)
        print(f"ok      header CRC, main CRC, aux CRC valid; version byte {ver & 0xff:#x}")
        return

    out, info = patch(fw, fake_rfid=a.fake_rfid, rfid_count=a.rfid_count,
                      rfid_1a=a.rfid_1a, rfid_59=a.rfid_59)
    suffix = "_fakerfid_crcfix.bin" if a.fake_rfid else "_norfid_crcfix.bin"
    name = a.output or a.firmware.rsplit(".", 1)[0] + suffix
    open(name, "wb").write(out)

    print(f"loader  {BASE + info['loader']:08x}  cfg[0x2f] forced to 0 at boot")
    print(f"setter  {BASE + info['setter']:08x}  cmd 0x2f payload 1 stores 0")
    if info["fake"]:
        f = info["fake"]
        print(f"fake    0x1a {BASE + f['hook']:08x}->{BASE + f['cave']:08x}, "
              f"0x59 {BASE + f['hook59']:08x}->{BASE + f['cave59']:08x}; "
              f"no-tag -> genuine 100x150 roll, count {f['count']}")
    print(f"crcfix  {BASE + COMPENSATE_AT:08x}  {info['comp']} (reserved vector slot 7)")
    print(f"crc32   main {zlib.crc32(out[HEADER:HEADER + struct.unpack_from('<I', out, 4)[0]]):08x} (unchanged), "
          f"aux unchanged, header byte-identical")
    print(f"wrote   {name}")
    if a.verify:
        main_img = out[HEADER:]
        print("  loader:")
        disasm(main_img, info["loader"] - 8, 8 + len(LOADER_NEW) + 4)
        print("  setter:")
        disasm(main_img, info["setter"] - 6, 14)
        if info["fake"]:
            for tag, hk, cv, n in (("0x1a", info["fake"]["hook"], info["fake"]["cave"], 30),
                                   ("0x59", info["fake"]["hook59"], info["fake"]["cave59"], 30)):
                print(f"  fake {tag} hook:"); disasm(main_img, hk, 4)
                print(f"  fake {tag} cave:"); disasm(main_img, cv, n)


if __name__ == "__main__":
    main()
