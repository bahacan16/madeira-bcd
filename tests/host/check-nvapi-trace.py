#!/usr/bin/env python3
"""NVAPI trace and display-name fallback (tools/patch-nvapi-trace.py); no Wine runs.

Applies tools/build-dxmt-nvapi.sh's four patches, in its order, to a copy of
dxmt/src/nvapi/nvapi.cpp (or $DXMT_SRC/src/nvapi/nvapi.cpp) and checks: the
trace patch refuses to run before patch-dxmt-nvapi.py, is idempotent, sends
the "[nvapi] query" line through DXMT's Logger instead of stderr, and routes
the adapter / display / driver entry points through logging wrappers while the
per-frame ones (latency, sleep, UAV overlap) stay direct. With an arm64ec
llvm-mingw ($MINGW, or arm64ec-w64-mingw32-clang++ on PATH) and the dxmt
submodule's external headers it also compiles the result and checks that
every wrapper has an x64-callable entry thunk in the hybrid map. Skipped with
a note when the dxmt sources are not checked out.
"""
from pathlib import Path
import os, shutil, struct, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
dxmt = Path(os.environ.get("DXMT_SRC", root / "dxmt"))
nvapi = dxmt / "src/nvapi/nvapi.cpp"
if not nvapi.exists():
    print("SKIP: %s not checked out (set DXMT_SRC)" % nvapi)
    sys.exit(0)

def run(script, path, ok=True):
    r = subprocess.run([sys.executable, str(root / "tools" / script), str(path)], capture_output=True, text=True)
    if ok is not None:
        assert (r.returncode == 0) == ok, (script, r.returncode, r.stdout, r.stderr)
    return r.stdout + r.stderr

with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    cpp = t / "nvapi.cpp"
    shutil.copy(nvapi, cpp)
    # dxmt db546ee already carries patch-dxmt-nvapi.py's entry points (willfaust/dxmt
    # merged them); the ordering refusal only applies to a tree that lacks them.
    probe = t / "probe.cpp"
    shutil.copy(nvapi, probe)
    run("patch-dxmt-nvapi.py", probe)
    if probe.read_text() != nvapi.read_text():
        assert "patch-dxmt-nvapi.py first" in run("patch-nvapi-trace.py", cpp, ok=False)
    run("patch-dxmt-nvapi.py", cpp)
    run("patch-nvapi-strings.py", cpp)
    out = run("patch-nvapi-gpu-info.py", cpp)
    assert "frame-buffer sizes" in out or "are upstream (willfaust/dxmt#11)" in out, out
    gpu_info = cpp.read_text()
    out = run("patch-nvapi-gpu-info.py", cpp)
    assert "already patched" in out or "are upstream (willfaust/dxmt#11)" in out, out
    assert cpp.read_text() == gpu_info
    assert "entry points traced" in run("patch-nvapi-trace.py", cpp)
    once = cpp.read_text()
    assert "already patched" in run("patch-nvapi-trace.py", cpp)
    assert cpp.read_text() == once

    assert 'fprintf(stderr, "[nvapi] query' not in once and "Logger::info(line);" in once
    qi = once[once.index('extern "C" __cdecl void *nvapi_QueryInterface(NvU32 id) {'):]
    for fn in ("DISP_GetDisplayIdByDisplayName", "GetAssociatedNvidiaDisplayHandle", "SYS_GetDriverAndBranchVersion",
               "GetDisplayDriverVersion", "EnumPhysicalGPUs", "GPU_GetConnectedDisplayIds",
               "DISP_GetGDIPrimaryDisplayId", "GPU_GetAdapterIdFromPhysicalGpu", "GPU_GetFullName"):
        assert "return (void *)&madeira_nv_%s;" % fn in qi, fn
        assert "return (void *)&NvAPI_%s;" % fn not in qi, fn
    for fn in ("Initialize", "GetAssociatedDisplayOutputId", "GPU_GetPCIIdentifiers", "EnumNvidiaDisplayHandle"):
        assert "madeira_nv_trace<&dxmt::NvAPI_%s, 0x" % fn in qi, fn
    for fn in ("D3D_SetLatencyMarker", "D3D_Sleep", "D3D11_BeginUAVOverlap", "D3D_SetSleepMode"):
        assert "return (void *)&NvAPI_%s;" % fn in qi, fn
    # patch-nvapi-gpu-info.py: GTA V Enhanced's frame-buffer queries resolve, the
    # core count answers (and keeps its trace status line).
    # (on dxmt db546ee the trace also wraps them; either way they resolve)
    for case, fn in (("0x46fbeb03", "GPU_GetPhysicalFrameBufferSize"), ("0x5a04b644", "GPU_GetVirtualFrameBufferSize")):
        assert ("  case %s:\n    return (void *)&NvAPI_%s;" % (case, fn) in qi or
                "  case %s:\n    return (void *)&madeira_nv_trace<&dxmt::NvAPI_%s, %su>::call;" % (case, fn, case) in qi), fn
    assert "madeira_nv_trace<&dxmt::NvAPI_GPU_GetGpuCoreCount, 0x" in qi
    core = once[once.index("NvAPI_GPU_GetGpuCoreCount(NvPhysicalGpuHandle hPhysicalGpu, NvU32 *pCount) {"):]
    core = core[:core.index("\n}\n")]
    assert "*pCount = 16384;" in core and "NVAPI_NOT_SUPPORTED" not in core
    fb = once[once.index("madeira_nv_DISP_GetDisplayIdByDisplayName(const char"):]
    fb = fb[:fb.index("\n}\n")]
    assert "s == NVAPI_NVIDIA_DEVICE_NOT_FOUND && displayId && madeira_nv_is_primary_adapter_name(displayName)" in fb
    assert "*displayId = WMTGetPrimaryDisplayId();" in fb

    mingw = os.environ.get("MINGW")
    cxx = Path(mingw) / "arm64ec-w64-mingw32-clang++" if mingw else shutil.which("arm64ec-w64-mingw32-clang++")
    if not cxx or not Path(cxx).exists() or not (dxmt / "external/nvapi/nvapi.h").exists():
        print("note: no arm64ec llvm-mingw or dxmt/external/nvapi; compile step skipped")
    else:
        u = dxmt / "src/util"
        obj = t / "nvapi.o"
        subprocess.run([str(cxx), "-std=c++20", "-O2", "-c", "-o", str(obj), str(cpp),
                        "-I%s" % (dxmt / "include"), "-I%s" % (dxmt / "libs"), "-I%s" % u,
                        "-I%s" % (dxmt / "src/winemetal"), "-I%s" % (dxmt / "external/nvapi"),
                        "-I%s" % (dxmt / "src/nvapi"), "-I%s" % (dxmt / "src/d3d11"), "-I%s" % (dxmt / "src/dxgi"),
                        "-DNOMINMAX", "-D_WIN32_WINNT=0xa00", "-DDXMT_IOS=1", "-DDXMT_PAGE_SIZE=4096", "-fblocks",
                        "-Wno-microsoft-exception-spec", "-Wno-unknown-attributes"], check=True)
        data = obj.read_bytes()
        _, nsec, _, symptr, nsym, optsz, _ = struct.unpack_from("<HHIIIHH", data, 0)
        strtab = symptr + nsym * 18
        def name(i):
            raw = data[symptr + i * 18:symptr + i * 18 + 8]
            if raw[:4] == b"\0\0\0\0":
                o = struct.unpack_from("<I", raw, 4)[0]
                return data[strtab + o:data.index(b"\0", strtab + o)].decode()
            return raw.rstrip(b"\0").decode()
        thunked = set()
        for i in range(nsec):
            sname, _, _, size, ptr = struct.unpack_from("<8sIIII", data, 20 + optsz + i * 40)
            if sname.rstrip(b"\0").startswith(b".hybmp"):
                for k in range(0, size, 12):
                    a, b, kind = struct.unpack_from("<III", data, ptr + k)
                    if kind == 1:
                        thunked.add(name(a))
        wrappers = [s for s in thunked if "madeira_nv" in s]
        assert len(wrappers) == 30, sorted(wrappers)
        print("compiled for arm64ec: 30 wrappers with entry thunks")

print("PASS: NVAPI query trace via the Logger; adapter/display/driver entry points wrapped (per-frame ones direct); "
      "display-id fallback for the primary adapter name; patch ordered and idempotent")
