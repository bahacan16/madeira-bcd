#!/usr/bin/env python3
"""Fail the build if a transplanted Build 79 runtime fix has gone missing.

Five of the fixes live directly in our own sources rather than in a CI patch
script, so nothing re-applies them on each run and nothing notices if one is
reverted -- by a bad merge, a stray revert, or a later edit to the same
function. The CI scripts already fail loudly when the pinned submodule moves;
this gives the in-tree half the same property.

It checks for a phrase unique to each fix, not for a whole hunk: a marker
survives reformatting and neighbouring edits, which is what we want. It cannot
prove a fix is still correct -- only that it has not vanished. See
docs/build79-transplant.md for what each one does.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (patch, file, marker, why it matters)
MARKERS = [
    (
        "06-native-tsd-bitmap-fix",
        "build/ntdll-unix/virtual_ios.c",
        "data_map[((i + 4) / 4) >> 3]",
        "the TEB-retarget pass must read data_map as a bitmap, not a byte array",
    ),
    (
        "08-fd-cache-exit-fix",
        "build/ntdll-unix/server_ios.c",
        "block[j].s.type != FD_TYPE_INVALID",
        "guest exit must not close a descriptor one higher than the real one",
    ),
    (
        "10-apc-signal-fix",
        "build/wineserver/mach_ios.c",
        "Use our task only\n     * for real thread-right extraction",
        "send_thread_signal must not read the disabled cross-process task port",
    ),
    (
        "11-apc-context-fix",
        "build/ntdll-unix/signal_arm64_ios.c",
        "An APC can interrupt FEX with a live x17",
        "usr1_handler must not run the x18 trampoline over a live x17",
    ),
    (
        "12-decommit-edge-fix",
        "build/ntdll-unix/virtual_ios.c",
        "IOS_DC_INLINE int ios_dc_prepare",
        "partial host pages must be prepared and restored, not cleared blind",
    ),
]

# Things a fix removed, which must not come back.
ABSENT = [
    (
        "11-apc-context-fix",
        "build/ntdll-unix/signal_arm64_ios.c",
        "        ios_fixup_x18_for_return( ucontext );",
        "the SIGUSR1 x18 fixup was deliberately dropped",
    ),
    (
        "12-decommit-edge-fix",
        "build/ntdll-unix/virtual_ios.c",
        "const char *dc_branch = \"none\";",
        "the old decommit_pages body was replaced whole",
    ),
]


def main():
    failures = []
    for patch, rel, marker, why in MARKERS:
        path = ROOT / rel
        if not path.exists():
            failures.append(f"{patch}: {rel} is missing entirely")
            continue
        if marker not in path.read_text(encoding="utf-8", errors="surrogateescape"):
            failures.append(f"{patch}: marker gone from {rel} -- {why}")

    for patch, rel, marker, why in ABSENT:
        path = ROOT / rel
        if path.exists() and marker in path.read_text(encoding="utf-8", errors="surrogateescape"):
            failures.append(f"{patch}: removed code is back in {rel} -- {why}")

    for f in failures:
        print(f"::error::transplant check: {f}")
    if failures:
        print(f"::error::{len(failures)} Build 79 transplant(s) no longer present; see docs/build79-transplant.md")
        return 1
    print(f"::notice::all {len(MARKERS)} in-tree Build 79 transplants present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
