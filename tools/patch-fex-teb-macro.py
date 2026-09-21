#!/usr/bin/env python3
"""Make Module.S read the TEB slot that ProcessInit discovered, not a placeholder.

NOT OUR FIX. This is `05-fex-runtime-teb-fix/runtime-teb.patch` from the
Madeira Build 79 corresponding-source package (GPLv3), written by Madeira's
developer and applied here verbatim -- the produced Module.S is byte-identical
to what `patch -p1 < runtime-teb.patch` yields on the pinned tree.

Why it matters here. Module.S reaches the TEB through IOS_LOAD_TEB, which the
pinned tree writes as a hand-assembled triplet with a FIXED slot:

    mrs \\reg, TPIDRRO_EL0
    and \\reg, \\reg, #~7
    ldr \\reg, [\\reg, #0x898]        <- placeholder, not the real slot

0x898 is meant to be rewritten at load time by wine's x18 binary patcher
("Pass 0: retarget hand-written TEB-from-TSD reads", virtual_ios.c:3850).
That pass prints a line whenever it matches anything, and across 17 device
logs it printed one in exactly two -- including none of the three 2026-09-15
x64-cube runs. The real slot on this device is 0x8d0, so those runs executed

    ldr x17, [x17, #0x898]

and got a TEB that is not the TEB. ExitFunctionEC's first act is
IOS_LOAD_TEB followed by `ldr x17, [x17, #0x1788]` (ChpeV2CpuAreaInfo), then
a store and two loads off that pointer -- which is where control goes missing
in every one of those logs. Meanwhile the JIT emitters (Dispatcher.cpp,
MiscOps.cpp, Arm64Emitter.cpp) read IosTebTsdOffset at runtime and get the
right slot, which is why compiled blocks work and the hand-written transition
asm does not.

The fix drops the dependency on the binary patcher: read IosTebTsdOffset
(defined in Arm64Emitter.cpp, published by ProcessInit from ntdll's
ios_teb_tsd_offset export) and fall back to x18 before publication. Each call
site hands the macro a scratch register that is dead at that point.

Applied as a CI patch rather than a submodule commit because the FEX submodule
points at willfaust/FEX, which we cannot push to -- the same reason the
suspend and fex-link patches live here.
"""
import sys

OLD_MACRO = r"""// iOS clobbers x18 on context switches. Read TEB from TPIDRRO_EL0 + the raw
// pthread TSD slot that wine's ntdll-unix publishes the TEB in, instead of
// trusting x18 directly. TPIDRRO_EL0 IS preserved by iOS across context
// switches, and AND #~7 + ldr is the canonical pthread TLS pattern.
//
// The #0x898 below is a PLACEHOLDER, not the slot. The real slot is whichever
// one backs ntdll-unix's TEB pthread key in this process, which is not knowable
// at assembly time and differs per device and per load order. Wine's x18
// binary patcher recognises this exact three-instruction triplet and rewrites
// the immediate through the .text RW alias, before this code is ever executed
// (see the "retarget hand-written TEB-from-TSD reads" pass in virtual_ios.c).
//
// Do NOT "correct" this constant, and do NOT break the triplet apart or
// reorder it -- the patcher matches all three instructions with matching
// registers, and an unmatched sequence silently keeps reading the wrong slot.
// The JIT emitters read the same offset at runtime via IosTebTsdOffset.
#ifdef FEX_IOS_HOST
.macro IOS_LOAD_TEB reg
  mrs \reg, TPIDRRO_EL0
  and \reg, \reg, #~7
  ldr \reg, [\reg, #0x898]
.endm
#endif"""

NEW_MACRO = r"""// Use the offset imported by ProcessInit, as the C++ IOSLoadTEB helper does.
// Each caller supplies a scratch register which is dead at this point. The
// sequence preserves NZCV and does not touch the stack or fixed TSD slots.
#ifdef FEX_IOS_HOST
.macro IOS_LOAD_TEB reg, scratch, scratchw
  adrp \scratch, IosTebTsdOffset
  ldr \scratchw, [\scratch, #:lo12:IosTebTsdOffset]
  cbz \scratchw, .Lios_teb_fallback\@
  mrs \reg, TPIDRRO_EL0
  and \reg, \reg, #~7
  ldr \reg, [\reg, \scratch]
  cbnz \reg, .Lios_teb_done\@
.Lios_teb_fallback\@:
  // Before ProcessInit or thread publication, retain the C++ helper fallback.
  mov \reg, x18
.Lios_teb_done\@:
.endm
#endif"""

# The call sites in file order, with the scratch register the patch gives each.
# check_target_ec and ios_ffs_xlate_done hold their result in x16 and so cannot
# use it as scratch; the rest load into x17 and scratch through x16.
CALL_SITES = [
    ("x16", "x17", "w17"),  # check_target_ec
    ("x16", "x23", "w23"),  # ios_ffs_xlate_done
    ("x17", "x16", "w16"),  # enter_jit
    ("x17", "x16", "w16"),  # BeginSimulation
    ("x17", "x16", "w16"),  # SyncThreadContext funnel
    ("x17", "x16", "w16"),  # ExitFunctionEC
]


def main(path):
    with open(path) as f:
        lines = f.read().split("\n")
    s = "\n".join(lines)

    if "IosTebTsdOffset" in s and ".macro IOS_LOAD_TEB reg, scratch, scratchw" in s:
        print("::notice::Module.S TEB macro already patched")
        return 0

    n = s.count(OLD_MACRO)
    if n != 1:
        print(f"::error::TEB macro anchor not found ({n} matches) -- pinned Module.S changed, review needed")
        return 1

    # Rewrite the call sites first, so the macro body (which contains no bare
    # `IOS_LOAD_TEB <reg>` line) cannot be confused for one.
    out, seen = [], []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("IOS_LOAD_TEB ") and "," not in stripped:
            reg = stripped.split()[1]
            idx = len(seen)
            if idx >= len(CALL_SITES):
                print(f"::error::more IOS_LOAD_TEB call sites than the patch knows about ({idx + 1})")
                return 1
            want, scratch, scratchw = CALL_SITES[idx]
            if reg != want:
                print(f"::error::call site {idx + 1} loads {reg}, patch expects {want} -- order changed")
                return 1
            seen.append(reg)
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}IOS_LOAD_TEB {reg}, {scratch}, {scratchw}")
        else:
            out.append(line)

    if len(seen) != len(CALL_SITES):
        print(f"::error::found {len(seen)} IOS_LOAD_TEB call sites, patch expects {len(CALL_SITES)}")
        return 1

    patched = "\n".join(out).replace(OLD_MACRO, NEW_MACRO)
    with open(path, "w") as f:
        f.write(patched)
    print(f"::notice::Module.S TEB macro now reads IosTebTsdOffset ({len(seen)} call sites updated)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
