#!/usr/bin/env python3
"""Take DXMT's PE half from the verified Build 79 IPA, because we build the other half.

DXMT is two halves. The unix half is compiled from the `research/dxmt`
submodule and linked into the Madeira binary; the PE half -- d3d11.dll,
dxgi.dll, d3d10core.dll, winemetal.dll -- ships as committed binaries that
nothing in this repository builds. The PE build was dropped as "redundant" in
commit 4184816 and the committed DLLs have been frozen since.

They are no longer the same DXMT. Measured on 2026-09-22 against the Build 79
IPA the user has, which runs Fallout and Stray on this device:

  * the submodule is pinned at b4b89f0, which is the tip of willfaust/dxmt
    ios-port -- there is nothing newer upstream, so Build 79's DLLs and our
    unix half are built from the *same source*;
  * Build 79's DLLs are dated 2026-09-17, ours 2026-08-27, and 2026-08-29 is
    when b4b89f0 landed. Ours predate the commit we link against;
  * the boundary between the modules moved in between. airconv's reflection
    symbols (MTL_SM50_SHADER_ARGUMENT_*, MTL_GEOMETRY_SHADER_PASS_THROUGH,
    ArgumentTableQwords) are in the submodule's airconv_public.h and in Build
    79's winemetal.dll, and absent from our committed winemetal.dll. Their
    d3d11.dll .text is 61,440 bytes *smaller* than ours: code moved out of
    d3d11 and into winemetal.

So our IPA pairs a unix half from b4b89f0 with a PE half from before it. That
is not a version we have ever tested; it is a version nobody built. And the
guest now dies with d3d11.dll on the stack.

Every other Windows DLL in the two bundles is byte-identical -- 259 of them --
so this is the whole of the difference on the graphics side.

The right end state is building the PE half here from the pinned submodule,
the way xtajit64.dll is built. Until that exists, this takes the binaries from
an IPA whose provenance is known: Build 79, version 0.34.7, GPLv3, its
corresponding source published and its runtime patches already transplanted
here. Every file is checked against tools/ref/build79-runtime.manifest before
it is used, and so is the IPA itself, so a replaced release asset fails the
build instead of silently changing what ships.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools/ref/build79-runtime.manifest"
DEST = ROOT / "app/Madeira"
PREFIX = "Payload/Madeira.app/"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load():
    ipa, files = None, []
    for line in MANIFEST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if f[0] == "IPA":
            ipa = (f[1], int(f[2]))
        else:
            files.append((f[0], int(f[1]), f[2]))
    return ipa, files


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    ipa_path = Path(argv[1])
    (want_sha, want_size), files = load()

    if not ipa_path.exists():
        print(f"::error::{ipa_path} is missing -- the Build 79 IPA was not downloaded")
        return 1
    size, got = ipa_path.stat().st_size, sha256(ipa_path)
    if got != want_sha:
        print(f"::error::Build 79 IPA does not match the manifest: got {got} "
              f"({size} bytes), expected {want_sha} ({want_size} bytes). The release "
              f"asset was replaced -- refusing to ship binaries we have not verified")
        return 1
    print(f"::notice::Build 79 IPA verified ({size} bytes)")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(ipa_path) as z:
            names = set(z.namelist())
            for want, want_n, rel in files:
                member = PREFIX + rel
                if member not in names:
                    print(f"::error::{member} is not in the IPA")
                    return 1
                out = tmp / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                got = sha256(out)
                if got != want or out.stat().st_size != want_n:
                    print(f"::error::{rel}: got {got} ({out.stat().st_size} bytes), "
                          f"expected {want} ({want_n} bytes)")
                    return 1

        replaced = 0
        for _, _, rel in files:
            target = DEST / rel
            before = sha256(target) if target.exists() else None
            src = tmp / rel
            if before == sha256(src):
                print(f"::notice::  {rel:<34} already current")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            was = target.stat().st_size if target.exists() else 0
            shutil.copy2(src, target)
            replaced += 1
            print(f"::notice::  {rel:<34} {was} -> {target.stat().st_size} bytes")

    print(f"::notice::Build 79 runtime import: {replaced} of {len(files)} components replaced")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
