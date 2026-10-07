#!/usr/bin/env python3
"""Decide plain stores on the ARM64EC SMC path without FEX's backpatch lock.

After HandleRWXAccessViolation claims a write into tracked code, the iOS SMC
branch (ml657) calls HandleUnalignedAccess for every faulting store. For a
plain store that call can only decline: it takes the backpatch lock in the
code buffer (a CASAL and a release store into the RX view, each one a Mach
exception emulated by Wine), then prints "Unhandled JIT SIGBUS" and returns
nothing. For a store FEX has already backpatched to DMB + STR it takes the
same lock and returns -4. This overlay decides those encodings first, from the
same words and in the same way, so Pc ends where it ended before; any other
encoding (the atomics) still goes to HandleUnalignedAccess unchanged.

MADEIRA_FEX_SMC_FASTPATH=0 restores the previous path. Apply to the temporary
ARM64EC build copy; the pinned submodule is never updated. The equivalence
rests on FEXCore's HandleUnalignedAccess as pinned here, so a changed body or
encoding constant refuses the patch.
"""
from pathlib import Path
import hashlib
import sys

MARKER = "madeira-bcd: smc-store-fastpath v1"
MODULE = "Source/Windows/ARM64EC/Module.cpp"
ARM64 = "FEXCore/Source/Utils/ArchHelpers/Arm64.cpp"

# FEXCore's HandleUnalignedAccess (from its signature to its closing brace)
# and the encodings it tests, as reviewed for this overlay.
HANDLER_START = "std::optional<int32_t> HandleUnalignedAccess(FEXCore::Core::InternalThreadState* Thread"
HANDLER_SHA256 = "135ddcd3f50ba18ba33d09db811d45fc9747a3bac142af8cb2e19ba073cfda40"
CONSTANTS = (
    "constexpr uint32_t CASPAL_MASK = 0xBF'E0'FC'00;",
    "constexpr uint32_t CASPAL_INST = 0x08'60'FC'00;",
    "constexpr uint32_t CASAL_MASK = 0x3F'E0'FC'00;",
    "constexpr uint32_t CASAL_INST = 0x08'E0'FC'00;",
    "constexpr uint32_t ATOMIC_MEM_MASK = 0x3B200C00;",
    "constexpr uint32_t ATOMIC_MEM_INST = 0x38200000;",
    "constexpr uint32_t RCPC2_MASK = 0x3F'E0'0C'00;",
    "constexpr uint32_t LDAPUR_INST = 0x19'40'00'00;",
    "constexpr uint32_t STLUR_INST = 0x19'00'00'00;",
    "constexpr uint32_t LDAXP_MASK = 0xBF'FF'80'00;",
    "constexpr uint32_t LDAXP_INST = 0x88'7F'80'00;",
    "constexpr uint32_t LDAXR_MASK = 0x3F'FF'FC'00;",
    "constexpr uint32_t LDAXR_INST = 0x08'5F'FC'00;",
    "constexpr uint32_t LDAR_INST = 0x08'DF'FC'00;",
    "constexpr uint32_t LDAPR_INST = 0x38'BF'C0'00;",
    "constexpr uint32_t STLR_INST = 0x08'9F'FC'00;",
    "constexpr uint32_t STLXR_MASK = 0x3F'E0'FC'00;",
    "constexpr uint32_t STLXR_INST = 0x08'00'FC'00;",
    "constexpr uint32_t LDSTREGISTER_MASK = 0b0011'1111'1111'1111'1111'1100'0000'0000;",
    "constexpr uint32_t LDR_INST = 0b0011'1000'0111'1111'0110'1000'0000'0000;",
    "constexpr uint32_t STR_INST = 0b0011'1000'0011'1111'0110'1000'0000'0000;",
    "constexpr uint32_t LDSTUNSCALED_MASK = 0b0011'1011'1110'0000'0000'1100'0000'0000;",
    "constexpr uint32_t LDUR_INST = 0b0011'1000'0100'0000'0000'0000'0000'0000;",
    "constexpr uint32_t STUR_INST = 0b0011'1000'0000'0000'0000'0000'0000'0000;",
    "constexpr uint32_t DMB = 0b1101'0101'0000'0011'0011'0000'1011'1111 | 0b1011'0000'0000; // Inner shareable all",
)

# After this PE's DualMap::WriteOffset: CTX and Exception::HandlerConfig are
# declared above it, and ProcessInit (which reads the switch) follows.
DEF_ANCHOR = "namespace FEXCore::DualMap {\nint64_t WriteOffset = 0;\n} // namespace FEXCore::DualMap\n#endif\n"
DEF_ADD = '''#ifdef FEX_IOS_HOST
/* madeira-bcd: smc-store-fastpath v1. On the iOS SMC path (ml657 below) a
 * plain store, or one FEX already backpatched to DMB + STR, used to enter
 * HandleUnalignedAccess, which takes the backpatch lock in the code buffer
 * (a CASAL and a release store into the RX view: two Mach exceptions that
 * Wine emulates) only to decline with "Unhandled JIT SIGBUS" or to return -4.
 * Decide those encodings here from the same words, with the same result for
 * Pc; everything else still goes to HandleUnalignedAccess. The masks are
 * FEXCore's (Arm64.cpp: LDSTUNSCALED_MASK/STUR_INST, LDSTREGISTER_MASK/
 * STR_INST, DMB); the build overlay refuses a changed handler. */
static bool IosSmcFastPath = true;

static bool IosSmcStoreFastPath(FEXCore::Core::InternalThreadState* Thread, ARM64_NT_CONTEXT& Context) {
  if (!IosSmcFastPath) {
    return false;
  }
  constexpr uint32_t IosStrImmMask = 0x3FC0'0000, IosStrImmInst = 0x3900'0000;  // STR/STRB/STRH Rt, [Xn, #uimm12]
  constexpr uint32_t IosSturMask = 0x3BE0'0C00, IosSturInst = 0x3800'0000;      // STUR*, including STURB
  constexpr uint32_t IosStrRegMask = 0x3FFF'FC00, IosStrRegInst = 0x383F'6800;  // STR* [Xn, xzr], the backpatch form
  constexpr uint32_t IosDmbIsh = 0xD503'3BBF;
  const auto* Pc = reinterpret_cast<const uint32_t*>(Context.Pc);
  const uint32_t Instr = __atomic_load_n(&Pc[0], __ATOMIC_ACQUIRE);
  if ((Instr & IosStrImmMask) == IosStrImmInst) {
    return true;  // no handler family matches: it declines, Pc unchanged
  }
  if ((Instr & IosSturMask) != IosSturInst && (Instr & IosStrRegMask) != IosStrRegInst) {
    return false;  // an atomic or anything else: unchanged path
  }
  if (__atomic_load_n(&Pc[-1], __ATOMIC_ACQUIRE) != IosDmbIsh) {
    return true;  // declines, or returns 0 for NonAtomic: Pc unchanged either way
  }
  if (!CTX->IsAddressInCodeBuffer(Thread, Context.Pc)) {
    return true;  // the non-JIT path has no case for these: it declines
  }
  if (Exception::HandlerConfig->GetUnalignedHandlerType() != FEXCore::ArchHelpers::Arm64::UnalignedHandlerType::NonAtomic) {
    Context.Pc -= 4;  // backpatched DMB + STR: rerun the half-barrier, as HandleUnalignedAccess returns -4
  }
  return true;
}
#endif

'''

CALL_ANCHOR = ("        } else if (Exception::HandleUnalignedAccess(CPUArea, *NativeContext, "
               "CTX->IsAddressInCodeBuffer(Thread, SmcPc))) {\n")
CALL_ADD = '''        } else if (IosSmcStoreFastPath(Thread, *NativeContext)) {
          /* madeira-bcd: smc-store-fastpath v1: decided without the backpatch lock */
'''

INIT_ANCHOR = "  Exception::HandlerConfig.emplace(*CTX);\n"
INIT_ADD = '''#ifdef FEX_IOS_HOST
  {
    /* madeira-bcd: smc-store-fastpath v1. MADEIRA_FEX_SMC_FASTPATH=0 restores the locked path. */
    const char* SmcFastOpt = getenv("MADEIRA_FEX_SMC_FASTPATH");
    IosSmcFastPath = !(SmcFastOpt && strcmp(SmcFastOpt, "0") == 0);
    LogMan::Msg::IFmt("FEX: smc-store-fastpath-v1 madeira-bcd enabled={}", IosSmcFastPath);
  }
#endif
'''

EDITS = (
    (DEF_ANCHOR, DEF_ANCHOR + "\n" + DEF_ADD.rstrip("\n") + "\n"),
    (CALL_ANCHOR, CALL_ADD + CALL_ANCHOR),
    (INIT_ANCHOR, INIT_ANCHOR + INIT_ADD),
)


def check_handler(arm64):
    for line in CONSTANTS:
        if arm64.count(line + "\n") != 1:
            raise ValueError("HandleUnalignedAccess encoding changed: " + line.split(" = ")[0])
    start = arm64.find(HANDLER_START)
    if start < 0 or arm64.count(HANDLER_START) != 1:
        raise ValueError("HandleUnalignedAccess not found")
    body = arm64[start:arm64.index("\n}\n", start) + 3]
    if hashlib.sha256(body.encode()).hexdigest() != HANDLER_SHA256:
        raise ValueError("HandleUnalignedAccess changed; re-review the fast path")


def patch(module, arm64):
    check_handler(arm64)
    if MARKER in module:
        if any(module.count(after) != 1 for _, after in EDITS):
            raise ValueError("partial or changed SMC store fast path")
        return module
    for anchor, _ in EDITS:
        if module.count(anchor) != 1:
            raise ValueError("Module.cpp anchor changed: " + anchor.strip().splitlines()[0][:60])
    for anchor, after in EDITS:
        module = module.replace(anchor, after, 1)
    return module


if __name__ == "__main__":
    root = Path(sys.argv[1])
    path = root / MODULE
    source = path.read_text()
    try:
        result = patch(source, (root / ARM64).read_text())
    except ValueError as error:
        sys.exit("patch-fex-ios-smc-store-fastpath: " + str(error))
    if result != source:
        path.write_text(result)
    print("FEX ARM64EC: SMC-path plain and backpatched stores decided without the backpatch lock "
          "(MADEIRA_FEX_SMC_FASTPATH=0 restores the locked path)")
