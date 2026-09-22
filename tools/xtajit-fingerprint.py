#!/usr/bin/env python3
"""Compare the xtajit64.dll we build against the one Build 79 ships.

Build 79's IPA runs Fallout and Stray on the device this fork dies on. Its
arm64ecfex build and ours come from the same pinned FEX revision (053c385,
Madeira line ml755) through the same llvm-mingw 22.1.4, and their .text
sections have the *same virtual size to the byte* -- 0x1ffcf6. So the two
binaries are near-identical, and every byte that is not is either one of our
patches or a bug.

Nothing here can download our own CI artifact: the runner's egress reaches
GitHub and nothing else, and the IPA is 57 MB besides. What it can do is put
the answer in the build log. This emits a reference -- per-chunk hashes of
Build 79's binary, small enough to keep in the tree -- and, on every build,
says which chunks of ours differ and dumps their bytes. Those bytes can then
be disassembled against the reference binary off-runner.

Chunk indices are section-relative, so a differing chunk maps straight to an
RVA and, through .pdata, to a function.

    emit    <dll> <ref>     write a reference fingerprint
    compare <dll> <ref>     report how <dll> differs from it

compare never fails the build. It is a measurement, not a gate; a build that
legitimately differs (a new probe, a new patch) should say so and carry on.
"""
import hashlib
import struct
import sys

# .text is the point of the exercise, so it gets the finest grain. The others
# are here to notice a layout change, not to be read instruction by
# instruction.
GRAIN = {".text": 256, ".rdata": 1024, ".data": 256, ".hexpthk": 64, ".a64xrm": 64}
DEFAULT_GRAIN = 4096

# How much of our build to print when chunks differ. Twelve chunks of .text is
# 3 KB of hex -- enough to disassemble, small enough to read.
DUMP_CHUNKS = 12


def sections(path):
    d = open(path, "rb").read()
    e = struct.unpack_from("<I", d, 0x3C)[0]
    if d[e:e + 4] != b"PE\0\0":
        raise SystemExit(f"{path}: not a PE")
    nsec = struct.unpack_from("<H", d, e + 6)[0]
    optsz = struct.unpack_from("<H", d, e + 20)[0]
    salign = struct.unpack_from("<I", d, e + 24 + 32)[0]
    off = e + 24 + optsz
    out = {}
    for i in range(nsec):
        b = d[off + i * 40:off + (i + 1) * 40]
        name = b[:8].rstrip(b"\0").decode(errors="replace")
        vsz, va, rsz, ra = struct.unpack_from("<IIII", b, 8)
        out[name] = (vsz, va, d[ra:ra + min(vsz, rsz)])
    return d, salign, out


def h(b, n=8):
    return hashlib.sha256(b).hexdigest()[:n]


def emit(dll, ref):
    d, salign, secs = sections(dll)
    lines = [
        "# xtajit64 reference fingerprint -- the arm64ecfex build shipped in",
        "# Madeira Build 79, the IPA that runs Fallout and Stray on device.",
        "# Chunk indices are section-relative; see tools/xtajit-fingerprint.py.",
        f"FILE {len(d)} {hashlib.sha256(d).hexdigest()}",
        f"ALIGN 0x{salign:x}",
    ]
    for name, (vsz, va, body) in secs.items():
        # The DWARF sections carry build paths and say nothing about behaviour.
        if name.startswith("/"):
            continue
        lines.append(f"SEC {name} {vsz} 0x{va:x} {h(body, 16)}")
    for name, (vsz, va, body) in secs.items():
        if name.startswith("/"):
            continue
        g = GRAIN.get(name, DEFAULT_GRAIN)
        for i in range(0, len(body), g):
            lines.append(f"C {name} {i // g} {h(body[i:i + g])}")
    open(ref, "w").write("\n".join(lines) + "\n")
    print(f"wrote {ref}: {len(lines)} lines from {dll}")
    return 0


def load(ref):
    meta, chunks = {}, {}
    for line in open(ref):
        f = line.split()
        if not f or f[0].startswith("#"):
            continue
        if f[0] == "FILE":
            meta["size"], meta["sha"] = int(f[1]), f[2]
        elif f[0] == "ALIGN":
            meta["align"] = int(f[1], 16)
        elif f[0] == "SEC":
            meta.setdefault("sec", {})[f[1]] = (int(f[2]), int(f[3], 16), f[4])
        elif f[0] == "C":
            chunks.setdefault(f[1], {})[int(f[2])] = f[3]
    return meta, chunks


def ranges(idx):
    """Collapse [1,2,3,9,10] into ['1-3', '9-10'] so the log stays readable."""
    out, start, prev = [], None, None
    for i in idx:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append(f"{start}-{prev}" if prev > start else f"{start}")
            start = prev = i
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else f"{start}")
    return out


def compare(dll, ref):
    meta, chunks = load(ref)
    d, salign, secs = sections(dll)

    if hashlib.sha256(d).hexdigest() == meta["sha"]:
        print("::notice::xtajit64 fingerprint: IDENTICAL to Build 79 byte for byte")
        return 0

    print(f"::notice::xtajit64 fingerprint: ours {len(d)} bytes align 0x{salign:x} "
          f"-- Build 79 {meta['size']} bytes align 0x{meta['align']:x}")

    dumps = []
    for name, (rvsz, rva, rsha) in sorted(meta["sec"].items()):
        if name not in secs:
            print(f"::warning::section {name} is in Build 79 and not in ours")
            continue
        vsz, va, body = secs[name]
        if h(body, 16) == rsha:
            print(f"::notice::  {name:<9} identical ({vsz} bytes)")
            continue
        if vsz != rvsz:
            print(f"::notice::  {name:<9} DIFFERENT SIZE: ours {vsz}, Build 79 {rvsz} "
                  f"({vsz - rvsz:+d}) -- chunk indices past the common part mean nothing")
        g = GRAIN.get(name, DEFAULT_GRAIN)
        theirs = chunks.get(name, {})
        diff = []
        for i in range(0, min(len(body), rvsz), g):
            k = i // g
            if k in theirs and h(body[i:i + g]) != theirs[k]:
                diff.append(k)
        total = min(len(body), rvsz + g - 1) // g + 1
        if not diff:
            print(f"::notice::  {name:<9} same content over the common {min(len(body), rvsz)} bytes")
            continue
        r = ranges(diff)
        print(f"::notice::  {name:<9} {len(diff)}/{total} chunks differ (chunk={g}B) :: "
              + " ".join(r[:24]) + (" ..." if len(r) > 24 else ""))
        if name == ".text":
            for k in diff[:DUMP_CHUNKS]:
                dumps.append((name, k, va + k * g, body[k * g:(k + 1) * g]))

    # The bytes themselves, because the log is the only way they leave the
    # runner. RVA is printed so the chunk can be found in the reference binary.
    for name, k, rva, blob in dumps:
        print(f"--- ours {name} chunk {k} rva 0x{rva:x} ({len(blob)} bytes)")
        for o in range(0, len(blob), 32):
            print(f"    +{o:03x} {blob[o:o + 32].hex()}")
    return 0


def main(argv):
    if len(argv) != 4 or argv[1] not in ("emit", "compare"):
        print(__doc__)
        return 2
    return (emit if argv[1] == "emit" else compare)(argv[2], argv[3])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
