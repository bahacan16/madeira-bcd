#!/usr/bin/env python3
"""Byte-aligned vertex fetch for D3D10-era shaders, and a census of odd vertex buffer offsets.

32-bit Crysis still draws trees and branches as long streaks with D3D10 after
the short-constant-buffer, 16-bit index realignment and vertex-fetch-bounds
fixes all fired (owner, 2026-09-30, build 262, log Crysis.exe 21:09). The same
game packs its index data at 2-byte granularity (659456 draws with a 16-bit
index offset of 2 mod 4, [idx-align]); if it packs vertex data the same way,
IASetVertexBuffers gets offsets (or strides) that are not multiples of 4. D3D
reads them byte-exact. airconv pulls attributes with loads that claim the
element's natural alignment (4 bytes for 32-bit floats), and an Apple GPU does
not honour a misaligned device load: the address is effectively rounded down
and every attribute of such a mesh is read from the wrong place.

1. airconv: while a shader model 4.x vertex shader's inputs are pulled, every
   load is emitted with alignment 1, so the GPU compiler reads byte-exact.
   MADEIRA_VFETCH_ALIGN1=0 turns it off, =1 applies it to every DXBC shader
   (God of War's SM 5.0 shaders are untouched by default). [vfetch-align]
   logs the mode once.
2. PE side (32-bit farm): IASetVertexBuffers counts bindings whose offset or
   stride is not a multiple of 4 and logs the first few ([vb-align]), so the
   log says whether the theory holds.

Needs tools/patch-dxmt-vfetch-bounds.py first (its madeira_sm4 flag).
Idempotent; fails by name if an anchor moves. Run from the repository root.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path("dxmt/src")
MARKER = "madeira-bcd: byte-aligned vertex fetch"


def edit(rel, pairs):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        print(f"{rel}: already patched")
        return
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"patch-dxmt-vb-align: anchor found {s.count(old)} times (want 1) in {rel}:\n{old}")
        s = s.replace(old, new)
    if MARKER not in s:
        sys.exit(f"patch-dxmt-vb-align: marker missing after editing {rel}")
    path.write_text(s)
    print(f"{rel}: patched")


edit("airconv/air_operations.cpp", [
    ("""AIRBuilderResult load_from_device_buffer(
  llvm::Type *value_type, pvalue base_addr, pvalue aligned_offest,
  uint32_t element_offset, uint32_t align
) {
  auto ctx = co_yield get_context();
""",
     """/* madeira-bcd: byte-aligned vertex fetch (tools/patch-dxmt-vb-align.py) -- set
 * by pull_vertex_input around an SM 4.x vertex attribute pull; builder ops run
 * synchronously on the converting thread. */
thread_local int madeira_vfetch_align1 = 0;

AIRBuilderResult load_from_device_buffer(
  llvm::Type *value_type, pvalue base_addr, pvalue aligned_offest,
  uint32_t element_offset, uint32_t align
) {
  auto ctx = co_yield get_context();
  if (madeira_vfetch_align1)
    align = 1;
"""),
])

edit("airconv/dxbc_converter_basicblock.cpp", [
    ("""\nnamespace dxmt::dxbc {\n""",
     """\nnamespace dxmt::air {
extern thread_local int madeira_vfetch_align1; /* madeira-bcd: byte-aligned vertex fetch */
}

namespace dxmt::dxbc {\n"""),
    ("""    auto vec4 = co_yield air::pull_vec4_from_addr(
      (air::MTLAttributeFormat)element_info.format, base_addr, byte_offset
    );
""",
     """    static int madeira_align_mode = -1;
    if (madeira_align_mode < 0) {
      const char *e = getenv("MADEIRA_VFETCH_ALIGN1");
      madeira_align_mode = (e && e[0] == '0') ? 0 : (e && e[0] == '1') ? 1 : 2;
      fprintf(stderr, "[vfetch-align] madeira-bcd byte-aligned vertex fetch %s\\n",
              madeira_align_mode == 0 ? "off (MADEIRA_VFETCH_ALIGN1=0)"
              : madeira_align_mode == 1 ? "on for every shader (MADEIRA_VFETCH_ALIGN1=1)"
                                        : "on for SM 4.x vertex shaders");
    }
    air::madeira_vfetch_align1 = madeira_align_mode == 1 || (madeira_align_mode == 2 && madeira_sm4);
    auto vec4 = co_yield air::pull_vec4_from_addr(
      (air::MTLAttributeFormat)element_info.format, base_addr, byte_offset
    );
    air::madeira_vfetch_align1 = 0;
"""),
])

edit("d3d11/d3d11_context_impl.cpp", [
    ("""    auto &VertexBuffers = state_.InputAssembler.VertexBuffers;
    for (unsigned slot = StartSlot; slot < StartSlot + NumBuffers; slot++) {
      auto pVertexBuffer = ppVertexBuffers[slot - StartSlot];
      if (pVertexBuffer) {
        bool replaced = false;""",
     """    auto &VertexBuffers = state_.InputAssembler.VertexBuffers;
    for (unsigned slot = StartSlot; slot < StartSlot + NumBuffers; slot++) {
      auto pVertexBuffer = ppVertexBuffers[slot - StartSlot];
      if (pVertexBuffer) {
        /* madeira-bcd: byte-aligned vertex fetch -- census of bindings the GPU
         * cannot fetch 32-bit elements from at their natural alignment. */
        {
          static unsigned madeira_vb_total, madeira_vb_odd_off, madeira_vb_odd_stride;
          UINT o = pOffsets ? pOffsets[slot - StartSlot] : 0, st = pStrides ? pStrides[slot - StartSlot] : 0;
          madeira_vb_total++;
          if (o & 3) {
            if (++madeira_vb_odd_off <= 6 || (madeira_vb_odd_off & 0xffff) == 0)
              WARN("[vb-align] madeira-bcd slot ", slot, " offset ", o, " (not 4-aligned) stride ", st,
                   " -- #", madeira_vb_odd_off, " of ", madeira_vb_total, " bindings");
          }
          if (st & 3) {
            if (++madeira_vb_odd_stride <= 6 || (madeira_vb_odd_stride & 0xffff) == 0)
              WARN("[vb-align] madeira-bcd slot ", slot, " stride ", st, " (not 4-aligned) offset ", o,
                   " -- #", madeira_vb_odd_stride, " of ", madeira_vb_total, " bindings");
          }
        }
        bool replaced = false;"""),
])

# Shaders build 262 converted (cache version 16) must be converted again.
# One past what patch-dxmt-vfetch-bounds.py set: 17 on dxmt a5e0cd3, 18 on
# db546ee (whose own version is 16).
_cache = (ROOT / "dxmt/dxmt_shader_cache.hpp").read_text()
_m = re.search(r"constexpr int kDXMTShaderCacheVersion = (\d+); /\* madeira-bcd: vertex fetch bounds \*/", _cache)
_old = _m.group(0) if _m else "constexpr int kDXMTShaderCacheVersion = <n>; /* madeira-bcd: vertex fetch bounds */"
_new = ("constexpr int kDXMTShaderCacheVersion = %d; /* madeira-bcd: vertex fetch bounds; madeira-bcd: byte-aligned vertex fetch */"
        % (int(_m.group(1)) + 1 if _m else 0))
edit("dxmt/dxmt_shader_cache.hpp", [(_old, _new)])
