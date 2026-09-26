#!/usr/bin/env python3
"""Add the NVAPI entry points DXMT's nvapi.cpp lacks and Nixxes ports call.

Ghost of Tsushima maps the NVAPI GPU to its DXGI adapter and the display to
an NVIDIA display handle before it reads the driver version:
NvAPI_GetLogicalGPUFromPhysicalGPU, NvAPI_GetAssociatedNvidiaDisplayHandle and
NvAPI_GetAssociatedDisplayOutputId came back "not implemented", and the game
then reported "Failed to get GPU Driver Info". Physical and logical GPU
handles are both the Metal device's registry ID in DXMT, so the mapping is
the identity; display handles are HMONITORs, as NvAPI_EnumNvidiaDisplayHandle
already returns.

Applied by tools/build-dxmt-nvapi.sh to a copy of the file; the dxmt
submodule is not modified. Usage: patch-dxmt-nvapi.py <copy of nvapi.cpp>
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: NVAPI entry points"
if marker in src:
    print("already patched")
    sys.exit(0)

funcs = r'''
/* madeira-bcd: NVAPI entry points Nixxes ports call (tools/patch-dxmt-nvapi.py). */
static bool madeira_gpu_exists(uint64_t registry_id) {
  auto devices = WMT::CopyAllDevices();
  for (unsigned i = 0; i < devices.count(); i++)
    if (devices.object(i).registryID() == registry_id)
      return true;
  return false;
}

NVAPI_INTERFACE
NvAPI_GetLogicalGPUFromPhysicalGPU(NvPhysicalGpuHandle hPhysicalGPU, NvLogicalGpuHandle *pLogicalGPU) {
  if (!pLogicalGPU)
    return NVAPI_INVALID_ARGUMENT;
  if (!madeira_gpu_exists(uint64_t(hPhysicalGPU)))
    return NVAPI_EXPECTED_PHYSICAL_GPU_HANDLE;
  *pLogicalGPU = (NvLogicalGpuHandle)hPhysicalGPU;
  return NVAPI_OK;
}

NVAPI_INTERFACE
NvAPI_GetPhysicalGPUsFromLogicalGPU(NvLogicalGpuHandle hLogicalGPU,
                                    NvPhysicalGpuHandle hPhysicalGPU[NVAPI_MAX_PHYSICAL_GPUS], NvU32 *pGpuCount) {
  if (!hPhysicalGPU || !pGpuCount)
    return NVAPI_INVALID_ARGUMENT;
  if (!madeira_gpu_exists(uint64_t(hLogicalGPU)))
    return NVAPI_EXPECTED_LOGICAL_GPU_HANDLE;
  hPhysicalGPU[0] = (NvPhysicalGpuHandle)hLogicalGPU;
  *pGpuCount = 1;
  return NVAPI_OK;
}

NVAPI_INTERFACE
NvAPI_GetAssociatedNvidiaDisplayHandle(const char *szDisplayName, NvDisplayHandle *pNvDispHandle) {
  if (!szDisplayName || !pNvDispHandle)
    return NVAPI_INVALID_ARGUMENT;
  for (unsigned i = 0;; i++) {
    HMONITOR monitor = wsi::enumMonitors(i);
    if (!monitor)
      break;
    MONITORINFOEXA info = {};
    info.cbSize = sizeof(info);
    if (GetMonitorInfoA(monitor, &info) && !_stricmp(info.szDevice, szDisplayName)) {
      *pNvDispHandle = (NvDisplayHandle)monitor;
      return NVAPI_OK;
    }
  }
  /* One display on this port: a name the monitor list does not know still
   * means that display (the game passes the GDI name DXGI gave it). */
  HMONITOR primary = wsi::enumMonitors(0);
  if (!primary)
    return NVAPI_NVIDIA_DEVICE_NOT_FOUND;
  *pNvDispHandle = (NvDisplayHandle)primary;
  return NVAPI_OK;
}

NVAPI_INTERFACE
NvAPI_GetAssociatedDisplayOutputId(NvDisplayHandle hNvDisplay, NvU32 *pOutputId) {
  if (!hNvDisplay || !pOutputId)
    return NVAPI_INVALID_ARGUMENT;
  for (unsigned i = 0; i < 32; i++) {
    HMONITOR monitor = wsi::enumMonitors(i);
    if (!monitor)
      break;
    if ((NvDisplayHandle)monitor == hNvDisplay) {
      *pOutputId = 1u << i;
      return NVAPI_OK;
    }
  }
  return NVAPI_EXPECTED_DISPLAY_HANDLE;
}

NVAPI_INTERFACE
NvAPI_GPU_GetPCIIdentifiers(NvPhysicalGpuHandle hPhysicalGpu, NvU32 *pDeviceId, NvU32 *pSubSystemId,
                            NvU32 *pRevisionId, NvU32 *pExtDeviceId) {
  if (!pDeviceId || !pSubSystemId || !pRevisionId || !pExtDeviceId)
    return NVAPI_INVALID_ARGUMENT;
  if (!madeira_gpu_exists(uint64_t(hPhysicalGpu)))
    return NVAPI_EXPECTED_PHYSICAL_GPU_HANDLE;
  /* DXGI reports vendor 0x10DE and device 0 with NVEXT; say the same. */
  *pDeviceId = 0x10DE;
  *pSubSystemId = 0;
  *pRevisionId = 0;
  *pExtDeviceId = 0;
  return NVAPI_OK;
}

'''

anchor = 'extern "C" __cdecl void *nvapi_QueryInterface(NvU32 id) {'
if src.count(anchor) != 1:
    sys.exit("patch-dxmt-nvapi: nvapi_QueryInterface anchor not found")
src = src.replace(anchor, funcs + anchor)

case_anchor = "  case 0x842b066e:\n    return (void *)&NvAPI_GPU_GetLogicalGpuInfo;\n"
if src.count(case_anchor) != 1:
    sys.exit("patch-dxmt-nvapi: QueryInterface table anchor not found")
src = src.replace(case_anchor, case_anchor + """  case 0xadd604d1:
    return (void *)&NvAPI_GetLogicalGPUFromPhysicalGPU;
  case 0xaea3fa32:
    return (void *)&NvAPI_GetPhysicalGPUsFromLogicalGPU;
  case 0x35c29134:
    return (void *)&NvAPI_GetAssociatedNvidiaDisplayHandle;
  case 0xd995937e:
    return (void *)&NvAPI_GetAssociatedDisplayOutputId;
  case 0x2ddfb66e:
    return (void *)&NvAPI_GPU_GetPCIIdentifiers;
""")

# Name every entry point the game asks for, once, so a log shows what a title
# expects from NVAPI (the table is NVIDIA's own id -> name list).
trace_anchor = anchor + "\n"
if src.count(trace_anchor) != 1:
    sys.exit("patch-dxmt-nvapi: QueryInterface body anchor not found")
src = src.replace(trace_anchor, trace_anchor + """  {
    static NvU32 seen[256];
    static unsigned nseen;
    bool known = false;
    for (unsigned i = 0; i < nseen; i++)
      if (seen[i] == id) { known = true; break; }
    if (!known && nseen < 256) {
      seen[nseen++] = id;
      const char *name = "?";
      for (auto &iface : nvapi_interface_table)
        if (iface.id == id) { name = iface.func; break; }
      fprintf(stderr, "[nvapi] query 0x%08x %s\\n", (unsigned)id, name);
    }
  }
""")

open(path, "w").write(src)
print("patched " + path)
