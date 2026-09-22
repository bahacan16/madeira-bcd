#!/usr/bin/env python3
"""Verify the shipped arm64ec ntdll.dll carries the Build 79 TLS fixes.

Patches 01 (ntdll static TLS isolation) and 03 (module TLS) change wine's
PE-side dlls/ntdll/loader.c. This repository ships ntdll.dll as a committed
binary and builds no PE DLL from source, so those two are the only Build 79
runtime fixes that cannot be applied as a patch here -- see
docs/build79-transplant.md.

The 2026-09-22 comparison established that this is what separates a build
which runs x64 guests from one which does not. Two logs from the same device,
same game, minutes apart:

    working build:   err:module:alloc_module_tls_slot   (three times)
    ours:            absent

alloc_module_tls_slot does not exist in the unpatched tree -- patch 03 adds
it ("+static NTSTATUS alloc_module_tls_slot( LDR_DATA_TABLE_ENTRY *mod )").
Its presence in a binary is therefore a direct test for the patch, and it
survives stripping because wine's debug macros embed the function name.

Without it, ntdll's own TLS index stays zero, which Madeira allocates to the
main executable, so wine's exception and unwind code reads the executable's
TLS block instead of its own. Our crash was a branch to the ARM64EC CPU area
-- a data structure -- from inside a JIT block.

This checks the shipped binary rather than the source because the shipped
binary is all we have. It cannot tell a good build from a bad one; it can
only tell whether this particular fix is in.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "app/Madeira/arm64ec-windows/ntdll.dll"
MARKER = b"alloc_module_tls_slot"


def main():
    if not TARGET.exists():
        print(f"::error::{TARGET.relative_to(ROOT)} is missing")
        return 1
    data = TARGET.read_bytes()
    if MARKER not in data:
        print(f"::error::shipped arm64ec ntdll.dll lacks {MARKER.decode()} -- "
              "Build 79 patches 01/03 are not in it, and x64 guests will branch "
              "into the ARM64EC CPU area. See docs/build79-transplant.md")
        return 1
    print(f"::notice::arm64ec ntdll.dll carries the Build 79 TLS fixes "
          f"({data.count(MARKER)} references, {len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
