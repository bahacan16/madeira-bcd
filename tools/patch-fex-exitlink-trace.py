#!/usr/bin/env python3
"""Log every ExitFunctionLink call, because the 2026-09-22 fault cannot be attributed without it.

The cube now dies inside FEX's dispatcher, and the log pins the instruction
exactly: pc is DispatchPtr -- [disp-addrs] prints Dispatch=0x156ffc000 and the
fault pc is 0x156ffc000 -- and the instruction is a9bf53f3, the first stp of
PushCalleeSavedRegisters. SP holds the guest's post-call RSP, 8 mod 16, so the
stp takes EXC_ARM_SP_ALIGN. DispatchPtr is the one-time entry into the JIT from
native code; nothing should branch to it while running.

Who did is not in the dump. LR is 0x156ffc2c8, and the three instructions
before it are

    mov x0, x28 ; mov x1, x30 ; ldr x2, [x28, #0x638]  ->  blr x2

which is the ExitFunctionLinker stub (Dispatcher.cpp:402-423) calling this
function through Pointers.ExitFunctionLink. That fits a `blr x2` whose x2 held
DispatchPtr. But the fault-time registers do not confirm it: x10 (TMP1) is
0x155ffe240 and x11 (TMP2) is 0, so neither of the two registers the dispatcher
branches through holds the faulting pc, and re-reading [x28+0x638] at fault time
gives 0x11f4c9b48, which is this function and is correct. Every reading left is
an inference, and inferences have had to be retracted on this bug before.

So log the call itself. Thirty-two entries answer, in one device run:

  * whether ExitFunctionLink is reached at all before the crash -- if the log is
    empty, the `blr x2` reading is wrong and the search moves elsewhere;
  * what Frame is on each call, so a stale or swapped CpuStateFrame shows up as
    a changing pointer;
  * what Frame->Pointers.DispatcherLoopTop holds, which is the value the two
    early-outs return and the value BranchOps branches through -- if it ever
    reads as DispatchPtr, that is the bug, whole;
  * what Frame->Pointers.ExitFunctionLink holds as seen from the same frame the
    stub loaded it from, rather than from the frame that survived to the fault.

Logging only. No control flow changes, so a wrong guess costs a few lines of
log rather than a device round.
"""
import sys

OLD = """uint64_t Arm64JITCore::ExitFunctionLink(FEXCore::Core::CpuStateFrame* Frame, FEXCore::Context::ExitFunctionLinkData* Record) {
  auto Thread = Frame->Thread;"""

NEW = """uint64_t Arm64JITCore::ExitFunctionLink(FEXCore::Core::CpuStateFrame* Frame, FEXCore::Context::ExitFunctionLinkData* Record) {
#ifdef FEX_IOS_HOST
  /* iOS-Madeira ml790: see tools/patch-fex-exitlink-trace.py. The 2026-09-22
   * fault is a branch to DispatchPtr with the guest's 8-mod-16 RSP, and the
   * only candidate caller is the ExitFunctionLinker stub's `blr x2`. Record
   * the frame and the two pointers it reads so the next dump can say whether
   * that is what happened instead of inferring it from stale registers. */
  {
    static std::atomic<int> IosExitLinkTrace {0};
    int IosTraceN = IosExitLinkTrace.fetch_add(1, std::memory_order_relaxed);
    if (IosTraceN < 32) {
      LogMan::Msg::EFmt("[exitlink] #{} frame={:#x} record={:#x} rip={:#x} loopTop={:#x} "
                        "exitFn={:#x} rev=ml790",
                        IosTraceN, reinterpret_cast<uint64_t>(Frame), reinterpret_cast<uint64_t>(Record),
                        Record ? Record->GuestRIP : 0, Frame->Pointers.DispatcherLoopTop,
                        Frame->Pointers.ExitFunctionLink);
    }
  }
#endif
  auto Thread = Frame->Thread;"""


def main(path):
    s = open(path).read()
    if "[exitlink] #{}" in s:
        print("::notice::fex exitlink trace already patched")
        return 0
    n = s.count(OLD)
    if n != 1:
        print(f"::error::exitlink anchor not found ({n} matches) -- upstream changed, review needed")
        return 1
    open(path, "w").write(s.replace(OLD, NEW))
    print("::notice::fex ExitFunctionLink trace added (first 32 calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
