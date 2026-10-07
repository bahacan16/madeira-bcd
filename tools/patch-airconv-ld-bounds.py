#!/usr/bin/env python3
"""D3D's out-of-range rule for texture loads: a Load outside the texture reads 0.

God of War darkens -- the whole picture, smoothly, camera-independent -- at any
internal render resolution below 1920x1080 and never at 1080p (owner, builds
277-279, 2026-10-01: native 1080p fine; 720p, 1568x720, MetalFX 1.5x from
720p, and FSR 2 quality/balanced at 1080p output all darken). Winlator+DXVK at
720p does not. At a lower resolution the game creates a second set of
luminance targets (R32F 1280x720, 640x360, 40x23, RGBA32F 160x90): the 32x32
tile grid of 1280x720 is 40x22.5 -> 23 rows, so the last row of tiles reads
rows 720..735 -- past the bottom of the texture. D3D defines such a Load (ld,
ld_uav_typed) to return 0 in every component, and DXVK gives the same through
Vulkan robustness; airconv emitted a plain Metal read(), whose result out of
range is undefined, so stale or garbage texels can flow into the scene
luminance and the eye adaptation fades the picture down.

airconv now checks the coordinate (and the mip level, and the array slice)
against the texture's size at that level for 2D, 2D-array and 3D textures
(including depth) in ld and ld_uav_typed, reads at a clamped coordinate and
returns 0 when it was out of range. MADEIRA_LD_BOUNDS=0 turns it off. The
native shader cache gets its own table (salt) for each setting, so the first
launch after this converts its shaders again.

Needs tools/patch-airconv-float-experiments.py first (its cache salt).
Native only. Idempotent; fails by name if an anchor moves. Run from the
repository root.
"""
import pathlib
import sys

ROOT = pathlib.Path("dxmt/src")
MARKER = "madeira-bcd: ld bounds"

# willfaust/dxmt#8 (34b738f, dxmt db546ee): TextureAccessInBounds, the same
# check for ld, ld_uav_typed and store_uav_typed, always on (no
# MADEIRA_LD_BOUNDS=0) and with kDXMTShaderCacheVersion 16 instead of a salt.
_base = (ROOT / "airconv/nt/dxbc_converter_base.cpp").read_text()
if MARKER not in _base and "TextureAccessInBounds(" in _base:
    print("dxbc_converter_base.cpp: texture load bounds are upstream (willfaust/dxmt#8); nothing to do")
    sys.exit(0)


def edit(rel, pairs):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        print(f"{rel}: already patched")
        return
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"patch-airconv-ld-bounds: anchor found {s.count(old)} times (want 1) in {rel}:\n{old}")
        s = s.replace(old, new)
    if MARKER not in s:
        sys.exit(f"patch-airconv-ld-bounds: marker missing after editing {rel}")
    path.write_text(s)
    print(f"{rel}: patched")


HELPER = '''/* madeira-bcd: ld bounds (tools/patch-airconv-ld-bounds.py) -- D3D returns 0
 * for a texture Load outside the texture; Metal's read() is undefined there.
 * Returns the in-range condition (null when this kind is not checked) and
 * clamps Address / ArrayIndex / LOD in place so the read itself stays inside. */
static bool madeira_ld_bounds_on() {
  static int mode = -1;
  if (mode < 0) {
    const char *e = getenv("MADEIRA_LD_BOUNDS");
    mode = !(e && e[0] == '0');
    fprintf(stderr, "[ld-bounds] madeira-bcd texture load bounds check %s (MADEIRA_LD_BOUNDS)\\n", mode ? "on" : "off");
  }
  return mode;
}

static llvm::Value *
madeira_ld_in_bounds(llvm::air::AIRBuilder &air, llvm::IRBuilderBase &ir, const TextureResourceHandle &Tex,
                     llvm::Value *&Address, llvm::Value *&ArrayIndex, llvm::Value *&LOD) {
  using namespace llvm::air;
  if (!madeira_ld_bounds_on())
    return nullptr;
  unsigned dims;
  bool array = false;
  switch (Tex.Logical) {
  case Texture::texture2d:
  case Texture::depth2d:
    dims = 2;
    break;
  case Texture::texture2d_array:
  case Texture::depth2d_array:
    dims = 2;
    array = true;
    break;
  case Texture::texture3d:
    dims = 3;
    break;
  default:
    return nullptr;
  }
  auto i32 = ir.getInt32Ty();
  llvm::Value *Level = LOD ? LOD : ir.getInt32(0);
  if (Level->getType() != i32)
    return nullptr;
  auto Mips = air.CreateTextureQuery(Tex.Texture, Tex.Handle, Texture::num_mip_levels, ir.getInt32(0));
  llvm::Value *Ok = ir.CreateICmpULT(Level, Mips);
  Level = ir.CreateSelect(Ok, Level, ir.getInt32(0));
  const Texture::Query q[3] = {Texture::width, Texture::height, Texture::depth};
  for (unsigned i = 0; i < dims; i++) {
    auto Size = air.CreateTextureQuery(Tex.Texture, Tex.Handle, q[i], Level);
    auto C = ir.CreateExtractElement(Address, (uint64_t)i);
    Ok = ir.CreateAnd(Ok, ir.CreateICmpULT(C, Size));
  }
  if (array && ArrayIndex) {
    auto Len = air.CreateTextureQuery(Tex.Texture, Tex.Handle, Texture::array_length, ir.getInt32(0));
    Ok = ir.CreateAnd(Ok, ir.CreateICmpULT(ArrayIndex, Len));
    ArrayIndex = ir.CreateSelect(Ok, ArrayIndex, ir.getInt32(0));
  }
  Address = ir.CreateSelect(Ok, Address, llvm::Constant::getNullValue(Address->getType()));
  if (LOD)
    LOD = Level;
  return Ok;
}

void
Converter::operator()(const InstLoad &load) {'''

edit("airconv/nt/dxbc_converter_base.cpp", [
    ("""void
Converter::operator()(const InstLoad &load) {""", HELPER),
    # ld: clamp before the read, zero after it
    ("""  llvm::Value *LOD = LoadOperand(load.src_address, kMaskComponentW);

  auto [Value, Residency] =
      air.CreateRead(Tex->Texture, Tex->Handle, Address, ArrayIndex, SampleIndex, LOD, Tex->GlobalCoherent);

  StoreOperand(load.dst, MaskSwizzle(Value, GetMask(load.dst), Tex->Swizzle));""",
     """  llvm::Value *LOD = LoadOperand(load.src_address, kMaskComponentW);
  llvm::Value *InBounds = SampleIndex ? nullptr : madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, LOD);

  auto [Value, Residency] =
      air.CreateRead(Tex->Texture, Tex->Handle, Address, ArrayIndex, SampleIndex, LOD, Tex->GlobalCoherent);
  if (InBounds)   /* madeira-bcd: ld bounds */
    Value = ir.CreateSelect(InBounds, Value, llvm::Constant::getNullValue(Value->getType()));

  StoreOperand(load.dst, MaskSwizzle(Value, GetMask(load.dst), Tex->Swizzle));"""),
    # ld_uav_typed
    ("""  if (Tex->Texture.memory_access == Texture::acesss_readwrite)
    air.CreateTextureFence(Tex->Texture, Tex->Handle);

  auto [Value, Residency] =
      air.CreateRead(Tex->Texture, Tex->Handle, Address, ArrayIndex, SampleIndex, air.getInt(0), Tex->GlobalCoherent);
""",
     """  if (Tex->Texture.memory_access == Texture::acesss_readwrite)
    air.CreateTextureFence(Tex->Texture, Tex->Handle);

  llvm::Value *NoLOD = nullptr;
  llvm::Value *InBounds = madeira_ld_in_bounds(air, ir, *Tex, Address, ArrayIndex, NoLOD);
  auto [Value, Residency] =
      air.CreateRead(Tex->Texture, Tex->Handle, Address, ArrayIndex, SampleIndex, air.getInt(0), Tex->GlobalCoherent);
  if (InBounds)   /* madeira-bcd: ld bounds */
    Value = ir.CreateSelect(InBounds, Value, llvm::Constant::getNullValue(Value->getType()));
"""),
])

# Separate shader-cache table for the bounds setting (on by default -> new
# table, so shaders converted without the check are not reused).
edit("winemetal/unix/cache.c", [
    ("""    salt = clamp * 10 + (b && b[0] == '1');""",
     """    salt = clamp * 10 + (b && b[0] == '1');
    { const char *c = getenv("MADEIRA_LD_BOUNDS");   /* madeira-bcd: ld bounds */
      salt += (c && c[0] == '0') ? 0 : 100; }"""),
])
