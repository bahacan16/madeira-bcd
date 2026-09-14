#!/usr/bin/env python3
"""Count x18 references in a PE's .text -- the instructions wine must patch.

iOS reserves x18; Windows ARM64EC uses it for the TEB. So virtual_ios.c's
ios_jit_patch_x18 scans .text for every instruction that names x18 and
rewrites it as a branch to a generated trampoline that reads the TEB from
TPIDR_EL0 instead.

That trampoline is where our self-built xtajit64.dll dies (tramp+0x64c,
c0000005, storing x16 to a stack it cannot write) and where the committed
DLL never goes -- the committed log has no tramp store at all. So the
count itself is the measurement: if ours has x18 references the committed
one lacks, the trampoline path is ours alone and the fix is upstream of it.

The classifier mirrors ios_insn_x18_role in virtual_ios.c exactly.
"""
import struct, sys

NONE, RN, RM, RT2 = 0, 1, 2, 3

def x18_role(insn):
    rn = (insn >> 5) & 0x1f
    rm = (insn >> 16) & 0x1f
    rt2 = (insn >> 10) & 0x1f
    if rn != 18 and rm != 18 and rt2 != 18:
        return NONE
    top8 = insn >> 24
    top11 = insn >> 21

    if (top8 & 0x3F) in (0x39, 0x3D, 0x19, 0x1D, 0x39, 0x3D):
        pass  # placeholder; explicit list below mirrors the C
    for v in (0x39, 0x3D, 0xB9, 0xBD, 0xF9, 0xFD, 0x79, 0x7D):
        if (top8 & 0x3F) == (v & 0x3F):
            if rn == 18:
                return RN
    if (top11 & 0x1F9) == 0x1C1 and ((insn >> 10) & 3) == 2:
        if rn == 18: return RN
        if rm == 18: return RM
    if (top11 & 0x3E7) == 0x1C0:
        if rn == 18: return RN
    for v in (0x28, 0x2C, 0xA8, 0xAC, 0x68, 0x6C):
        if (top8 & 0x3E) == (v & 0x3E):
            if rn == 18: return RN
            if rt2 == 18: return RT2
    if (top11 & 0x7FF) in (0x150, 0x550):
        if rm == 18: return RM
    if (top8 & 0x5F) == 0x11:
        if rn == 18: return RN
    if (top8 & 0x5F) == 0x0B:
        if rn == 18: return RN
        if rm == 18: return RM
    return NONE

def main(path):
    d = open(path, 'rb').read()
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    nsec = struct.unpack_from('<H', d, pe + 6)[0]
    start = pe + 24 + struct.unpack_from('<H', d, pe + 20)[0]

    # Largest executable section -- the same .text the patcher walks.
    text = None
    for i in range(nsec):
        o = start + i * 40
        name = d[o:o+8].rstrip(b'\0').decode(errors='replace')
        vsize, va, rawsize, rawptr = struct.unpack_from('<IIII', d, o + 8)
        chars = struct.unpack_from('<I', d, o + 36)[0]
        if (chars & 0x20000000) and (text is None or vsize > text[1]):
            text = (name, vsize, rawptr, rawsize)
    if not text:
        print(f"::error::{path}: no executable section")
        return 1

    name, vsize, rawptr, rawsize = text
    body = d[rawptr:rawptr + min(rawsize, vsize)]
    hits = 0
    for off in range(0, len(body) - 3, 4):
        if x18_role(struct.unpack_from('<I', body, off)[0]) != NONE:
            hits += 1
    print(f"{path}: .text={name} size=0x{vsize:x} -- {hits} x18 references "
          f"({hits * 32} bytes of trampoline)")
    return 0

if __name__ == '__main__':
    for p in sys.argv[1:]:
        main(p)
