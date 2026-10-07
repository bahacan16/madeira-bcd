#!/usr/bin/env python3
"""DXIL tessellation through the Metal Shader Converter's emulation.

madeira_d3d12 converts a DXIL hull+domain pipeline the way Apple's
IRRuntimeNewGeometryTessellationEmulationPipeline expects (vertex shader as the
object function with tessellation on, hull shader = hull function + tessellator,
domain shader + the converter's passthrough geometry shader in the mesh stage).
Ghost of Tsushima draws its water this way; the runtime had no such pipeline, so
the water was missing.

This patch gives winemetal what the runtime needs for it:
- winemetal.h: WMTGeometryEmulationInfo's reserved words carry the hull and
  domain libraries, the maximum tessellation factor and a tessellation flag
  (same size and offsets as before; every producer memsets the struct).
- winemetal_unix.c: the local geometry-emulation pipeline builder builds the
  tessellation variant when the flag is set: object function
  "<vs>.dxil_irconverter_object_shader" with tessellationEnabled = true,
  "irconverter_hull_shader" and "irconverter_tessellator" with
  vertex_shader_output_size_fc / max_tessellation_factor_fc, linked into the
  object stage with the stage-in function; "irconverter_dxil_domain_shader"
  linked into the mesh stage, whose function is the passthrough geometry shader.
- winemetal_unix.c: a mesh draw whose reserved[1] is 0x7e55 sets the object
  threadgroup memory length from reserved[0] first (the converter's draw helpers
  set 15360 bytes at index 0). Every producer memsets its commands, so reserved
  was zero before. The indirect mesh draw does the same (indirect DXIL
  tessellation draws, whose grid a helper kernel computes on the GPU).

Idempotent; fails by name if an anchor moves. Run from the repository root.
"""
import pathlib
import sys

HDR = pathlib.Path("dxmt/src/winemetal/winemetal.h")
UNIX = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: DXIL tessellation"

HDR_OLD = """  uint32_t gs_vertex_size_bytes;
  uint32_t gs_max_input_primitives;
  uint32_t reserved[6];
};"""

HDR_NEW = """  uint32_t gs_vertex_size_bytes;
  uint32_t gs_max_input_primitives;
  /* madeira-bcd: DXIL tessellation (tools/patch-dxmt-dxil-tess.py); was reserved[6] */
  obj_handle_t hull_library;
  obj_handle_t domain_library;
  float max_tessellation_factor;
  uint32_t tessellation;
};"""

BUILD_OLD = """    d.rasterSampleCount = i->raster_sample_count ? i->raster_sample_count : 1;

    cv = [[[MTLFunctionConstantValues alloc] init] autorelease];
    fsi = [[Ls newFunctionWithName:Ls.functionNames.firstObject] autorelease];"""

BUILD_NEW = """    d.rasterSampleCount = i->raster_sample_count ? i->raster_sample_count : 1;

    if (g->tessellation) {
      /* madeira-bcd: DXIL tessellation, built exactly as the converter runtime's
       * IRRuntimeNewGeometryTessellationEmulationPipeline builds it. */
      id<MTLLibrary> Lh = (id<MTLLibrary>)g->hull_library, Ld = (id<MTLLibrary>)g->domain_library;
      id<MTLFunction> th = nil, tt = nil, td = nil, tm = nil;
      MTLFunctionConstantValues *vc = [[[MTLFunctionConstantValues alloc] init] autorelease];
      MTLFunctionConstantValues *hc = [[[MTLFunctionConstantValues alloc] init] autorelease];
      BOOL on = YES;
      float mtf = g->max_tessellation_factor;
      if (!Lh || !Ld) {
        fprintf(stderr, "[winemetal] DXIL tessellation pipeline: missing library (hull %d domain %d)\\n", !!Lh, !!Ld);
        return STATUS_SUCCESS;
      }
      fsi = [[Ls newFunctionWithName:Ls.functionNames.firstObject] autorelease];
      [vc setConstantValue:&on type:MTLDataTypeBool withName:@"tessellationEnabled"];
      e = nil;
      fo = [[Lv newFunctionWithName:[NSString stringWithFormat:@"%s.dxil_irconverter_object_shader", g->vertex_function]
                     constantValues:vc error:&e] autorelease];
      if (!fo)
        fprintf(stderr, "[winemetal] DXIL tessellation pipeline: object function '%s.dxil_irconverter_object_shader': %s\\n",
                g->vertex_function, e ? [[e localizedDescription] UTF8String] : "?");
      [hc setConstantValue:&vsz type:MTLDataTypeInt withName:@"vertex_shader_output_size_fc"];
      [hc setConstantValue:&mtf type:MTLDataTypeFloat withName:@"max_tessellation_factor_fc"];
      e = nil;
      th = [[Lh newFunctionWithName:@"irconverter_hull_shader" constantValues:hc error:&e] autorelease];
      if (!th)
        fprintf(stderr, "[winemetal] DXIL tessellation pipeline: hull function: %s\\n", e ? [[e localizedDescription] UTF8String] : "?");
      e = nil;
      tt = [[Lh newFunctionWithName:@"irconverter_tessellator" constantValues:hc error:&e] autorelease];
      if (!tt)
        fprintf(stderr, "[winemetal] DXIL tessellation pipeline: tessellator function: %s\\n", e ? [[e localizedDescription] UTF8String] : "?");
      td = [[Ld newFunctionWithName:@"irconverter_dxil_domain_shader"] autorelease];
      tm = [[Ld newFunctionWithName:[NSString stringWithUTF8String:g->geometry_function]] autorelease];
      if (g->fragment_function[0])
        ff = [[Lf newFunctionWithName:[NSString stringWithUTF8String:g->fragment_function]] autorelease];
      if (!fsi || !fo || !th || !tt || !td || !tm || (g->fragment_function[0] && !ff)) {
        fprintf(stderr, "[winemetal] DXIL tessellation pipeline REFUSED: stage-in %d object %d hull %d tessellator %d domain %d "
                        "mesh '%s' %d fragment %d\\n", !!fsi, !!fo, !!th, !!tt, !!td, g->geometry_function, !!tm, !!ff);
        return STATUS_SUCCESS;
      }
      d.objectFunction = fo;
      d.meshFunction = tm;
      d.fragmentFunction = ff;
      {
        MTLLinkedFunctions *ol = [MTLLinkedFunctions linkedFunctions], *ml = [MTLLinkedFunctions linkedFunctions];
        ol.functions = @[ fsi, th ];
        ml.functions = @[ tt, td ];
        d.objectLinkedFunctions = ol;
        d.meshLinkedFunctions = ml;
      }
      e = nil;
      params->ret_pso = (obj_handle_t)[(id<MTLDevice>)params->device newRenderPipelineStateWithMeshDescriptor:d
                                                                                                      options:MTLPipelineOptionNone
                                                                                                   reflection:nil
                                                                                                        error:&e];
      if (!params->ret_pso)
        fprintf(stderr, "[winemetal] DXIL tessellation pipeline: %s\\n", e ? [[e localizedDescription] UTF8String] : "?");
      else {
        static unsigned said;
        if (said++ < 4)
          fprintf(stderr, "[winemetal] DXIL tessellation pipeline OK: vs '%s' mesh '%s' ps '%s', vertex %u B, max factor %.1f\\n",
                  g->vertex_function, g->geometry_function, g->fragment_function, g->gs_vertex_size_bytes, (double)mtf);
      }
      return STATUS_SUCCESS;
    }

    cv = [[[MTLFunctionConstantValues alloc] init] autorelease];
    fsi = [[Ls newFunctionWithName:Ls.functionNames.firstObject] autorelease];"""

DRAW_OLD = """    case WMTRenderCommandDrawMeshThreadgroups: {
      struct wmtcmd_render_draw_meshthreadgroups *body = (struct wmtcmd_render_draw_meshthreadgroups *)next;
      [encoder drawMeshThreadgroups:MTLSizeMake("""

DRAW_NEW = """    case WMTRenderCommandDrawMeshThreadgroups: {
      struct wmtcmd_render_draw_meshthreadgroups *body = (struct wmtcmd_render_draw_meshthreadgroups *)next;
      /* madeira-bcd: DXIL tessellation draws carry the object threadgroup memory
       * the converter's draw helpers set (reserved[1] = 0x7e55 marks it). */
      if (body->reserved[1] == 0x7e55 && body->reserved[0])
        [encoder setObjectThreadgroupMemoryLength:body->reserved[0] atIndex:0];
      [encoder drawMeshThreadgroups:MTLSizeMake("""

DRAWI_OLD = """    case WMTRenderCommandDrawMeshThreadgroupsIndirect: {
      struct wmtcmd_render_draw_meshthreadgroups_indirect *body =
          (struct wmtcmd_render_draw_meshthreadgroups_indirect *)next;
      [encoder drawMeshThreadgroupsWithIndirectBuffer:"""

DRAWI_NEW = """    case WMTRenderCommandDrawMeshThreadgroupsIndirect: {
      struct wmtcmd_render_draw_meshthreadgroups_indirect *body =
          (struct wmtcmd_render_draw_meshthreadgroups_indirect *)next;
      /* madeira-bcd: DXIL tessellation, indirect (same marker as the direct draw) */
      if (body->reserved[1] == 0x7e55 && body->reserved[0])
        [encoder setObjectThreadgroupMemoryLength:body->reserved[0] atIndex:0];
      [encoder drawMeshThreadgroupsWithIndirectBuffer:"""


def patch(path, pairs):
    s = path.read_text()
    if MARKER in s:
        print(f"{path}: DXIL tessellation already present")
        return 0
    for old, new in pairs:
        if s.count(old) != 1:
            print(f"::error::{path}: DXIL tessellation anchor not found once -- dxmt moved, review this patch")
            return 1
        s = s.replace(old, new)
    path.write_text(s)
    print(f"{path}: DXIL tessellation patched")
    return 0


def main():
    if MARKER not in HDR.read_text() and "obj_handle_t hull_library;" in HDR.read_text():
        # willfaust/dxmt#5 (490f7d1): the pipeline, the header fields and the
        # direct draw are upstream; its squash dropped the indirect draw's
        # 0x7e55 handling, which madeira_d3d12's ExecuteIndirect path uses.
        print(f"{HDR}: DXIL tessellation is upstream (willfaust/dxmt#5); adding only the indirect draw")
        return patch(UNIX, [(DRAWI_OLD, DRAWI_NEW)])
    rc = patch(HDR, [(HDR_OLD, HDR_NEW)])
    rc |= patch(UNIX, [(BUILD_OLD, BUILD_NEW), (DRAW_OLD, DRAW_NEW), (DRAWI_OLD, DRAWI_NEW)])
    return rc


if __name__ == "__main__":
    sys.exit(main())
