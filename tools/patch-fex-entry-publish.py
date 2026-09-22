#!/usr/bin/env python3
"""Make FEX's own BAD-ENTRY guard say something when it passes, and when it cannot run.

The 2026-09-22 evening run closed the cache question and opened this one. The
block-cache probe logged nothing at all -- not one of its first 24
unconditional lines -- so LookupCache::FindBlock never returned a hit before
the crash, and [fexlock] UNPUB-COMPILE never fired either, so the branch that
consults the cache is the one that ran and it simply always missed. The value
the dispatcher branched to therefore came from the compile path, not the
cache.

And the compile path already has a check for exactly this fault, written by
the Madeira developer in Core.cpp:

    /* iOS-Madeira diag (Thumper desktop ILL 2026-07-06): three crashes
     * branched to BlockTail+0x18 instead of a code entry -- the published
     * entry itself was wrong. */

BlockTail+0x18 is tail+24, which is where two of our three faults landed (the
third was tail+8). Same bug, same signature, seen in July on Thumper's
desktop, diagnosed and left.

Its [fex-entry] line has never appeared in any of our logs. That is two
different statements and the code cannot tell them apart:

  * the published entry was valid and something downstream produced tail+24;
  * or the guard never ran, because it is written
    `if (CodePtr && CompiledCode.BlockBegin)` and BlockBegin was null, in
    which case a bad entry sails through in silence.

So this makes the guard account for itself. The BAD check is unchanged. Added
around it: the first 16 publications are logged whether or not they pass, and
a publication that cannot be validated at all -- CodePtr set, BlockBegin null
-- is logged separately and capped. One run then says which of the two
statements is true, and if entries are being published sound, the search moves
to the only step left: the dispatcher's own `br` on the returned value.
"""
import sys

OLD = """  if (CodePtr && CompiledCode.BlockBegin) {
    uint32_t TailOff = *reinterpret_cast<uint32_t*>(CompiledCode.BlockBegin);
    uint8_t* Tail = CompiledCode.BlockBegin + TailOff;
    uint32_t FirstInsn = *reinterpret_cast<uint32_t*>(CodePtr);
    if (reinterpret_cast<uint8_t*>(CodePtr) < CompiledCode.BlockBegin ||
        reinterpret_cast<uint8_t*>(CodePtr) >= Tail || FirstInsn == 0xd503201fu) {
      LogMan::Msg::EFmt("[fex-entry] BAD ENTRY at publication: rip=0x{:x} entry=0x{:x} "
                        "block=0x{:x} tail_off=0x{:x} first_insn=0x{:08x}",
                        GuestRIP, reinterpret_cast<uintptr_t>(CodePtr),
                        reinterpret_cast<uintptr_t>(CompiledCode.BlockBegin), TailOff, FirstInsn);
    }
  }"""

NEW = """  if (CodePtr && CompiledCode.BlockBegin) {
    uint32_t TailOff = *reinterpret_cast<uint32_t*>(CompiledCode.BlockBegin);
    uint8_t* Tail = CompiledCode.BlockBegin + TailOff;
    uint32_t FirstInsn = *reinterpret_cast<uint32_t*>(CodePtr);
    if (reinterpret_cast<uint8_t*>(CodePtr) < CompiledCode.BlockBegin ||
        reinterpret_cast<uint8_t*>(CodePtr) >= Tail || FirstInsn == 0xd503201fu) {
      LogMan::Msg::EFmt("[fex-entry] BAD ENTRY at publication: rip=0x{:x} entry=0x{:x} "
                        "block=0x{:x} tail_off=0x{:x} first_insn=0x{:08x}",
                        GuestRIP, reinterpret_cast<uintptr_t>(CodePtr),
                        reinterpret_cast<uintptr_t>(CompiledCode.BlockBegin), TailOff, FirstInsn);
    }
#ifdef FEX_IOS_HOST
    /* iOS-Madeira ml792: see tools/patch-fex-entry-publish.py. A guard that
     * only speaks when it fails cannot be told apart from a guard that never
     * ran, and this one has been silent through three faults at the address
     * it was written for. */
    else {
      static std::atomic<int> IosPubOK {0};
      const int IosPubN = IosPubOK.fetch_add(1, std::memory_order_relaxed);
      if (IosPubN < 16) {
        LogMan::Msg::EFmt("[fex-entry] ok #{} rip={:#x} entry={:#x} block={:#x} "
                          "tail_off={:#x} first_insn={:#010x} rev=ml792",
                          IosPubN, GuestRIP, reinterpret_cast<uintptr_t>(CodePtr),
                          reinterpret_cast<uintptr_t>(CompiledCode.BlockBegin), TailOff, FirstInsn);
      }
    }
#endif
  }
#ifdef FEX_IOS_HOST
  else if (CodePtr) {
    /* ml792: CodePtr set but BlockBegin null -- the guard above cannot run at
     * all, so a bad entry would be published in silence. */
    static std::atomic<int> IosPubBlind {0};
    if (IosPubBlind.fetch_add(1, std::memory_order_relaxed) < 8) {
      LogMan::Msg::EFmt("[fex-entry] UNVALIDATED rip={:#x} entry={:#x} -- BlockBegin is null, "
                        "the publication guard cannot run rev=ml792", GuestRIP,
                        reinterpret_cast<uintptr_t>(CodePtr));
    }
  }
#endif"""


def main(path):
    s = open(path).read()
    if "[fex-entry] ok #{}" in s:
        print("::notice::fex entry-publication trace already patched")
        return 0
    n = s.count(OLD)
    if n != 1:
        print(f"::error::entry-publication anchor not found ({n} matches) -- upstream changed, review needed")
        return 1
    open(path, "w").write(s.replace(OLD, NEW, 1))
    print("::notice::fex entry-publication trace added (first 16 good, plus every unvalidatable one)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
