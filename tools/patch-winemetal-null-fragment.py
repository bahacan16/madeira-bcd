#!/usr/bin/env python3
"""An empty fragment function for mesh pipelines that have none.

Metal does not refuse a mesh render pipeline with rasterization on and no
fragment function: it ABORTS ("Mesh Render Pipeline Descriptor Validation /
fragmentFunction must not be nil", MTLReportFailure -> abort). D3D allows a
pipeline with no pixel shader (a depth-only pass). Horizon Zero Dawn creates
vertex + geometry shader pipelines without one; its AsyncPsoQueue thread died
inside the geometry-emulation builder and the game's main thread waited for
it forever after its first frame (build 469, log 2026-10-09 13:57).

In winemetal_unix.c every mesh pipeline builder (the D3D12 runtime's
geometry-emulation builder, its DXIL tessellation branch, and DXMT's own
_MTLDevice_newMeshRenderPipelineState) now checks the descriptor before Metal
sees it: no fragment function and rasterization on -> the fragment function
becomes `fragment void madeira_null_fragment() {}` (compiled once per process
from source) and every colour attachment gets write mask none, as D3D writes
no render target without a pixel shader; alpha to coverage goes off. Depth and
stencil work as before. A pipeline with a fragment function, or with
rasterization off, is untouched, so working games build exactly what they built
before. madeira.cfg / a game's config gs-null-fragment = 0 refuses such a
pipeline instead (the D3D12 runtime then drops the geometry shader, as it does
for any refused geometry pipeline); neither way reaches Metal's abort. If the
empty function cannot be compiled, the pipeline is refused too. [null-fs]
lines (8 at most) say which. Native only: not in the i386 farm's cache key.
Idempotent; fails by name if an anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: empty fragment function"

ANCHOR_FN = "static NTSTATUS\n_MTLDevice_newMeshRenderPipelineState(void *obj) {\n"
HELPERS = r'''/* madeira-bcd: empty fragment function (tools/patch-winemetal-null-fragment.py).
 * Metal ABORTS on a mesh pipeline with rasterization on and no fragment
 * function; D3D allows a pipeline with no pixel shader (Horizon Zero Dawn's
 * VS + GS pipelines killed its pipeline thread, build 469). Such a pipeline
 * gets an empty fragment function and no colour writes; gs-null-fragment = 0
 * refuses it instead. Returns 0 when the pipeline must be refused. */
static id<MTLLibrary> madeira_null_fs_lib;   /* kept for the process, like the function */
static id<MTLFunction> madeira_null_fs_fn;
static id<MTLDevice> madeira_null_fs_dev;

static id<MTLFunction> madeira_null_fs_get(id<MTLDevice> dev) {
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    NSError *e = nil;
    madeira_null_fs_lib = [dev newLibraryWithSource:@"fragment void madeira_null_fragment() {}" options:nil error:&e];
    madeira_null_fs_fn = madeira_null_fs_lib ? [madeira_null_fs_lib newFunctionWithName:@"madeira_null_fragment"] : nil;
    madeira_null_fs_dev = dev;
    if (!madeira_null_fs_fn)
      fprintf(stderr, "[null-fs] madeira-bcd empty fragment function could not be compiled: %s\n",
              e ? [[e localizedDescription] UTF8String] : "?");
  });
  return madeira_null_fs_dev == dev ? madeira_null_fs_fn : nil;
}

static int madeira_null_fs_fix(id<MTLDevice> dev, MTLMeshRenderPipelineDescriptor *d, const char *who) {
  static long long on = -1;
  static unsigned said;
  id<MTLFunction> f;
  if (d.fragmentFunction || !d.rasterizationEnabled) return 1;
  if (on < 0) on = madeira_cfg_int("gs-null-fragment", 1) ? 1 : 0;
  f = on ? madeira_null_fs_get(dev) : nil;
  if (!f) {
    if (said++ < 8)
      fprintf(stderr, "[null-fs] madeira-bcd %s pipeline with no pixel shader REFUSED (%s)\n", who,
              on ? "no empty fragment function" : "gs-null-fragment = 0");
    return 0;
  }
  d.fragmentFunction = f;
  for (unsigned c = 0; c < 8; c++) d.colorAttachments[c].writeMask = MTLColorWriteMaskNone;
  d.alphaToCoverageEnabled = NO;
  if (said++ < 8)
    fprintf(stderr, "[null-fs] madeira-bcd %s pipeline with no pixel shader: empty fragment function, "
                    "no colour writes (gs-null-fragment = 0 refuses it)\n", who);
  return 1;
}

'''

MESH_OLD = """  NSError *err = NULL;
  params->ret_pso = (obj_handle_t)[(id<MTLDevice>)params->device newRenderPipelineStateWithMeshDescriptor:descriptor
"""
MESH_NEW = """  if (!madeira_null_fs_fix((id<MTLDevice>)params->device, descriptor, "mesh")) {   /* madeira-bcd */
    params->ret_pso = 0;
    params->ret_error = 0;
    [descriptor release];
    return STATUS_SUCCESS;
  }
  NSError *err = NULL;
  params->ret_pso = (obj_handle_t)[(id<MTLDevice>)params->device newRenderPipelineStateWithMeshDescriptor:descriptor
"""

TESS_OLD = """      d.meshFunction = tm;
      d.fragmentFunction = ff;
"""
TESS_NEW = """      d.meshFunction = tm;
      d.fragmentFunction = ff;
      if (!madeira_null_fs_fix((id<MTLDevice>)params->device, d, "DXIL tessellation"))   /* madeira-bcd */
        return STATUS_SUCCESS;
"""

GEOM_OLD = """    d.meshFunction = fm;
    d.fragmentFunction = ff;
"""
GEOM_NEW = """    d.meshFunction = fm;
    d.fragmentFunction = ff;
    if (!madeira_null_fs_fix((id<MTLDevice>)params->device, d, "geometry-emulation"))   /* madeira-bcd */
      return STATUS_SUCCESS;
"""


def main():
    src = PATH.read_text()
    if MARKER in src:
        print(f"{PATH}: empty fragment function already patched")
        return 0
    for name, old in (("_MTLDevice_newMeshRenderPipelineState", ANCHOR_FN), ("mesh pipeline creation", MESH_OLD),
                      ("DXIL tessellation fragment", TESS_OLD), ("geometry-emulation fragment", GEOM_OLD)):
        if src.count(old) != 1:
            print(f"::error::{PATH}: {name} anchor not found once -- dxmt moved, review "
                  "tools/patch-winemetal-null-fragment.py")
            return 1
    if src.index(ANCHOR_FN) > src.index(TESS_OLD) or src.index(ANCHOR_FN) > src.index(GEOM_OLD):
        print(f"::error::{PATH}: the mesh builder no longer comes before the geometry-emulation builder")
        return 1
    src = src.replace(ANCHOR_FN, HELPERS + ANCHOR_FN)
    src = src.replace(MESH_OLD, MESH_NEW)
    src = src.replace(TESS_OLD, TESS_NEW)
    src = src.replace(GEOM_OLD, GEOM_NEW)
    PATH.write_text(src)
    print(f"{PATH}: empty fragment function patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
