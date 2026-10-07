#!/usr/bin/env python3
"""Give the D3D10/11 (DXBC) vertex fetch D3D's out-of-range rule.

airconv pulls vertex attributes in the shader: base address + stride * index
+ element offset, straight into device memory. The table entry carries the
binding's valid length, but only the D3D9 path (dxso_compile.cpp) consults it;
the DXBC path never did. D3D10 defines a fetch past the end of the bound
vertex buffer as zero; Metal has no robustness, so such a fetch reads whatever
memory follows -- vertices flung across the screen. 32-bit Crysis draws its
trees and branches as long streaks with D3D10 and correctly with -dx9 (owner,
2026-09-30, builds 249-256), whose path clamps; the short-constant-buffer and
16-bit index realignment fixes did not change the picture.

A DXBC attribute whose byte offset lies at or past the binding's length now
reads (0,0,0,0), through the null-binding branch pull_vec4_from_addr already
has. By default only for shader model 4.x vertex shaders (D3D10-era, like
Crysis's): God of War is D3D11 with SM 5.0 shaders on this same converter, and
the owner asked that it not change. MADEIRA_VFETCH_BOUNDS=1 applies it to
every DXBC vertex shader, =0 to none (read when a shader is converted).
The shader cache version is bumped so shaders converted before this are not
reused (the committed 64-bit PE d3d11.dll keeps its own version).

Idempotent; fails by name if an anchor moves. Run from the repository root.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path("dxmt/src")
MARKER = "madeira-bcd: vertex fetch bounds"


def edit(rel, pairs):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        print(f"{rel}: already patched")
        return
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"patch-dxmt-vfetch-bounds: anchor found {s.count(old)} times (want 1) in {rel}:\n{old}")
        s = s.replace(old, new)
    if MARKER not in s:
        sys.exit(f"patch-dxmt-vfetch-bounds: marker missing after editing {rel}")
    path.write_text(s)
    print(f"{rel}: patched")


edit("airconv/dxbc_converter_basicblock.cpp", [
    ("#include <stack>\n", "#include <stack>\n#include <cstdio>\n#include <cstdlib>\n"),
    ("""IREffect pull_vertex_input(
  air::FunctionSignatureBuilder &func_signature, uint32_t to_reg, uint32_t mask,
  SM50_IA_INPUT_ELEMENT element_info, uint32_t slot_mask
) {""",
     """IREffect pull_vertex_input(
  air::FunctionSignatureBuilder &func_signature, uint32_t to_reg, uint32_t mask,
  SM50_IA_INPUT_ELEMENT element_info, uint32_t slot_mask,
  bool madeira_sm4 /* madeira-bcd: vertex fetch bounds */
) {"""),
    ("""    auto base_addr = builder.CreateExtractValue(vertex_buffer_entry, {0});
    auto stride = builder.CreateExtractValue(vertex_buffer_entry, {1});
    auto byte_offset = builder.CreateAdd(
      builder.CreateMul(stride, index),
      builder.getInt32(element_info.aligned_byte_offset)
    );
""",
     """    auto base_addr = builder.CreateExtractValue(vertex_buffer_entry, {0});
    auto stride = builder.CreateExtractValue(vertex_buffer_entry, {1});
    auto byte_offset = builder.CreateAdd(
      builder.CreateMul(stride, index),
      builder.getInt32(element_info.aligned_byte_offset)
    );
    /* madeira-bcd: vertex fetch bounds (tools/patch-dxmt-vfetch-bounds.py) --
     * D3D10 reads zero past the end of the binding; route such a fetch to the
     * null-binding branch of pull_vec4_from_addr instead of reading on.
     * Default: SM 4.x shaders only (madeira_sm4); env 1 = all, 0 = none. */
    static int madeira_vfetch_mode = -1;
    static unsigned madeira_vfetch_logged;
    if (madeira_vfetch_mode < 0) {
      const char *e = getenv("MADEIRA_VFETCH_BOUNDS");
      madeira_vfetch_mode = (e && e[0] == '0') ? 0 : (e && e[0] == '1') ? 1 : 2;
      fprintf(stderr, "[vfetch-bounds] madeira-bcd DXBC vertex fetch bounds %s\\n",
              madeira_vfetch_mode == 0 ? "off (MADEIRA_VFETCH_BOUNDS=0)"
              : madeira_vfetch_mode == 1 ? "on for every shader (MADEIRA_VFETCH_BOUNDS=1)"
                                         : "on for SM 4.x vertex shaders");
    }
    bool madeira_bounds = madeira_vfetch_mode == 1 || (madeira_vfetch_mode == 2 && madeira_sm4);
    if (madeira_bounds && ++madeira_vfetch_logged <= 4)
      fprintf(stderr, "[vfetch-bounds] madeira-bcd bounded attribute (reg %u, slot %u, #%u)\\n",
              (unsigned)element_info.reg, (unsigned)element_info.slot, madeira_vfetch_logged);
    if (madeira_bounds) {
      auto vb_length = builder.CreateExtractValue(vertex_buffer_entry, {2});
      base_addr = builder.CreateSelect(
        builder.CreateICmpULT(byte_offset, vb_length), base_addr,
        llvm::ConstantPointerNull::get(llvm::cast<llvm::PointerType>(base_addr->getType()))
      );
    }
"""),
])

edit("airconv/dxbc_converter.hpp", [
    ("""IREffect pull_vertex_input(
  air::FunctionSignatureBuilder &func_signature, uint32_t to_reg, uint32_t mask,
  SM50_IA_INPUT_ELEMENT element_info, uint32_t slot_mask
);""",
     """IREffect pull_vertex_input(
  air::FunctionSignatureBuilder &func_signature, uint32_t to_reg, uint32_t mask,
  SM50_IA_INPUT_ELEMENT element_info, uint32_t slot_mask,
  bool madeira_sm4 = false /* madeira-bcd: vertex fetch bounds */
);"""),
    ("""  microsoft::D3D10_SB_TOKENIZED_PROGRAM_TYPE shader_type;
  /* for domain shader""",
     """  microsoft::D3D10_SB_TOKENIZED_PROGRAM_TYPE shader_type;
  uint32_t madeira_sm_major = 5; /* madeira-bcd: vertex fetch bounds */
  /* for domain shader"""),
])

edit("airconv/dxbc_converter.cpp", [
    ("""  sm50_shader->shader_type = CodeParser.ShaderType();
""",
     """  sm50_shader->shader_type = CodeParser.ShaderType();
  sm50_shader->madeira_sm_major = CodeParser.ShaderMajorVersion(); /* madeira-bcd: vertex fetch bounds */
"""),
])

edit("airconv/dxbc_signature.cpp", [
    ("""                ctx.prologue << pull_vertex_input(
                  ctx.func_signature, reg, mask, ctx.ia_layout->elements[i],
                  ctx.ia_layout->slot_mask
                );""",
     """                ctx.prologue << pull_vertex_input(
                  ctx.func_signature, reg, mask, ctx.ia_layout->elements[i],
                  ctx.ia_layout->slot_mask,
                  madeira_sm4 /* madeira-bcd: vertex fetch bounds */
                );"""),
    ("""      signature_handlers.push_back(
        [=, type = (InputAttributeComponentType)sig.componentType(),
         name = sig.fullSemanticString()](SignatureContext &ctx) {
          if (ctx.ia_layout) {""",
     """      signature_handlers.push_back(
        [=, type = (InputAttributeComponentType)sig.componentType(),
         name = sig.fullSemanticString(),
         madeira_sm4 = sm50_shader->madeira_sm_major < 5](SignatureContext &ctx) {
          if (ctx.ia_layout) {"""),
])

# One past the pin's own version, whatever it is: 15 -> 16 on dxmt a5e0cd3,
# 16 -> 17 on db546ee (willfaust/dxmt#8 took 16 for its bounds checks).
_cache = (ROOT / "dxmt/dxmt_shader_cache.hpp").read_text()
_m = re.search(r"constexpr int kDXMTShaderCacheVersion = (\d+);\n", _cache)
_old = _m.group(0)[:-1] if _m else "constexpr int kDXMTShaderCacheVersion = <pin's own version>;"
_new = "constexpr int kDXMTShaderCacheVersion = %d; /* madeira-bcd: vertex fetch bounds */" % (int(_m.group(1)) + 1 if _m else 0)
edit("dxmt/dxmt_shader_cache.hpp", [(_old, _new)])
