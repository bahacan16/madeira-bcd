#!/usr/bin/env python3
"""The empty fragment function for mesh pipelines that have none; no Metal.

Metal aborts on a mesh render pipeline with rasterization on and no fragment
function ("fragmentFunction must not be nil", MTLReportFailure -> abort).
Horizon Zero Dawn's vertex + geometry shader pipelines without a pixel shader
killed its pipeline thread in the geometry-emulation builder, and the game
hung after its first frame (build 469, log 2026-10-09 13:57).
tools/patch-winemetal-null-fragment.py gives such pipelines an empty fragment
function. Checks, on winemetal_unix.c as the CI build has it (patched in
place, or a patched copy of the pinned source):
  - the patch applies once and a second run only says it is already there;
  - all three mesh builders (DXMT's mesh pipeline, the geometry-emulation
    builder and its DXIL tessellation branch) run the check after their
    fragment function is set and before Metal builds the pipeline; a refusal
    returns no pipeline and no error, and the mesh builder releases its
    descriptor;
  - the check leaves a descriptor with a fragment function, or with
    rasterization off, alone; otherwise it sets the empty function, write
    mask none on all eight colour attachments and alpha to coverage off; with
    gs-null-fragment = 0, or without the function, it refuses;
  - the function is `fragment void madeira_null_fragment() {}`, compiled once
    per process and kept;
  - build-ipa.yml runs the patch after the other winemetal patches and before
    the DXMT build, and runs this test; the catalog lists gs-null-fragment, on
    by default.
"""
from pathlib import Path
import os, re, shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
patch = root / "tools/patch-winemetal-null-fragment.py"
wm = root / "dxmt/src/winemetal/unix/winemetal_unix.c"
workflow = (root / ".github/workflows/build-ipa.yml").read_text()
catalog = (root / "app/Madeira/ConfigCatalog.generated.swift").read_text()
ok = True


def check(what, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + what)
    ok &= bool(cond)


if not wm.exists():
    print(f"SKIP: no dxmt sources at {wm.parent} (check out the submodule)")
    sys.exit(0)

marker = "madeira-bcd: empty fragment function"
with tempfile.TemporaryDirectory() as t:
    dst = Path(t) / "dxmt/src/winemetal/unix/winemetal_unix.c"
    dst.parent.mkdir(parents=True)
    shutil.copy(wm, dst)
    already = marker in dst.read_text()
    r1 = subprocess.run([sys.executable, str(patch)], cwd=t, capture_output=True, text=True)
    r2 = subprocess.run([sys.executable, str(patch)], cwd=t, capture_output=True, text=True)
    check("the patch applies (or is already in place) and a second run only says so",
          r1.returncode == 0 and r2.returncode == 0 and "already patched" in r2.stdout
          and (already or ("empty fragment function patched" in r1.stdout and "already" not in r1.stdout)))
    src = dst.read_text()

check("the helpers are in once", src.count(marker) == 1 and src.count("static int madeira_null_fs_fix(") == 1)

fix = src[src.index("static int madeira_null_fs_fix("):]
fix = fix[:fix.index("\n}\n")]
check("a descriptor with a fragment function, or with rasterization off, is left alone",
      "if (d.fragmentFunction || !d.rasterizationEnabled) return 1;" in fix
      and fix.index("if (d.fragmentFunction || !d.rasterizationEnabled) return 1;") < fix.index("d.fragmentFunction = f;"))
check("otherwise: the empty function, no colour writes on all eight attachments, alpha to coverage off",
      "d.fragmentFunction = f;" in fix
      and "for (unsigned c = 0; c < 8; c++) d.colorAttachments[c].writeMask = MTLColorWriteMaskNone;" in fix
      and "d.alphaToCoverageEnabled = NO;" in fix)
check("gs-null-fragment = 0, or no function, refuses (on by default)",
      'madeira_cfg_int("gs-null-fragment", 1)' in fix and "f = on ? madeira_null_fs_get(dev) : nil;" in fix
      and fix.index("if (!f) {") < fix.index("return 0;") < fix.index("d.fragmentFunction = f;"))

get = src[src.index("static id<MTLFunction> madeira_null_fs_get("):]
get = get[:get.index("\n}\n")]
check("the function is an empty fragment function, compiled once and kept",
      "dispatch_once(&once, ^{" in get
      and '[dev newLibraryWithSource:@"fragment void madeira_null_fragment() {}" options:nil error:&e]' in get
      and '[madeira_null_fs_lib newFunctionWithName:@"madeira_null_fragment"]' in get
      and "release" not in get)


def body(start):
    i = src.index(start)
    return src[i:src.index("\n}\n", i)]


mesh = body("static NTSTATUS\n_MTLDevice_newMeshRenderPipelineState(void *obj) {")
mcall = 'if (!madeira_null_fs_fix((id<MTLDevice>)params->device, descriptor, "mesh")) {'
check("DXMT's mesh builder: checked after its functions are set and before Metal builds; a refusal releases the descriptor",
      mesh.count(mcall) == 1
      and mesh.index("descriptor.fragmentFunction = (id<MTLFunction>)info->fragment_function;") < mesh.index(mcall)
      and mesh.index("descriptor.rasterizationEnabled = info->rasterization_enabled;") < mesh.index(mcall)
      < mesh.index("newRenderPipelineStateWithMeshDescriptor:descriptor")
      and "params->ret_pso = 0;\n    params->ret_error = 0;\n    [descriptor release];\n    return STATUS_SUCCESS;"
      in mesh[mesh.index(mcall):])

geo = body("static NTSTATUS\n_MTLDevice_newGeometryEmulationPipelineState(void *obj) {")
tcall = 'if (!madeira_null_fs_fix((id<MTLDevice>)params->device, d, "DXIL tessellation"))'
gcall = 'if (!madeira_null_fs_fix((id<MTLDevice>)params->device, d, "geometry-emulation"))'
tess = geo[geo.index("if (g->tessellation) {"):]
plain = tess[tess.index("cv = [[[MTLFunctionConstantValues alloc] init] autorelease];"):]
tess = tess[:tess.index("cv = [[[MTLFunctionConstantValues alloc] init] autorelease];")]
check("the DXIL tessellation branch: checked after its fragment function and rasterization are set, before Metal builds",
      tess.count(tcall) == 1 and tess.index("d.fragmentFunction = ff;") < tess.index(tcall)
      < tess.index("newRenderPipelineStateWithMeshDescriptor:d")
      and geo.index("d.rasterizationEnabled = i->rasterization_enabled;") < geo.index(tcall)
      and tess[tess.index(tcall):].split("\n")[1].strip() == "return STATUS_SUCCESS;")
check("the geometry-emulation builder: the same, and no pipeline on a refusal",
      plain.count(gcall) == 1 and plain.index("d.fragmentFunction = ff;") < plain.index(gcall)
      < plain.index("newRenderPipelineStateWithMeshDescriptor:d")
      and plain[plain.index(gcall):].split("\n")[1].strip() == "return STATUS_SUCCESS;"
      and "params->ret_pso = 0;" in geo[:geo.index("if (wmtr_enabled()) {")])
check("no mesh pipeline reaches Metal unchecked",
      src.count("newRenderPipelineStateWithMeshDescriptor:") == 3
      and src.count("madeira_null_fs_fix((id<MTLDevice>)params->device,") == 3)

steps = re.findall(r"python3 (tools/patch-[\w.-]+\.py)", workflow)
check("build-ipa.yml runs the patch after the other winemetal patches and before the DXMT build",
      "tools/patch-winemetal-null-fragment.py" in steps
      and steps.index("tools/patch-winemetal-null-fragment.py") > steps.index("tools/patch-winemetal-pso-warm.py")
      and workflow.index("python3 tools/patch-winemetal-null-fragment.py") < workflow.index("build/dxmt-ios/build.sh"))
check("build-ipa.yml runs this test", "python3 tests/host/check-null-fragment.py" in workflow)
check("the catalog lists gs-null-fragment, on by default",
      re.search(r'key: "gs-null-fragment".*defaultValue: "1"', catalog) is not None)

print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
