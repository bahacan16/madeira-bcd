#!/usr/bin/env python3
"""madeira-bcd: ucrtbase's floor and sqrt as x64 code for x64 callers.

An x64 program calls an ARM64EC export through the export's x64 thunk (the
linker's fast-forward sequence in .hexpthk), and FEX then leaves the emulator
for the native body and comes back after it: a transition of several dozen
instructions, with a full barrier on the way back into the JIT. For floor and
sqrt the native body is one instruction. Horizon Zero Dawn calls floor 1.8-3.2
million times and sqrt 0.2-0.8 million times a second, about half of all its
x64->EC calls (build 492, log 2026-10-10 20:41, [xp-api-top]: ucrtbase.dll
+0x84200 = floor, +0x3ad0c = sqrt), while the iOS power clamp holds the whole
process to about four efficiency cores.

This patch gives the exports floor, _o_floor, sqrt and _o_sqrt x64 bodies
(dlls/ucrtbase/x64math.c, a `#pragma makedep arm64ec_x64` source as ntdll's
signal_x86_64.c is), which the emulator runs without leaving it:
  floor  roundsd $9 (toward minus infinity, no precision exception): the
         result Wine's floor (musl) gives for every input; a signalling NaN
         comes back quiet, as from Windows' own floor.
  sqrt   sqrtsd for x >= 0 (and +inf): the result Wine's MSVCRT_sqrt gives.
         Negative x and NaN go on to Wine's MSVCRT_sqrt (errno, _matherr),
         as through the fast-forward sequence.
lld names the x64 thunk of an export EXP+#<export name> and uses one defined
in an object instead of generating it; its CHPE code map marks the bodies X64
and its redirection metadata still sends ARM64EC importers (and
GetProcAddress) to the native bodies, so ARM64EC callers are unchanged.

The patched DLL ships as arm64ec-windows/ucrtbase-x64math.dll NEXT TO the
untouched ucrtbase.dll; only a game whose settings have
env.MADEIRA_UCRT_X64_MATH = 1 gets it (app/Madeira/WineProcessBridge.m links
it as system32\\ucrtbase.dll for that session). Every other game keeps the
shipped ucrtbase.dll.

Usage:
  patch-wine-ucrtbase-x64-math.py WINE_SRC          patch the tree (idempotent)
  patch-wine-ucrtbase-x64-math.py --verify DLL      check a built DLL: the four
      exports point into X64 code ranges and start with the expected bytes
tools/build-wine-extra-dlls.sh applies it around its ucrtbase build and
restores the tree afterwards.
"""
import os
import struct
import sys

MARK = "madeira-bcd: x64 bodies"
SOURCE = "x64math.c"

X64MATH_C = r'''/*
 * madeira-bcd: x64 bodies for ucrtbase's floor and sqrt exports
 * (tools/patch-wine-ucrtbase-x64-math.py; shipped as ucrtbase-x64math.dll,
 * used only with env.MADEIRA_UCRT_X64_MATH = 1).
 *
 * An x64 caller of an ARM64EC export leaves the emulator for the native body;
 * for these one-instruction functions that transition is nearly all the cost.
 * lld uses EXP+#<export name> as the export's x64 thunk when an object defines
 * it, and still redirects ARM64EC callers to the native bodies.
 */

#if 0
#pragma makedep arm64ec_x64
#endif

#ifdef __arm64ec_x64__

#include "wine/asm.h"

/* toward minus infinity, precision exception suppressed: Wine's floor
 * (musl) for every input but a signalling NaN, which comes back quiet */
#define X64_FLOOR "roundsd $9,%xmm0,%xmm0\n\tret"

/* x >= 0 and +inf: the result of Wine's MSVCRT_sqrt; negative x and NaN
 * (ucomisd sets CF for both) go on to it for errno and _matherr */
#define X64_SQRT  "xorpd %xmm1,%xmm1\n\t" \
                  "ucomisd %xmm1,%xmm0\n\t" \
                  "jb 1f\n\t" \
                  "sqrtsd %xmm0,%xmm0\n\t" \
                  "ret\n" \
                  "1:\tjmp \"#MSVCRT_sqrt\""

__ASM_GLOBAL_FUNC( "EXP+#floor", X64_FLOOR )
__ASM_GLOBAL_FUNC( "EXP+#_o_floor", X64_FLOOR )
__ASM_GLOBAL_FUNC( "EXP+#sqrt", X64_SQRT )
__ASM_GLOBAL_FUNC( "EXP+#_o_sqrt", X64_SQRT )

#endif  /* __arm64ec_x64__ */
'''

# The bodies' bytes, as x86_64 clang assembles them (checked by --verify).
FLOOR_BYTES = bytes.fromhex("660f3a0bc009" "c3")
SQRT_BYTES = bytes.fromhex("660f57c9" "660f2ec1" "7205" "f20f51c0" "c3" "e9")
EXPORTS = {"floor": FLOOR_BYTES, "_o_floor": FLOOR_BYTES, "sqrt": SQRT_BYTES, "_o_sqrt": SQRT_BYTES}


def patch(wine):
    mk = os.path.join(wine, "dlls", "ucrtbase", "Makefile.in")
    src = os.path.join(wine, "dlls", "ucrtbase", SOURCE)
    if not os.path.isfile(mk):
        sys.exit("patch-wine-ucrtbase-x64-math: no " + mk)
    with open(src, "w") as f:
        f.write(X64MATH_C)
    text = open(mk).read()
    if "\t%s \\\n" % SOURCE in text or "\t%s\n" % SOURCE in text:
        print("already patched"); return
    lines = text.split("\n")
    try:
        start = lines.index("SOURCES = \\")
    except ValueError:
        sys.exit("patch-wine-ucrtbase-x64-math: SOURCES anchor not found in " + mk)
    end = start + 1
    while end < len(lines) and lines[end].endswith("\\"):
        end += 1
    if end >= len(lines) or not lines[end].startswith("\t"):
        sys.exit("patch-wine-ucrtbase-x64-math: SOURCES list not understood in " + mk)
    # the list's last line has no continuation: give it one and append ours
    lines[end] = lines[end] + " \\"
    lines.insert(end + 1, "\t" + SOURCE)
    open(mk, "w").write("\n".join(lines))
    print("patched " + mk + " (+" + SOURCE + ")")


def pe_sections(d):
    pe = struct.unpack_from("<I", d, 0x3c)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    secs = []
    for i in range(nsec):
        s = opt + optsz + 40 * i
        vsz, va, rsz, raw = struct.unpack_from("<IIII", d, s + 8)
        secs.append((va, max(vsz, rsz), raw))
    return pe, opt, secs


def rva_to_off(secs, rva):
    for va, size, raw in secs:
        if va <= rva < va + size:
            return rva - va + raw
    raise ValueError("rva %#x outside the sections" % rva)


def verify(path):
    d = open(path, "rb").read()
    pe, opt, secs = pe_sections(d)
    if struct.unpack_from("<H", d, opt)[0] != 0x20b:
        sys.exit("verify: not a PE32+ image")
    ddir = opt + 112
    exp_rva = struct.unpack_from("<I", d, ddir)[0]
    cfg_rva, cfg_size = struct.unpack_from("<II", d, ddir + 10 * 8)
    e = rva_to_off(secs, exp_rva)
    nfun, nnames, afun, anames, aords = struct.unpack_from("<IIIII", d, e + 20)
    exports = {}
    for i in range(nnames):
        no = rva_to_off(secs, struct.unpack_from("<I", d, rva_to_off(secs, anames) + 4 * i)[0])
        name = d[no:d.index(b"\0", no)].decode("latin-1")
        o = struct.unpack_from("<H", d, rva_to_off(secs, aords) + 2 * i)[0]
        exports[name] = struct.unpack_from("<I", d, rva_to_off(secs, afun) + 4 * o)[0]
    # IMAGE_LOAD_CONFIG_DIRECTORY64.CHPEMetadataPointer at 0xC8; the metadata's
    # CodeMap RVA and count at +4 and +8, entries (StartOffset | type, Length)
    if cfg_size <= 0xC8:
        sys.exit("verify: no CHPE metadata (load config too small)")
    image_base = struct.unpack_from("<Q", d, opt + 24)[0]
    chpe = struct.unpack_from("<Q", d, rva_to_off(secs, cfg_rva) + 0xC8)[0] - image_base
    m = rva_to_off(secs, chpe)
    cm_rva, cm_n = struct.unpack_from("<II", d, m + 4)
    ranges = []
    for i in range(cm_n):
        start, length = struct.unpack_from("<II", d, rva_to_off(secs, cm_rva) + 8 * i)
        ranges.append((start & ~3, length, start & 3))
    bad = []
    for name, body in EXPORTS.items():
        rva = exports.get(name)
        if rva is None:
            bad.append("%s: not exported" % name); continue
        kind = [k for s, l, k in ranges if s <= rva < s + l]
        if kind != [2]:
            bad.append("%s: rva %#x is not in an X64 code range (%s)" % (name, rva, kind)); continue
        got = d[rva_to_off(secs, rva):rva_to_off(secs, rva) + len(body)]
        if got != body:
            bad.append("%s: body %s, expected %s" % (name, got.hex(), body.hex()))
    if bad:
        sys.exit("verify %s:\n  %s" % (path, "\n  ".join(bad)))
    print("verify %s: floor, _o_floor, sqrt and _o_sqrt are x64 bodies in X64 code ranges" % path)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify":
        verify(sys.argv[2])
    elif len(sys.argv) == 2:
        patch(sys.argv[1])
    else:
        sys.exit(__doc__)
