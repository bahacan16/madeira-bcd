#!/usr/bin/env python3
"""Check the ARM64EC SMC-path store fast path against FEXCore's handler.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. The overlay is idempotent, refuses drift (anchors, handler body, encoding
   constants) and composes with the other Module.cpp overlays in build order.
2. Encoding algebra on the constants read from FEXCore's Arm64.cpp: no family
   that HandleUnalignedAccess emulates or backpatches can match an encoding the
   fast path claims, so the handler could only decline, return 0, or (DMB +
   STR/STUR in JIT code, half-barrier mode) return -4.
3. The overlay's own function, compiled on the host with stub types, gives the
   same Pc as that model for every case, and declines everything else.
"""
from pathlib import Path
import importlib.util
import json
import os
import random
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, root / 'tools' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


overlay = load('fastpath', 'patch-fex-ios-smc-store-fastpath.py')
history = load('history', 'patch-fex-ios-branch-history.py')
launcher = load('launcher', 'patch-fex-ios-launcher-smc.py')

module = (root / 'FEX' / overlay.MODULE).read_text()
arm64 = (root / 'FEX' / overlay.ARM64).read_text()

# 1. Idempotence and drift.
patched = overlay.patch(module, arm64)
assert patched != module and overlay.MARKER in patched
assert overlay.patch(patched, arm64) == patched


def refused(source, handler):
    try:
        overlay.patch(source, handler)
    except ValueError:
        return True
    return False


for anchor, after in overlay.EDITS:
    assert refused(module.replace(anchor, 'changed-anchor', 1), arm64), 'anchor drift accepted'
    assert refused(patched.replace(after, anchor, 1), arm64), 'partial overlay accepted'
assert refused(module, arm64.replace('  LogMan::Msg::EFmt("Unhandled JIT SIGBUS: PC:',
                                     '  LogMan::Msg::DFmt("Unhandled JIT SIGBUS: PC:', 1)), 'handler drift accepted'
assert refused(module, arm64.replace("constexpr uint32_t STUR_INST = 0b0011'1000'0000",
                                     "constexpr uint32_t STUR_INST = 0b0011'1000'0001", 1)), 'constant drift accepted'

# The call sits on the iOS SMC branch, after the byte-release-store rewrite and
# before the unchanged HandleUnalignedAccess call; the switch is read after
# HandlerConfig exists, and the function is defined before ProcessInit uses it.
smc = patched.index('const uint64_t SmcPc = NativeContext->Pc;')
call = patched.index('} else if (IosSmcStoreFastPath(Thread, *NativeContext)) {')
assert smc < patched.index('if (Ml1018IsByteRelStore) {', smc) < call
assert call < patched.index('} else if (Exception::HandleUnalignedAccess(CPUArea, *NativeContext, '
                            'CTX->IsAddressInCodeBuffer(Thread, SmcPc))) {', call)
assert patched.count('IosSmcStoreFastPath(') == 2
assert patched.index('static bool IosSmcStoreFastPath(') < patched.index('NTSTATUS ProcessInit() {')
init = patched.index('const char* SmcFastOpt = getenv("MADEIRA_FEX_SMC_FASTPATH");')
assert patched.index('Exception::HandlerConfig.emplace(*CTX);') < init < patched.index('InvalidationTracker.emplace(*CTX, Threads);')
assert patched.index('std::optional<FEX::Windows::TSOHandlerConfig> HandlerConfig;') < patched.index('static bool IosSmcStoreFastPath(')

# Build order: the other Module.cpp overlays first, then this one; every one
# stays idempotent on the combined result.
with tempfile.TemporaryDirectory() as tmp:
    copy = Path(tmp) / 'Module.cpp'
    copy.write_text(module)
    subprocess.run(['python3', str(root / 'tools/patch-fex-ios-mapview-selfshared.py'), str(copy)],
                   check=True, capture_output=True)
    combined = copy.read_text()
assert combined != module
sources = {name: (root / 'FEX' / name).read_text() for name in history.EDITS}
sources[history.MODULE] = combined
sources = history.patch(sources)
sources[history.MODULE] = launcher.patch(sources[history.MODULE])
sources[history.MODULE] = overlay.patch(sources[history.MODULE], arm64)
assert history.patch(sources) == sources
assert launcher.patch(sources[history.MODULE]) == sources[history.MODULE]
assert overlay.patch(sources[history.MODULE], arm64) == sources[history.MODULE]

build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-smc-store-fastpath.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-launcher-smc.py') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and ' Source/Windows/ARM64EC/Module.cpp' in restore[0], 'Module.cpp must be restored'
assert overlay.ARM64 not in restore[0], 'Arm64.cpp is read, never written'

# 2. Encoding algebra on FEXCore's own constants.
def constant(name):
    match = re.search(r'^constexpr uint32_t ' + name + r' = ([^;]*);', arm64, re.M)
    expression = match.group(1).replace("'", '')
    return eval(expression, {})


C = {name: constant(name) for name in (
    'CASPAL_MASK', 'CASPAL_INST', 'CASAL_MASK', 'CASAL_INST', 'ATOMIC_MEM_MASK', 'ATOMIC_MEM_INST',
    'RCPC2_MASK', 'LDAPUR_INST', 'STLUR_INST', 'LDAXP_MASK', 'LDAXP_INST', 'LDAXR_MASK', 'LDAXR_INST',
    'LDAR_INST', 'LDAPR_INST', 'STLR_INST', 'STLXR_MASK', 'STLXR_INST', 'LDSTREGISTER_MASK', 'LDR_INST',
    'STR_INST', 'LDSTUNSCALED_MASK', 'LDUR_INST', 'STUR_INST', 'DMB')}
assert C['DMB'] == 0xD5033BBF


def overlap(a, b):
    """Two (mask, value) patterns share at least one instruction word."""
    return ((a[1] ^ b[1]) & a[0] & b[0]) == 0


# Every family HandleUnalignedAccess emulates or backpatches, JIT and non-JIT.
families = {
    'CASPAL': (C['CASPAL_MASK'], C['CASPAL_INST']), 'CASAL': (C['CASAL_MASK'], C['CASAL_INST']),
    'LDAR': (C['LDAXR_MASK'], C['LDAR_INST']), 'LDAPR': (C['LDAXR_MASK'], C['LDAPR_INST']),
    'STLR': (C['LDAXR_MASK'], C['STLR_INST']), 'ATOMIC_MEM': (C['ATOMIC_MEM_MASK'], C['ATOMIC_MEM_INST']),
    'LDAXR': (C['LDAXR_MASK'], C['LDAXR_INST']), 'LDAXP': (C['LDAXP_MASK'], C['LDAXP_INST']),
    'LDAPUR': (C['RCPC2_MASK'], C['LDAPUR_INST']), 'STLUR': (C['RCPC2_MASK'], C['STLUR_INST']),
    'STLXR': (C['STLXR_MASK'], C['STLXR_INST']),
}
# The "already backpatched" check at the end of the JIT path.
final = {
    'LDR': (C['LDSTREGISTER_MASK'], C['LDR_INST']), 'LDUR': (C['LDSTUNSCALED_MASK'], C['LDUR_INST']),
    'STR': (C['LDSTREGISTER_MASK'], C['STR_INST']), 'STUR': (C['LDSTUNSCALED_MASK'], C['STUR_INST']),
    'DMB': (0xFFFFFFFF, C['DMB']),
}

add = overlay.DEF_ADD


def pattern(mask_name, inst_name):
    mask = int(re.search(mask_name + r" = (0x[0-9A-F']+)", add).group(1).replace("'", ''), 16)
    inst = int(re.search(inst_name + r" = (0x[0-9A-F']+)", add).group(1).replace("'", ''), 16)
    return mask, inst


str_imm = pattern('IosStrImmMask', 'IosStrImmInst')
stur = pattern('IosSturMask', 'IosSturInst')
str_reg = pattern('IosStrRegMask', 'IosStrRegInst')
assert stur == final['STUR'] and str_reg == final['STR'], 'fast path must use FEXCore\'s STUR/STR forms'
assert int(re.search(r"IosDmbIsh = (0x[0-9A-F']+)", add).group(1).replace("'", ''), 16) == C['DMB']
for name, family in families.items():
    for claimed in (str_imm, stur, str_reg):
        assert not overlap(claimed, family), f'{name} could match a claimed store {claimed}'
for name, check in final.items():
    assert not overlap(str_imm, check), f'STR (immediate) overlaps the backpatch check {name}'
assert not overlap(stur, final['LDUR']) and not overlap(stur, final['LDR']) and not overlap(stur, final['STR'])
assert not overlap(str_reg, final['LDUR']) and not overlap(str_reg, final['LDR']) and not overlap(str_reg, final['STUR'])
# The words seen on device: 0x39000028 (STRB, the ml1065 rewrite of STLRB) and
# 0x38000028 (STURB, the rewrite of STLURB).
for word, form in ((0x39000028, str_imm), (0x38000028, stur)):
    assert (word & form[0]) == form[1]

# The handler's JIT path, in order, as reviewed: CASPAL, CASAL, LDAR/LDAPR/STLR
# fall through to the lock, ATOMIC_MEM, LDAXR, LDAXP; under the lock LDAR/LDAPR,
# STLR, LDAPUR, STLUR are backpatched; then the final check. Pin that order.
body_start = arm64.index(overlay.HANDLER_START)
body = arm64[body_start:arm64.index('\n}\n', body_start)]
jit = body[body.index('const auto Frame = Thread->CurrentFrame;'):]
order = [jit.index(token) for token in (
    'CASPAL_MASK) == ArchHelpers::Arm64::CASPAL_INST', 'CASAL_MASK) == ArchHelpers::Arm64::CASAL_INST',
    '(Instr & LDAXR_MASK) == STLR_INST) {  // STLR*', 'ATOMIC_MEM_MASK) == ArchHelpers::Arm64::ATOMIC_MEM_INST',
    'LDAXR_MASK) == ArchHelpers::Arm64::LDAXR_INST', 'LDAXP_MASK) == ArchHelpers::Arm64::LDAXP_INST',
    'uint32_t* BPFutex', '(AtomicInst & LDSTREGISTER_MASK) == STR_INST || (AtomicInst & LDSTUNSCALED_MASK) == STUR_INST',
    'if (DMBInst == DMB) {\n        // Return handled, make sure to adjust PC so we run the DMB.\n        return -4;',
    'LogMan::Msg::EFmt("Unhandled JIT SIGBUS: PC:')]
assert order == sorted(order), 'HandleUnalignedAccess order changed'


# Reference: Pc delta HandleUnalignedAccess produces on the SMC path (None for
# "declined", which leaves Pc unchanged too), for words outside every family.
def reference(word, prev, is_jit, non_atomic):
    for family in families.values():
        if (word & family[0]) == family[1]:
            return 'other'
    if not is_jit:
        return None
    for name in ('LDR', 'LDUR'):
        check = final[name]
        if (word & check[0]) == check[1]:
            return 'other'
    if (word & final['STR'][0]) == final['STR'][1] or (word & final['STUR'][0]) == final['STUR'][1]:
        if non_atomic:
            return 0
        return -4 if prev == C['DMB'] else None
    return None


# 3. The overlay's function on the host.
harness = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <optional>
namespace FEXCore::Core { struct InternalThreadState { int Unused; }; }
namespace FEXCore::ArchHelpers::Arm64 { enum class UnalignedHandlerType { HalfBarrier, NonAtomic }; }
struct ARM64_NT_CONTEXT { uint64_t Pc; };
static bool IsJit;
static FEXCore::ArchHelpers::Arm64::UnalignedHandlerType Type;
struct FakeContext { bool IsAddressInCodeBuffer(FEXCore::Core::InternalThreadState*, uint64_t) const { return IsJit; } };
static FakeContext FakeCtx;
static FakeContext* CTX = &FakeCtx;
namespace Exception {
struct Config { FEXCore::ArchHelpers::Arm64::UnalignedHandlerType GetUnalignedHandlerType() const { return Type; } };
static std::optional<Config> HandlerConfig {Config {}};
}
#define FEX_IOS_HOST 1
''' + add + r'''
int main(int argc, char** argv) {
  IosSmcFastPath = strcmp(argv[1], "1") == 0;
  uint32_t Code[2];
  FEXCore::Core::InternalThreadState Thread {};
  unsigned long Prev, Word; int Jit, NonAtomic;
  printf("[");
  bool First = true;
  while (scanf("%lx %lx %d %d", &Word, &Prev, &Jit, &NonAtomic) == 4) {
    Code[0] = static_cast<uint32_t>(Prev);
    Code[1] = static_cast<uint32_t>(Word);
    IsJit = Jit;
    Type = NonAtomic ? FEXCore::ArchHelpers::Arm64::UnalignedHandlerType::NonAtomic
                     : FEXCore::ArchHelpers::Arm64::UnalignedHandlerType::HalfBarrier;
    ARM64_NT_CONTEXT Context {reinterpret_cast<uint64_t>(&Code[1])};
    const bool Claimed = IosSmcStoreFastPath(&Thread, Context);
    printf("%s[%d,%lld]", First ? "" : ",", Claimed, static_cast<long long>(Context.Pc - reinterpret_cast<uint64_t>(&Code[1])));
    First = false;
  }
  printf("]\n");
}
'''

rng = random.Random(1065)
words = [0x39000028, 0x38000028, 0x383F6828, 0x783F6828, 0xB83F6828, 0xF83F6828, 0xB8001028, 0xF81F8028,
         0x79000028, 0xB9000028, 0xF9000028, 0x3D800028, 0x089FFC28, 0x489FFC28, 0x19000028, 0x99000028,
         0x88E9FEC8, 0x08DFFC28, 0x38BFC028, 0x885FFC28, 0x8800FC28, 0x38208028, 0x887F8028, 0xD5033BBF,
         0x387F6828, 0x38400028, 0xA9000028, 0x52800020]
for form in (str_imm, stur, str_reg):
    for _ in range(300):
        words.append((rng.getrandbits(32) & ~form[0]) | form[1])
for _ in range(2000):
    words.append(rng.getrandbits(32))
cases = []
for word in words:
    for prev in (C['DMB'], 0xD503201F, 0x38BFC108):
        for is_jit in (1, 0):
            for non_atomic in (0, 1):
                cases.append((word, prev, is_jit, non_atomic))

with tempfile.TemporaryDirectory() as tmp:
    source = os.path.join(tmp, 'fastpath.cpp')
    binary = os.path.join(tmp, 'fastpath')
    Path(source).write_text(harness)
    compiler = os.environ.get('CXX', 'c++')
    subprocess.run([compiler, '-std=c++20', '-O1', '-Wall', '-Werror', source, '-o', binary], check=True)
    stdin = ''.join(f'{w:x} {p:x} {j} {n}\n' for w, p, j, n in cases)
    on = json.loads(subprocess.run([binary, '1'], input=stdin, capture_output=True, text=True, check=True).stdout)
    off = json.loads(subprocess.run([binary, '0'], input=stdin, capture_output=True, text=True, check=True).stdout)

claimed_total = 0
for (word, prev, is_jit, non_atomic), (claimed, delta), (off_claimed, off_delta) in zip(cases, on, off):
    assert not off_claimed and off_delta == 0, 'MADEIRA_FEX_SMC_FASTPATH=0 must leave every fault to the old path'
    expect = reference(word, prev, is_jit, non_atomic)
    in_forms = any((word & form[0]) == form[1] for form in (str_imm, stur, str_reg))
    if not in_forms:
        assert not claimed and delta == 0, f'{word:08x} claimed outside the store forms'
        continue
    assert claimed, f'{word:08x} store form not claimed'
    assert expect != 'other', f'{word:08x} is a handler family'
    assert delta == (expect or 0), f'{word:08x} prev={prev:08x} jit={is_jit} nonatomic={non_atomic}: {delta} vs {expect}'
    claimed_total += 1
assert claimed_total > 1000
print(f'SMC store fast path: {len(cases)} cases, {claimed_total} claimed with the handler\'s Pc result; '
      'encodings disjoint from every handler family; overlay idempotent, drift refused, wired before build')
