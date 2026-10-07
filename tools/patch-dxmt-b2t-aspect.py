#!/usr/bin/env python3
"""Let a buffer->texture blit write ONE aspect of a depth-stencil texture.

winemetal's wmtcmd_blit_copy_from_buffer_to_texture has no options field, so a
copy into a Depth32Float_Stencil8 texture went through copyFromBuffer:...
toTexture: without MTLBlitOptionDepthFromDepthStencil / StencilFromDepthStencil,
which Metal leaves undefined for combined formats. Ghost of Tsushima copies its
stencil plane out to an R8 texture and back every frame; the write-back landed
across the depth and stencil memory in a grid (the black squares and the green
block pattern).

madeira_d3d12 puts the MTLBlitOption in the command's reserved[0] (always zero
before, every producer memsets its commands); this makes the local encoder pass
it on and skip the BC-pitch check for such an aspect copy, like the
texture->buffer path does (ml1102). Idempotent; fails by name if the anchor
moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: depth-stencil aspect for buffer->texture"

OLD = """      if (!texture_upload_pitch_ok(dst, body->size.width, body->bytes_per_row))
        break;
      wmt_stale_check(body->src, "blit copy src"); wmt_stale_check(body->dst, "blit copy dst");
      [encoder copyFromBuffer:(id<MTLBuffer>)body->src
                 sourceOffset:body->src_offset
            sourceBytesPerRow:body->bytes_per_row
          sourceBytesPerImage:body->bytes_per_image
                   sourceSize:MTLSizeMake(body->size.width, body->size.height, body->size.depth)
                    toTexture:dst
             destinationSlice:body->slice
             destinationLevel:body->level
            destinationOrigin:MTLOriginMake(body->origin.x, body->origin.y, body->origin.z)];
      break;"""

NEW = """      if (!body->reserved[0] && !texture_upload_pitch_ok(dst, body->size.width, body->bytes_per_row))
        break;
      wmt_stale_check(body->src, "blit copy src"); wmt_stale_check(body->dst, "blit copy dst");
      /* madeira-bcd: depth-stencil aspect for buffer->texture (reserved[0] = MTLBlitOption) */
      [encoder copyFromBuffer:(id<MTLBuffer>)body->src
                 sourceOffset:body->src_offset
            sourceBytesPerRow:body->bytes_per_row
          sourceBytesPerImage:body->bytes_per_image
                   sourceSize:MTLSizeMake(body->size.width, body->size.height, body->size.depth)
                    toTexture:dst
             destinationSlice:body->slice
             destinationLevel:body->level
            destinationOrigin:MTLOriginMake(body->origin.x, body->origin.y, body->origin.z)
                      options:(MTLBlitOption)body->reserved[0]];
      break;"""


def main():
    s = PATH.read_text()
    if MARKER in s:
        print("winemetal_unix.c: buffer->texture aspect option already present")
        return 0
    if "b2t_plane_option(" in s:
        # willfaust/dxmt#4 (45ce600), which also validates the option value
        print("winemetal_unix.c: buffer->texture aspect option is upstream (willfaust/dxmt#4); nothing to do")
        return 0
    if s.count(OLD) != 1:
        print(f"::error::{PATH}: buffer->texture blit anchor not found once -- dxmt moved, review this patch")
        return 1
    PATH.write_text(s.replace(OLD, NEW))
    print("winemetal_unix.c: buffer->texture blits take a depth/stencil aspect from reserved[0]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
