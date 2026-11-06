#!/usr/bin/env python3
"""Read a NIIMBOT B4's RFID replies (0x1a and 0x59) over USB, for b4patch.py --fake-rfid.

Load a GENUINE roll of the size you want to fake (e.g. a real 100x150 roll), connect the
printer by USB, and run:

    python3 b4rfid_read.py                      # uses /dev/ttyACM0
    python3 b4rfid_read.py /dev/ttyACM1

It prints the two reply payloads as ready-to-paste b4patch arguments:

    --rfid-1a <hex>     your roll's identity + geometry (UID, barcode, serial, size, count)
    --rfid-59 <hex>     the 18-byte print-params reply (optional; a generic 100x150 one is built in)

These come from your own roll; the 0x1a reply in particular identifies that physical consumable,
so keep it to yourself. Needs: pip install pyserial. Linux: your user must be able to open the
port (group dialout, or an ACL). This script only sends read queries; it changes nothing.
"""
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("needs pyserial:  pip install pyserial")


def frame(cmd, data=b"\x01"):
    chk = cmd ^ len(data)
    for b in data:
        chk ^= b
    return bytes([0x55, 0x55, cmd, len(data)]) + data + bytes([chk, 0xAA, 0xAA])


def parse(buf):
    out = []
    while True:
        i = buf.find(b"\x55\x55")
        if i < 0 or len(buf) < i + 4:
            break
        n = buf[i + 3]
        if len(buf) < i + 4 + n + 3:
            break
        out.append((buf[i + 2], buf[i + 4:i + 4 + n]))
        buf = buf[i + 4 + n + 3:]
    return out


def ask(s, cmd, want, timeout=1.5):
    s.reset_input_buffer()
    s.write(frame(cmd))
    s.flush()
    buf = b""
    t = time.time()
    while time.time() - t < timeout:
        buf += s.read(256)
        for c, d in parse(buf):
            if c == want:
                return d
    return None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
    s = serial.Serial(port, 115200, timeout=0.1)
    time.sleep(0.2)

    r1a = ask(s, 0x1A, 0x1B)
    r59 = ask(s, 0x59, 0x69)

    if not r1a or len(r1a) < 20:
        sys.exit("0x1a returned no tag - is a GENUINE roll loaded and readable? (got: %s)"
                 % (r1a.hex() if r1a else "nothing"))
    print("0x1a reply (%d bytes): %s" % (len(r1a), r1a.hex()))
    if r59 and len(r59) == 18:
        print("0x59 reply (18 bytes): %s" % r59.hex())
    else:
        print("0x59 reply: %s (will use b4patch's built-in generic 100x150 profile)"
              % (r59.hex() if r59 else "none"))
    print()
    print("Pass to b4patch.py:")
    arg = "  --rfid-1a %s" % r1a.hex()
    if r59 and len(r59) == 18:
        arg += " --rfid-59 %s" % r59.hex()
    print(arg)


if __name__ == "__main__":
    main()
