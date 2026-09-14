#!/usr/bin/env python3
"""Report whether a PE's sections land on iOS's 16KB page grid.

iOS pages are 16KB. When a PE's .data does not start on that grid,
virtual_ios.c (ios_jit_data_align_delta) shifts the WHOLE pool copy of the
image so .data lands right -- and the x18 trampoline region, which is placed
"right after the aligned image", moves with it.

That is not theoretical. Our first self-built xtajit64.dll came out 0x6000
smaller than the committed one, which put .data 0x2000 off the grid:

  [data-align] libarm64ecfex.dll shifted +0x2000 so .data lands on a 16KB page

and the x64 cube then died at tramp+0x64c with c0000005, where the committed
DLL -- needing no shift -- had got much further. So a build that needs the
shift is worth saying out loud.
"""
import struct, sys

def main(path):
    d = open(path, 'rb').read()
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    if d[pe:pe+4] != b'PE\0\0':
        print(f"::error::{path} is not a PE")
        return 1
    opt = pe + 24
    salign = struct.unpack_from('<I', d, opt + 32)[0]
    nsec = struct.unpack_from('<H', d, pe + 6)[0]
    start = opt + struct.unpack_from('<H', d, pe + 20)[0]

    off_grid = []
    for i in range(nsec):
        o = start + i * 40
        name = d[o:o+8].rstrip(b'\0').decode(errors='replace')
        va = struct.unpack_from('<I', d, o + 12)[0]
        if name == '.data':
            ok = va % 0x4000 == 0
            print(f"::notice::.data VA=0x{va:x} SectionAlignment=0x{salign:x} "
                  f"16KB-aligned={'YES -- no pool shift' if ok else 'NO -- the pool copy WILL be shifted'}")
        if va % 0x4000:
            off_grid.append(f"{name}@0x{va:x}")

    if off_grid:
        print(f"::warning::sections off the 16KB grid: {' '.join(off_grid[:8])}")
    else:
        print("::notice::every section is on the 16KB grid")
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
