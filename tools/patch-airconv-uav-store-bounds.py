#!/usr/bin/env python3
"""D3D's out-of-range rule for typed UAV stores: a write outside the texture
is dropped.

Build 282 made out-of-range texture LOADS read 0 (tools/patch-airconv-ld-
bounds.py); God of War's darkening below 1080p got only ~20 % better (owner,
log 2026-10-01 15:52:42). The new readback census shows what remains: the
16-byte value the game reads back every frame (its exposure state: a log
luminance, the exposure factor, a luminance sum) sits near x=-0.15, z=1500
outdoors, but every few tens of seconds one frame reads x=5..6, z=4000..5200
-- ~64x brighter -- and right after it the exposure factor falls (0.086 ->
0.028) and recovers over seconds: that is the darkening. Single-frame spikes
from a scene that did not change look like memory corruption, and the same
32x32 tile grid that read past the bottom of a 1280x720 target also WRITES
past it: airconv emitted store_uav_typed as a plain Metal write(), which has
no defined behaviour out of range and can land in whatever memory follows the
texture. D3D drops such writes.

store_uav_typed on 2D / 2D-array / 3D textures now checks mip 0 size and the
array length (the same helper as the loads) and skips the write when out of
range (a branch around the write). Same switch as the loads:
MADEIRA_LD_BOUNDS=0 turns both off. The cache salt for "on" moves from 100 to
200 so shaders converted by build 282 are not reused.

Needs tools/patch-airconv-ld-bounds.py first. Native only. Idempotent; fails
by name if an anchor moves. Run from the repository root.
"""
import pathlib
import sys

ROOT = pathlib.Path("dxmt/src")
MARKER = "madeira-bcd: uav store bounds"

# willfaust/dxmt#8 (34b738f, dxmt db546ee): the store is branched around
# upstream too ("uav_store_in_bounds"); see patch-airconv-ld-bounds.py.
_base = (ROOT / "airconv/nt/dxbc_converter_base.cpp").read_text()
if MARKER not in _base and "TextureAccessInBounds(" in _base and '"uav_store_in_bounds"' in _base:
    print("dxbc_converter_base.cpp: typed UAV store bounds are upstream (willfaust/dxmt#8); nothing to do")
    sys.exit(0)


def edit(rel, pairs):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        print(f"{rel}: already patched")
        return
    if "madeira-bcd: ld bounds" not in s:
        sys.exit(f"patch-airconv-uav-store-bounds: run tools/patch-airconv-ld-bounds.py first ({rel})")
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"patch-airconv-uav-store-bounds: anchor found {s.count(old)} times (want 1) in {rel}:\n{old}")
        s = s.replace(old, new)
    if MARKER not in s:
        sys.exit(f"patch-airconv-uav-store-bounds: marker missing after editing {rel}")
    path.write_text(s)
    print(f"{rel}: patched")


edit("airconv/nt/dxbc_converter_base.cpp", [
    ("""  auto Value = LoadOperand(store.src, kMaskAll);

  air.CreateWrite(Tex->Texture, Tex->Handle, Address, ArrayIndex, nullptr, ir.getInt32(0), Value, Tex->GlobalCoherent);
}""",
     """  auto Value = LoadOperand(store.src, kMaskAll);

  /* madeira-bcd: uav store bounds (tools/patch-airconv-uav-store-bounds.py) --
   * D3D drops a typed UAV write outside the texture; Metal's write() there is
   * undefined and can land in other memory. Branch around the write. */
  llvm::Value *NoLOD = nullptr;
  llvm::Value *InBounds = madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, NoLOD);
  llvm::BasicBlock *Cont = nullptr;
  if (InBounds) {
    auto *Fn = ir.GetInsertBlock()->getParent();
    auto *Then = llvm::BasicBlock::Create(air.getContext(), "uav_store_in_bounds", Fn);
    Cont = llvm::BasicBlock::Create(air.getContext(), "uav_store_done", Fn);
    ir.CreateCondBr(InBounds, Then, Cont);
    ir.SetInsertPoint(Then);
  }

  air.CreateWrite(Tex->Texture, Tex->Handle, Address, ArrayIndex, nullptr, ir.getInt32(0), Value, Tex->GlobalCoherent);

  if (Cont) {
    ir.CreateBr(Cont);
    ir.SetInsertPoint(Cont);
  }
}"""),
])

edit("winemetal/unix/cache.c", [
    ("""      salt += (c && c[0] == '0') ? 0 : 100; }""",
     """      salt += (c && c[0] == '0') ? 0 : 200; }   /* madeira-bcd: uav store bounds (was 100 in build 282) */"""),
])
