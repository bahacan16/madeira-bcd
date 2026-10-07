#!/usr/bin/env python3
"""God of War below-1080p darkening: opt-in converter experiments and a DXBC
shader dump, so one device session can A/B several hypotheses and hand over
the exact shaders. Everything is OFF unless its switch is set; with no switch
set the converted shaders are byte-for-byte what the earlier patches produce.

Switches (game file: `env.NAME = 1`; each one is independent and gets its own
shader-cache table, see winemetal/unix/cache.c below):

  MADEIRA_TGSM_SYNC=1     Warp-synchronous groupshared code. (a) Port of
      upstream DXMT a4cfaac (2026-04-14, after our pin): in compute kernels a
      simdgroup barrier is inserted after a groupshared store that a later
      groupshared load depends on -- "some games expect shared memory to be
      coherent within a warp, which is not the case on Metal". (b) DXBC
      `sync_g` without `_t` (HLSL GroupMemoryBarrier(), the usual fence in a
      warp-synchronous reduction tail) emitted nothing at all; it now emits a
      simdgroup barrier with threadgroup memory scope. Luminance / histogram
      reductions are the classic place for such code.
  MADEIRA_SAMPLE_L_BIAS=1 Port of upstream eec0b65 (2026-06-01): SampleLevel
      (`sample_l`) now adds the sampler's MipLODBias, as D3D (and Vulkan, so
      DXVK) do. DXMT emulates the bias in the shader and forgot it for
      sample_l. Matters when a game biases its samplers by render scale (the
      FSR 2 case re-created God of War's samplers, log 2026-10-01 19:16:44).
  MADEIRA_PRECISE_MATH=1  No LLVM fast-math flags (contract, reassoc,
      reciprocal, approx-func, nsz) and the precise air.* math functions
      instead of air.fast_*: one switch that rules every fast-math difference
      in or out at once.
  MADEIRA_BOUNDS_EXTRA=1  The D3D out-of-range rules MADEIRA_LD_BOUNDS does
      not cover: typed UAV atomics on 2D / 2D-array / 3D textures and on typed
      buffers are skipped outside the resource (original value 0); typed
      buffer (Buffer<>/RWBuffer<>) loads read 0 and stores are dropped outside
      the view (port of upstream 3bcaf16 -- DXMT emulates the view offset, so
      an out-of-range index read or wrote the neighbouring data); resinfo
      (GetDimensions) on a mip level past the last returns 0 sizes.
  MADEIRA_SHADER_DUMP=<spec>  Writes the DXBC of the shaders the game creates
      to Documents/shader-dump/<sha1>.<type>.dxbc (MADEIRA_SHADER_DUMP_DIR
      overrides the directory). <spec>: comma list of shader types (cs, ps,
      vs, gs, hs, ds, all) and/or 8-hex-digit SHA-1 prefixes (the 8 hex digits
      in DXMT's Metal function names, e.g. shader_1a2b3c4d = compute shader
      1a2b3c4d...). Caps at 256 MB. Disassemble on a PC with
      `vkd3d-compiler -x dxbc-tpf -b d3d-asm FILE` (vkd3d >= 1.10) or
      convert with a host airconv build (docs/gow-darkening.md). Does not
      change the converted code, so it has no cache salt.

Logs: `[gow-exp] madeira-bcd converter experiments: ...` once (the switch
state), `[tgsm-sync] ...` for the first kernels that got barriers,
`[shader-dump] ...`.

Needs tools/patch-airconv-float-experiments.py, -ld-bounds.py and
-uav-store-bounds.py first. Native only (airconv + winemetal cache.c); not in
the i386 farm key. Idempotent; fails by name if an anchor moves. Run from the
repository root.
"""
import pathlib
import sys

ROOT = pathlib.Path("dxmt/src")
MARKER = "madeira-bcd: gow experiments"
NAME = "patch-airconv-gow-experiments"


# dxmt db546ee carries willfaust/dxmt#6 (lean SM50 shaders) and #8 (ld / typed
# UAV store bounds) itself, so patch-airconv-sm50-lean.py, -ld-bounds.py and
# -uav-store-bounds.py leave such a pin alone. Their upstream forms differ in
# names only: TextureAccessInBounds (always on, so no Force argument and no
# MADEIRA_LD_BOUNDS salt), `Done` for the store's join block, the shader's
# `bytecode` (kept by every SM50Initialize) for madeira_bytecode/madeira_lean.
_base = (ROOT / "airconv/nt/dxbc_converter_base.cpp").read_text()
UP_BOUNDS = "madeira-bcd: ld bounds" not in _base and "TextureAccessInBounds(" in _base
_hpp = (ROOT / "airconv/dxbc_converter.hpp").read_text()
UP_LEAN = "madeira-bcd: lean SM50 shaders" not in _hpp and "with_parsed_program(" in _hpp
UP_SALT_OLD = ("      salt += (c && c[0] == '0') ? 0 : 200; }   "
               "/* madeira-bcd: uav store bounds (was 100 in build 282) */\n")
UP_SALT_NEW = "    salt = clamp * 10 + (b && b[0] == '1');\n"
UP_LEAN_OLD = """    madeira_lean_lean++;
    madeira_lean_bytecode += (long long)BytecodeSize;
  }
"""
UP_LEAN_NEW = "  decltype(sm50_shader->signature_handlers)().swap(sm50_shader->signature_handlers);\n"


def upstream_form(rel, text):
    if UP_BOUNDS and rel == "airconv/nt/dxbc_converter_base.cpp":
        text = text.replace("madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, NoLOD, true)",
                            "TextureAccessInBounds(air, ir, *Tex, Address, ArrayIndex, NoLOD)")
        text = text.replace("madeira_ld_in_bounds(", "TextureAccessInBounds(")
        text = text.replace("  if (InBounds)   /* madeira-bcd: ld bounds */\n", "  if (InBounds)\n")
        text = text.replace("  llvm::BasicBlock *Cont = nullptr;\n", "  llvm::BasicBlock *Done = nullptr;\n")
    if UP_BOUNDS and rel == "winemetal/unix/cache.c":
        text = text.replace(UP_SALT_OLD, UP_SALT_NEW)
    if UP_LEAN and rel == "airconv/dxbc_converter.hpp":
        text = text.replace("  bool madeira_lean = false;\n", "  std::vector<uint8_t> bytecode;\n")
    if UP_LEAN and rel == "airconv/dxbc_converter.cpp":
        text = text.replace(UP_LEAN_OLD, UP_LEAN_NEW)
        text = text.replace("sh->madeira_lean", "!sh->bytecode.empty()").replace("sh->madeira_bytecode", "sh->bytecode")
        text = text.replace("they keep their DXBC (madeira_bytecode)", "they keep their DXBC (bytecode)")
    return text


def edit(rel, pairs, needs=None):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        print(f"{rel}: already patched")
        return
    if UP_BOUNDS and needs == "madeira-bcd: uav store bounds":
        needs = '"uav_store_in_bounds"' if rel.endswith(".cpp") else "madeira-bcd: float experiments"
    if UP_LEAN and needs == "madeira-bcd: lean SM50 shaders":
        needs = "with_parsed_program("
    if UP_BOUNDS:   # the Force argument: upstream's check is always on
        pairs = [p for p in pairs if "madeira_ld_bounds_on()" not in p[0]]
    pairs = [(upstream_form(rel, o), upstream_form(rel, n), w) for o, n, w in pairs]
    if needs and needs not in s:
        sys.exit(f"{NAME}: {rel} lacks '{needs}' -- run the earlier airconv patches first")
    for old, new, want in pairs:
        n = s.count(old)
        if n != want:
            sys.exit(f"{NAME}: anchor found {n} times (want {want}) in {rel}:\n{old}")
        s = s.replace(old, new)
    if MARKER not in s:
        sys.exit(f"{NAME}: marker missing after editing {rel}")
    path.write_text(s)
    print(f"{rel}: patched")


# --- shared switch helpers (air_builder.hpp is included by every user) -------
HELPERS = r'''#include "llvm/IR/IRBuilder.h"
#include <cstdio>
#include <cstdlib>

/* madeira-bcd: gow experiments (tools/patch-airconv-gow-experiments.py) --
 * opt-in converter switches, read once. The bits are the ones
 * winemetal/unix/cache.c adds to the shader-cache salt (1000 << bit). */
namespace madeira_gow {
inline bool env_on(const char *name) {
  const char *e = getenv(name);
  return e && e[0] == '1';
}
inline unsigned mask() {
  static const unsigned m = [] {
    unsigned v = (env_on("MADEIRA_TGSM_SYNC") ? 1u : 0u) | (env_on("MADEIRA_SAMPLE_L_BIAS") ? 2u : 0u) |
                 (env_on("MADEIRA_PRECISE_MATH") ? 4u : 0u) | (env_on("MADEIRA_BOUNDS_EXTRA") ? 8u : 0u);
    fprintf(stderr,
            "[gow-exp] madeira-bcd converter experiments: TGSM_SYNC=%u SAMPLE_L_BIAS=%u PRECISE_MATH=%u "
            "BOUNDS_EXTRA=%u (MADEIRA_*=1 turns one on)\n",
            v & 1u, (v >> 1) & 1u, (v >> 2) & 1u, (v >> 3) & 1u);
    return v;
  }();
  return m;
}
inline bool tgsm_sync() { return mask() & 1u; }
inline bool sample_l_bias() { return mask() & 2u; }
inline bool precise_math() { return mask() & 4u; }
inline bool bounds_extra() { return mask() & 8u; }
} // namespace madeira_gow
'''

edit("airconv/nt/air_builder.hpp", [
    ('#include "llvm/IR/IRBuilder.h"\n', HELPERS, 1),
    ("  CallInst *CreateBarrier(MemFlags Flags);\n",
     "  CallInst *CreateBarrier(MemFlags Flags, bool SimdGroup = false);   /* madeira-bcd: gow experiments */\n", 1),
])

edit("airconv/nt/air_builder.cpp", [
    ("AIRBuilder::CreateBarrier(MemFlags Flags) {", "AIRBuilder::CreateBarrier(MemFlags Flags, bool SimdGroup) {", 1),
    ('  auto Fn = getModule()->getOrInsertFunction("air.wg.barrier", FunctionType::get(getVoidTy(), Tys, false), Attrs);\n',
     '  /* madeira-bcd: gow experiments -- simdgroup variant (upstream a4cfaac) */\n'
     '  auto Fn = getModule()->getOrInsertFunction(\n'
     '      SimdGroup ? "air.simdgroup.barrier" : "air.wg.barrier", FunctionType::get(getVoidTy(), Tys, false), Attrs\n'
     '  );\n', 1),
    ('  if (FastVariant)\n    FnName += "fast_";\n',
     '  if (FastVariant && !madeira_gow::precise_math())   /* madeira-bcd: gow experiments */\n    FnName += "fast_";\n', 2),
])

edit("airconv/nt/dxbc_converter_base.hpp", [
    ("""    } else if (sync.tgsm_execution_barrier) {
      air.CreateBarrier(mem_flag);
    }
  }
""",
     """    } else if (sync.tgsm_execution_barrier) {
      air.CreateBarrier(mem_flag);
    } else if (sync.tgsm_memory_barrier && madeira_gow::tgsm_sync()) {
      /* madeira-bcd: gow experiments -- GroupMemoryBarrier() without a group
       * sync (`sync_g`) emitted nothing; warp-synchronous code relies on it
       * inside one SIMD group. */
      air.CreateBarrier(MemFlags::Threadgroup, true);
    }
  }
""", 1),
])

GUARDED = r'''
/* madeira-bcd: gow experiments -- run `emit` only when Ok (null = always);
 * outside, the result is 0, as D3D defines for an out-of-range atomic. */
template <typename F>
static llvm::Value *
madeira_guarded(llvm::air::AIRBuilder &air, llvm::IRBuilderBase &ir, llvm::Value *Ok, F &&emit) {
  if (!Ok)
    return emit();
  auto *Fn = ir.GetInsertBlock()->getParent();
  auto *Pre = ir.GetInsertBlock();
  auto *Then = llvm::BasicBlock::Create(air.getContext(), "madeira_atomic_in_bounds", Fn);
  auto *Cont = llvm::BasicBlock::Create(air.getContext(), "madeira_atomic_done", Fn);
  ir.CreateCondBr(Ok, Then, Cont);
  ir.SetInsertPoint(Then);
  llvm::Value *V = emit();
  auto *ThenEnd = ir.GetInsertBlock();
  ir.CreateBr(Cont);
  ir.SetInsertPoint(Cont);
  if (!V)
    return nullptr;
  auto *Phi = ir.CreatePHI(V->getType(), 2);
  Phi->addIncoming(V, ThenEnd);
  Phi->addIncoming(llvm::Constant::getNullValue(V->getType()), Pre);
  return Phi;
}

void
Converter::operator()(const InstLoad &load) {'''

ATOMIC_CASE_OLD = """    llvm::Value *Address = nullptr;
    llvm::Value *ArrayIndex = nullptr;

    switch (Tex->Logical) {
    case Texture::texture_buffer:
      Address = LoadOperand(atomic.dst_address, kMaskComponentX);
      Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
      break;
"""
ATOMIC_CASE_NEW = """    llvm::Value *Address = nullptr;
    llvm::Value *ArrayIndex = nullptr;
    llvm::Value *MadeiraOk = nullptr;   /* madeira-bcd: gow experiments -- atomic bounds */

    switch (Tex->Logical) {
    case Texture::texture_buffer:
      Address = LoadOperand(atomic.dst_address, kMaskComponentX);
      if (madeira_gow::bounds_extra())
        MadeiraOk = ir.CreateICmpULT(Address, DecodeTextureBufferElement(Tex->Metadata));
      Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
      break;
"""
ATOMIC_2D = """    if (madeira_gow::bounds_extra() && !MadeiraOk) {
      llvm::Value *NoLOD = nullptr;
      MadeiraOk = madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, NoLOD, true);
    }
"""

edit("airconv/nt/dxbc_converter_base.cpp", [
    # the ld-bounds helper can be forced on (atomics under BOUNDS_EXTRA)
    ("""                     llvm::Value *&Address, llvm::Value *&ArrayIndex, llvm::Value *&LOD) {
  using namespace llvm::air;
  if (!madeira_ld_bounds_on())
    return nullptr;
""",
     """                     llvm::Value *&Address, llvm::Value *&ArrayIndex, llvm::Value *&LOD, bool Force = false) {
  using namespace llvm::air;
  if (!Force && !madeira_ld_bounds_on())   /* madeira-bcd: gow experiments -- Force: BOUNDS_EXTRA atomics */
    return nullptr;
""", 1),
    ("""
void
Converter::operator()(const InstLoad &load) {""", GUARDED, 1),
    # ld on a typed buffer
    ("""  llvm::Constant *Offset = nullptr;

  switch (Tex->Logical) {
  case Texture::texture_buffer:
    Address = LoadOperand(load.src_address, kMaskComponentX);
    Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
    Offset = air.getInt(load.offsets[0]);
    break;
""",
     """  llvm::Constant *Offset = nullptr;
  llvm::Value *MadeiraTbOk = nullptr;   /* madeira-bcd: gow experiments -- typed buffer bounds */

  switch (Tex->Logical) {
  case Texture::texture_buffer:
    Address = LoadOperand(load.src_address, kMaskComponentX);
    Offset = air.getInt(load.offsets[0]);
    if (madeira_gow::bounds_extra()) {
      MadeiraTbOk = ir.CreateICmpULT(ir.CreateAdd(Address, Offset), DecodeTextureBufferElement(Tex->Metadata));
      Address = ir.CreateSelect(MadeiraTbOk, Address, ir.CreateNeg(Offset));
    }
    Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
    break;
""", 1),
    # ld_uav_typed on a typed buffer
    ("""  llvm::Value *SampleIndex = nullptr;

  switch (Tex->Logical) {
  case Texture::texture_buffer:
    Address = LoadOperand(load.src_address, kMaskComponentX);
    Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
    break;
""",
     """  llvm::Value *SampleIndex = nullptr;
  llvm::Value *MadeiraTbOk = nullptr;   /* madeira-bcd: gow experiments -- typed buffer bounds */

  switch (Tex->Logical) {
  case Texture::texture_buffer:
    Address = LoadOperand(load.src_address, kMaskComponentX);
    if (madeira_gow::bounds_extra()) {
      MadeiraTbOk = ir.CreateICmpULT(Address, DecodeTextureBufferElement(Tex->Metadata));
      Address = ir.CreateSelect(MadeiraTbOk, Address, ir.getInt32(0));
    }
    Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
    break;
""", 1),
    # both loads: zero the result outside a typed buffer
    ("""  if (InBounds)   /* madeira-bcd: ld bounds */
    Value = ir.CreateSelect(InBounds, Value, llvm::Constant::getNullValue(Value->getType()));
""",
     """  if (InBounds)   /* madeira-bcd: ld bounds */
    Value = ir.CreateSelect(InBounds, Value, llvm::Constant::getNullValue(Value->getType()));
  if (MadeiraTbOk)   /* madeira-bcd: gow experiments -- typed buffer bounds */
    Value = ir.CreateSelect(MadeiraTbOk, Value, llvm::Constant::getNullValue(Value->getType()));
""", 2),
    # store_uav_typed on a typed buffer
    ("""  auto Tex = LoadTexture(store.dst);
  if (!Tex.hasValue())
    return;
  llvm::Value *Address = nullptr;
  llvm::Value *ArrayIndex = nullptr;

  switch (Tex->Logical) {
  case Texture::texture_buffer:
    Address = LoadOperand(store.src_address, kMaskComponentX);
    Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
    break;
""",
     """  auto Tex = LoadTexture(store.dst);
  if (!Tex.hasValue())
    return;
  llvm::Value *Address = nullptr;
  llvm::Value *ArrayIndex = nullptr;
  llvm::Value *MadeiraTbOk = nullptr;   /* madeira-bcd: gow experiments -- typed buffer bounds */

  switch (Tex->Logical) {
  case Texture::texture_buffer:
    Address = LoadOperand(store.src_address, kMaskComponentX);
    if (madeira_gow::bounds_extra())
      MadeiraTbOk = ir.CreateICmpULT(Address, DecodeTextureBufferElement(Tex->Metadata));
    Address = ir.CreateAdd(Address, DecodeTextureBufferOffset(Tex->Metadata));
    break;
""", 1),
    ("""  llvm::Value *InBounds = madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, NoLOD);
  llvm::BasicBlock *Cont = nullptr;
""",
     """  llvm::Value *InBounds = madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, NoLOD);
  if (!InBounds && MadeiraTbOk)   /* madeira-bcd: gow experiments -- typed buffer bounds */
    InBounds = MadeiraTbOk;
  llvm::BasicBlock *Cont = nullptr;
""", 1),
    # SampleLevel + sampler MipLODBias (upstream eec0b65)
    ("""  llvm::Value *LOD = LoadOperand(sample.src_lod, kMaskComponentX);

  auto [Value, Residency] =
      air.CreateSample(Tex->Texture, Tex->Handle, SamplerHandle, Coord, ArrayIndex, sample.offsets, sample_level{LOD});
""",
     """  llvm::Value *LOD = LoadOperand(sample.src_lod, kMaskComponentX);
  if (madeira_gow::sample_l_bias())   /* madeira-bcd: gow experiments -- upstream eec0b65 */
    LOD = ir.CreateFAdd(LOD, Sampler->Bias);

  auto [Value, Residency] =
      air.CreateSample(Tex->Texture, Tex->Handle, SamplerHandle, Coord, ArrayIndex, sample.offsets, sample_level{LOD});
""", 1),
    # resinfo on a mip level past the last
    ("""  switch (resinfo.modifier) {
  case InstResourceInfo::M::none: {
""",
     """  if (madeira_gow::bounds_extra() && !llvm::isa<llvm::Constant>(MipCount)) {   /* madeira-bcd: gow experiments */
    auto *MipOk = ir.CreateICmpULT(Level, MipCount);
    X = ir.CreateSelect(MipOk, X, ir.getInt32(0));
    Y = ir.CreateSelect(MipOk, Y, ir.getInt32(0));
    if (Tex->Logical == Texture::texture3d)
      Z = ir.CreateSelect(MipOk, Z, ir.getInt32(0));
  }
  switch (resinfo.modifier) {
  case InstResourceInfo::M::none: {
""", 1),
    # typed UAV atomics: both atomic instructions share the case code
    (ATOMIC_CASE_OLD, ATOMIC_CASE_NEW, 2),
    ("""    default:
      return;
    }
    auto Value =
        air.CreateAtomicRMW(Tex->Texture, Tex->Handle, Op, Address, LoadOperand(atomic.src, kMaskAll), ArrayIndex);
    StoreOperand(atomic.dst_original, Value);
""",
     """    default:
      return;
    }
""" + ATOMIC_2D + """    auto Value = madeira_guarded(air, ir, MadeiraOk, [&]() -> llvm::Value * {
      return air.CreateAtomicRMW(Tex->Texture, Tex->Handle, Op, Address, LoadOperand(atomic.src, kMaskAll), ArrayIndex);
    });
    StoreOperand(atomic.dst_original, Value);
""", 1),
    ("""    default:
      return;
    }
    auto [Value, Flag_DISCARDED] = air.CreateAtomicCmpXchg(
        Tex->Texture, Tex->Handle, Address, LoadOperand(atomic.src0, kMaskAll), LoadOperand(atomic.src1, kMaskAll),
        ArrayIndex
    );
    StoreOperand(atomic.dst, Value);
""",
     """    default:
      return;
    }
""" + ATOMIC_2D + """    auto Value = madeira_guarded(air, ir, MadeiraOk, [&]() -> llvm::Value * {
      auto [V, Flag_DISCARDED] = air.CreateAtomicCmpXchg(
          Tex->Texture, Tex->Handle, Address, LoadOperand(atomic.src0, kMaskAll), LoadOperand(atomic.src1, kMaskAll),
          ArrayIndex
      );
      return V;
    });
    StoreOperand(atomic.dst, Value);
""", 1),
    # PRECISE_MATH: no fast-math flags at all
    ("""Converter::UseFastMath(bool OptOut) {
  if (OptOut)
    return nullptr;
""",
     """Converter::UseFastMath(bool OptOut) {
  if (OptOut || madeira_gow::precise_math())   /* madeira-bcd: gow experiments */
    return nullptr;
""", 1),
], needs="madeira-bcd: uav store bounds")

# --- the simdgroup barrier pass (upstream a4cfaac, inline instead of a new
# file so build/dxmt-ios/build.sh needs no change) ----------------------------
PASS = r'''#include "transforms/lower_16bit_texread.hpp"
/* madeira-bcd: gow experiments -- MADEIRA_TGSM_SYNC: port of upstream DXMT
 * a4cfaac "insert simdgroup barrier between consecutive threadgroup memory
 * read-after-write" (src/airconv/transforms/simdgroup_implicit_membarrier.cpp).
 * Some games expect groupshared memory to be coherent within a warp; on Metal
 * that needs simdgroup_barrier(). Compute kernels only. */
#include "llvm/ADT/DenseSet.h"
#include "llvm/Analysis/MemorySSA.h"
#include "nt/air_builder.hpp"
#include <atomic>
namespace {
class MadeiraSimdgroupImplicitMemBarrierPass : public llvm::PassInfoMixin<MadeiraSimdgroupImplicitMemBarrierPass> {
public:
  llvm::PreservedAnalyses
  run(llvm::Function &F, llvm::FunctionAnalysisManager &AM) {
    using namespace llvm;
    if (!F.getParent()->getNamedMetadata("air.kernel"))
      return PreservedAnalyses::all();
    unsigned Inserted = 0;
    SmallVector<LoadInst *, 4> ToCheck;
    SmallDenseSet<Instruction *, 4> Fenced;
    auto &MemSSA = AM.getResult<MemorySSAAnalysis>(F).getMSSA();
    for (auto &BB : F)
      for (auto &Inst : BB)
        if (auto Load = dyn_cast<LoadInst>(&Inst))
          if (Load->getPointerAddressSpace() == 3)
            ToCheck.push_back(Load);
    for (auto *Load : ToCheck) {
      auto MA = MemSSA.getMemoryAccess(Load);
      if (!MA)
        continue;
      auto DefAccess = MA->getDefiningAccess();
      if (auto Def = dyn_cast_or_null<MemoryDef>(DefAccess)) {
        auto DefInst = Def->getMemoryInst();
        if (!DefInst || Fenced.contains(DefInst))
          continue;
        if (auto Store = dyn_cast<StoreInst>(DefInst)) {
          if (Store->getPointerAddressSpace() == 3) {
            Fenced.insert(DefInst);
            IRBuilder<> builder(Store->getParent());
            if (Store->getNextNode())
              builder.SetInsertPoint(Store->getNextNode());
            air::AIRBuilder ab(builder, nulls());
            ab.CreateBarrier(air::MemFlags::Threadgroup, true);
            Inserted++;
          }
        }
      }
    }
    if (Inserted) {
      static std::atomic<unsigned> kernels{0};
      unsigned k = ++kernels;
      if (k <= 24 || (k & 63) == 0)
        fprintf(stderr, "[tgsm-sync] madeira-bcd %s: %u simdgroup barrier(s) after groupshared stores (kernel #%u)\n",
                F.getName().str().c_str(), Inserted, k);
    }
    return Inserted ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }
};
} // namespace
'''

edit("airconv/airconv_context.cpp", [
    ('#include "transforms/lower_16bit_texread.hpp"\n', PASS, 1),
    ("    GlobalCleanupPM.addPass(air::Lower16BitTexReadPass());\n",
     "    GlobalCleanupPM.addPass(air::Lower16BitTexReadPass());\n"
     "    if (madeira_gow::tgsm_sync())   /* madeira-bcd: gow experiments */\n"
     "      GlobalCleanupPM.addPass(MadeiraSimdgroupImplicitMemBarrierPass());\n", 1),
])

# --- DXBC dump ---------------------------------------------------------------
DUMP = r'''/* madeira-bcd: gow experiments -- MADEIRA_SHADER_DUMP: write the DXBC of the
 * shaders a game creates (tools/patch-airconv-gow-experiments.py). The file
 * name is the SHA-1 of the bytecode, the same hash DXMT puts (first 8 hex
 * digits) into its Metal function names: shader_<sha8> (compute),
 * ps_<sha8>_..., vs_<sha8>_... */
#include <cctype>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <mutex>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <unordered_map>

static void
madeira_sha1(const uint8_t *data, size_t len, uint8_t out[20]) {
  uint32_t h[5] = {0x67452301u, 0xEFCDAB89u, 0x98BADCFEu, 0x10325476u, 0xC3D2E1F0u};
  auto rol = [](uint32_t v, int s) { return (v << s) | (v >> (32 - s)); };
  uint64_t bits = (uint64_t)len * 8;
  size_t total = ((len + 8) / 64 + 1) * 64;
  for (size_t off = 0; off < total; off += 64) {
    uint8_t blk[64];
    for (int i = 0; i < 64; i++) {
      size_t p = off + i;
      uint8_t b;
      if (p < len)
        b = data[p];
      else if (p == len)
        b = 0x80;
      else if (p >= total - 8)
        b = (uint8_t)(bits >> (8 * (total - 1 - p)));
      else
        b = 0;
      blk[i] = b;
    }
    uint32_t w[80];
    for (int i = 0; i < 16; i++)
      w[i] = (uint32_t)blk[4 * i] << 24 | (uint32_t)blk[4 * i + 1] << 16 | (uint32_t)blk[4 * i + 2] << 8 |
             (uint32_t)blk[4 * i + 3];
    for (int i = 16; i < 80; i++)
      w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
    for (int i = 0; i < 80; i++) {
      uint32_t f, k;
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5A827999u; }
      else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1u; }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDCu; }
      else { f = b ^ c ^ d; k = 0xCA62C1D6u; }
      uint32_t t = rol(a, 5) + f + e + k + w[i];
      e = d; d = c; c = rol(b, 30); b = a; a = t;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
  }
  for (int i = 0; i < 5; i++) {
    out[4 * i] = (uint8_t)(h[i] >> 24); out[4 * i + 1] = (uint8_t)(h[i] >> 16);
    out[4 * i + 2] = (uint8_t)(h[i] >> 8); out[4 * i + 3] = (uint8_t)h[i];
  }
}

static const char *const madeira_dump_tname[6] = {"ps", "vs", "gs", "hs", "ds", "cs"};
static std::mutex madeira_dump_mu;

/* Documents/shader-dump (MADEIRA_SHADER_DUMP_DIR overrides); "" = none. */
static const std::string &
madeira_dump_dir() {
  static const std::string dir = [] {
    std::string d;
    const char *e = getenv("MADEIRA_SHADER_DUMP_DIR");
    if (e && *e) {
      d = e;
    } else {
      const char *docs = getenv("MADEIRA_DOCS_DIR");
      if (docs && *docs) d = docs;
      else if ((docs = getenv("CFFIXED_USER_HOME")) && *docs) d = std::string(docs) + "/Documents";
      else if ((docs = getenv("HOME")) && *docs) d = std::string(docs) + "/Documents";
      else return std::string();
      d += "/shader-dump";
    }
    mkdir(d.c_str(), 0755);
    return d;
  }();
  return dir;
}

/* Write one shader (caller holds madeira_dump_mu). 1 written, 0 already there
 * or capped, -1 error. */
static int
madeira_dump_write(const void *bc, size_t size, unsigned type, const char *hex, const char *why) {
  static unsigned long long bytes = 0;
  static unsigned files = 0;
  const std::string &dir = madeira_dump_dir();
  if (dir.empty() || !bc || !size)
    return -1;
  if (bytes + size > (256ull << 20)) {
    static bool told;
    if (!told) {
      told = true;
      fprintf(stderr, "[shader-dump] madeira-bcd stopped at the 256 MB cap (%u files)\n", files);
    }
    return 0;
  }
  std::string path = dir + "/" + hex + "." + (type < 6 ? madeira_dump_tname[type] : "xx") + ".dxbc";
  int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0644);
  if (fd < 0)
    return errno == EEXIST ? 0 : -1;
  ssize_t w = write(fd, bc, size);
  close(fd);
  if (w != (ssize_t)size)
    return -1;
  bytes += size;
  files++;
  if (files <= 3 || files % 200 == 0)
    fprintf(stderr, "[shader-dump] madeira-bcd %u files, %llu KB in %s (latest %.8s.%s, %s)\n", files, bytes >> 10,
            dir.c_str(), hex, type < 6 ? madeira_dump_tname[type] : "xx", why);
  return 1;
}

static void
madeira_hex(const uint8_t dg[20], char hex[41]) {
  for (int i = 0; i < 20; i++)
    snprintf(hex + 2 * i, 3, "%02x", dg[i]);
}

/* Live shaders by the first 4 bytes of their SHA-1 (= DXMT's 8 hex digits),
 * kept while MADEIRA_GPU_TRACE is set so the frame trace can dump exactly the
 * shaders a frame uses (madeira_airconv_dump_function). Lean shaders only:
 * they keep their DXBC (madeira_bytecode). */
static std::unordered_map<uint32_t, dxmt::dxbc::SM50ShaderInternal *> madeira_reg;

static bool
madeira_reg_on() {
  static const bool on = [] {
    const char *e = getenv("MADEIRA_GPU_TRACE");
    return e && atof(e) > 0;
  }();
  return on;
}

static void
madeira_shader_dump(const void *bc, size_t size, unsigned type, dxmt::dxbc::SM50ShaderInternal *sh) {
  static int state = -1; /* -1 unread, 0 off, 1 on */
  static unsigned types = 0;
  static std::string prefixes;
  std::lock_guard<std::mutex> lock(madeira_dump_mu);
  if (state < 0) {
    state = 0;
    const char *spec = getenv("MADEIRA_SHADER_DUMP");
    if (spec && *spec && strcmp(spec, "0")) {
      std::string s(spec);
      size_t i = 0;
      while (i < s.size()) {
        size_t j = s.find_first_of(", ;", i);
        if (j == std::string::npos)
          j = s.size();
        std::string tok = s.substr(i, j - i);
        i = j + 1;
        if (tok.empty())
          continue;
        if (tok == "all") { types = 0x3f; continue; }
        bool named = false;
        for (unsigned t = 0; t < 6; t++)
          if (tok == madeira_dump_tname[t]) { types |= 1u << t; named = true; }
        if (!named && tok.size() >= 4 && tok.size() <= 40 &&
            tok.find_first_not_of("0123456789abcdefABCDEF") == std::string::npos) {
          for (auto &ch : tok)
            ch = (char)tolower((unsigned char)ch);
          prefixes += " " + tok;
        }
      }
      state = (types || !prefixes.empty()) ? 1 : 0;
      fprintf(stderr, "[shader-dump] madeira-bcd MADEIRA_SHADER_DUMP=%s -> %s (types 0x%x, prefixes:%s)\n", spec,
              state ? madeira_dump_dir().c_str() : "nothing to dump", types,
              prefixes.empty() ? " none" : prefixes.c_str());
    }
    if (madeira_reg_on())
      fprintf(stderr, "[shader-dump] madeira-bcd shaders a traced frame uses go to %s (MADEIRA_GPU_TRACE)%s\n",
              madeira_dump_dir().c_str(), sh && sh->madeira_lean ? "" : " -- needs lean SM50 shaders (MADEIRA_SM50_LEAN)");
  }
  bool reg = madeira_reg_on() && sh && sh->madeira_lean;
  if ((state != 1 && !reg) || !bc || !size)
    return;
  uint8_t dg[20];
  madeira_sha1((const uint8_t *)bc, size, dg);
  char hex[41];
  madeira_hex(dg, hex);
  if (reg) {
    uint32_t key = (uint32_t)dg[0] << 24 | (uint32_t)dg[1] << 16 | (uint32_t)dg[2] << 8 | dg[3];
    sh->madeira_sha8 = key;
    madeira_reg[key] = sh;
  }
  if (state != 1)
    return;
  bool want = type < 6 && (types & (1u << type));
  if (!want && !prefixes.empty()) {
    size_t p = 0;
    while (!want && (p = prefixes.find(' ', p)) != std::string::npos) {
      size_t q = prefixes.find(' ', p + 1);
      std::string pre = prefixes.substr(p + 1, (q == std::string::npos ? prefixes.size() : q) - p - 1);
      want = !pre.empty() && !strncmp(hex, pre.c_str(), pre.size());
      p = p + 1;
    }
  }
  if (want)
    madeira_dump_write(bc, size, type, hex, "created");
}

static void
madeira_reg_remove(dxmt::dxbc::SM50ShaderInternal *sh) {
  if (!sh || !sh->madeira_sha8)
    return;
  std::lock_guard<std::mutex> lock(madeira_dump_mu);
  auto it = madeira_reg.find(sh->madeira_sha8);
  if (it != madeira_reg.end() && it->second == sh)
    madeira_reg.erase(it);
}

/* For winemetal's frame trace: dump the DXBC behind a Metal function name
 * DXMT generated (shader_<sha8>, ps_<sha8>_<variant>, vs_..., ...).
 * 1 written, 0 already dumped, -1 unknown. */
extern "C" int
madeira_airconv_dump_function(const char *fn) {
  if (!fn)
    return -1;
  const char *u = strchr(fn, '_');
  if (!u || strlen(u + 1) < 8)
    return -1;
  char h8[9];
  memcpy(h8, u + 1, 8);
  h8[8] = 0;
  if (strspn(h8, "0123456789abcdef") != 8)
    return -1;
  uint32_t key = (uint32_t)strtoul(h8, nullptr, 16);
  std::lock_guard<std::mutex> lock(madeira_dump_mu);
  auto it = madeira_reg.find(key);
  if (it == madeira_reg.end())
    return -1;
  auto *sh = it->second;
  const auto &bc = sh->madeira_bytecode;
  if (bc.empty())
    return -1;
  uint8_t dg[20];
  madeira_sha1(bc.data(), bc.size(), dg);
  char hex[41];
  madeira_hex(dg, hex);
  return madeira_dump_write(bc.data(), bc.size(), (unsigned)sh->shader_type, hex, "traced frame");
}

AIRCONV_API int SM50Initialize('''

edit("airconv/dxbc_converter.hpp", [
    ("  bool madeira_lean = false;\n",
     "  bool madeira_lean = false;\n"
     "  uint32_t madeira_sha8 = 0;   /* madeira-bcd: gow experiments -- registry key (MADEIRA_GPU_TRACE) */\n", 1),
], needs="madeira-bcd: lean SM50 shaders")

edit("airconv/dxbc_converter.cpp", [
    ("AIRCONV_API int SM50Initialize(", DUMP, 1),
    # the lean wrapper: after it decided to keep the bytecode, so the registry
    # sees madeira_lean
    ("""    madeira_lean_lean++;
    madeira_lean_bytecode += (long long)BytecodeSize;
  }
""",
     """    madeira_lean_lean++;
    madeira_lean_bytecode += (long long)BytecodeSize;
  }
  madeira_shader_dump(pBytecode, BytecodeSize, (unsigned)sm50_shader->shader_type, sm50_shader);   /* madeira-bcd: gow experiments */
""", 1),
    ("AIRCONV_API void SM50Destroy(sm50_shader_t pShader) {\n",
     "AIRCONV_API void SM50Destroy(sm50_shader_t pShader) {\n"
     "  madeira_reg_remove((dxmt::dxbc::SM50ShaderInternal *)pShader);   /* madeira-bcd: gow experiments */\n", 1),
], needs="madeira-bcd: lean SM50 shaders")

# --- one shader-cache table per experiment combination -----------------------
edit("winemetal/unix/cache.c", [
    ("""      salt += (c && c[0] == '0') ? 0 : 200; }   /* madeira-bcd: uav store bounds (was 100 in build 282) */
""",
     """      salt += (c && c[0] == '0') ? 0 : 200; }   /* madeira-bcd: uav store bounds (was 100 in build 282) */
    { static const char *const gow[] = {"MADEIRA_TGSM_SYNC", "MADEIRA_SAMPLE_L_BIAS", "MADEIRA_PRECISE_MATH",
                                        "MADEIRA_BOUNDS_EXTRA"};   /* madeira-bcd: gow experiments */
      for (int i = 0; i < 4; i++) {
        const char *v = getenv(gow[i]);
        if (v && v[0] == '1') salt += 1000 << i;
      } }
""", 1),
], needs="madeira-bcd: uav store bounds")
