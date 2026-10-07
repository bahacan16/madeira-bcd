#!/usr/bin/env python3
"""Halve the ARM64EC call-return stack reservation from 16 MiB to 8 MiB.

The call-return stack is a return-address predictor. Every JIT access is
bounded to [base + 2 MiB, base + 6 MiB): the CALL push and RET pop sites in
BranchOps.cpp reset the pointer to base + 4 MiB once it leaves that window
(`(sp - (base + 2 MiB)) >> 22`), so the upper 10 MiB of each 16 MiB
reservation are never touched. With ~260 translated threads in a licensed
Grand Theft Auto V Enhanced session (Rockstar Games Launcher, Chromium,
Steam alive) those reservations took ~4 GB of the 12 GB FEX arena that
env.MADEIRA_SC_PA_POOLS = 2 leaves, and the arena ran out (FEXAlloc stop,
0xC000001D; builds 434 and 436).

8 MiB keeps the window and the reset target where they are. The three
derived offsets become fixed values (they were SIZE/4, SIZE/8, SIZE/4), the
two sites that computed the default location as SIZE/4 use the named
constant, and the dispatcher's whole-region test before the JITCallback
sentinel push shifts by 23 instead of 24 so it still means "outside the
reservation". A Madeira build overlay for the ARM64EC module only
(tools/build-xtajit64.sh), not a change to the pinned submodule.
"""
from pathlib import Path
import sys

MARKER = "madeira-bcd: 8 MiB call-return stack"

EDITS = {
    "FEXCore/include/FEXCore/Debug/InternalThreadState.h": (
        (
            "  static constexpr size_t CALLRET_STACK_SIZE {0x1000000};",
            "  /* " + MARKER + " (tools/patch-fex-ios-callret-8mb.py): every JIT\n"
            "   * access stays in [2 MiB, 6 MiB), so 16 MiB reserved 10 MiB that\n"
            "   * nothing touches, per thread, in a 12 GB FEX arena. */\n"
            "  static constexpr size_t CALLRET_STACK_SIZE {0x800000};",
        ),
        (
            "  static constexpr size_t CALLRET_DEFAULT_OFFSET {CALLRET_STACK_SIZE / 4};",
            "  static constexpr size_t CALLRET_DEFAULT_OFFSET {0x400000};",
        ),
        (
            "  static constexpr size_t CALLRET_LIVE_OFFSET {CALLRET_STACK_SIZE / 8};",
            "  static constexpr size_t CALLRET_LIVE_OFFSET {0x200000};",
        ),
        (
            "  static constexpr size_t CALLRET_LIVE_SIZE {CALLRET_STACK_SIZE / 4};",
            "  static constexpr size_t CALLRET_LIVE_SIZE {0x400000};",
        ),
    ),
    "FEXCore/Source/Interface/Core/Core.cpp": (
        (
            "CRBase + FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE / 4 - CRSp",
            "CRBase + FEXCore::Core::InternalThreadState::CALLRET_DEFAULT_OFFSET - CRSp",
        ),
        (
            "uint64_t default_loc = crbase + FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE / 4;",
            "uint64_t default_loc = crbase + FEXCore::Core::InternalThreadState::CALLRET_DEFAULT_OFFSET;",
        ),
    ),
    "Source/Windows/Common/CallRetStack.h": (
        (
            "          Base + FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE / 4};",
            "          Base + FEXCore::Core::InternalThreadState::CALLRET_DEFAULT_OFFSET};",
        ),
    ),
    "FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp": (
        (
            "      sub(ARMEmitter::Size::i64Bit, TMP1, REG_CALLRET_SP, TMP1);\n"
            "      lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 24);",
            "      sub(ARMEmitter::Size::i64Bit, TMP1, REG_CALLRET_SP, TMP1);\n"
            "      lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 23);   /* " + MARKER + ": the reservation is 2^23 */",
        ),
    ),
}

# Lines that must still read the same after the edits: the JIT window and
# its reset target (BranchOps.cpp) are what make 8 MiB enough.
WINDOW_CHECKS = (
    ("FEXCore/Source/Interface/Core/JIT/BranchOps.cpp", "add(ARMEmitter::Size::i64Bit, TMP1, TMP1, 0x200000);", 3),
    ("FEXCore/Source/Interface/Core/JIT/BranchOps.cpp", "lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 22);", 3),
    ("FEXCore/Source/Interface/Core/JIT/BranchOps.cpp", "add(ARMEmitter::Size::i64Bit, REG_CALLRET_SP, REG_CALLRET_SP, 0x400000);", 3),
    ("FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp", "add(ARMEmitter::Size::i64Bit, REG_CALLRET_SP, REG_CALLRET_SP, 0x400000);", 1),
)


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} <FEX source root>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    for rel, needle, minimum in WINDOW_CHECKS:
        text = (root / rel).read_text()
        if text.count(needle) < minimum:
            print(f"{rel}: expected at least {minimum} x {needle!r}; the JIT window changed, "
                  "8 MiB may no longer be enough", file=sys.stderr)
            return 1
    for rel, edits in EDITS.items():
        path = root / rel
        text = path.read_text()
        if all(old not in text and new in text for old, new in edits):
            print(f"{rel}: already patched")
            continue
        for old, new in edits:
            if text.count(old) != 1:
                print(f"{rel}: expected exactly one {old.splitlines()[0]!r}, found {text.count(old)}",
                      file=sys.stderr)
                return 1
            text = text.replace(old, new)
        path.write_text(text)
        print(f"{rel}: patched")
    for rel in ("FEXCore/Source/Interface/Core/Core.cpp", "Source/Windows/Common/CallRetStack.h"):
        if "CALLRET_STACK_SIZE / 4" in (root / rel).read_text():
            print(f"{rel}: a SIZE/4 default location is left", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
