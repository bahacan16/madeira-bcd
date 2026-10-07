#!/usr/bin/env python3
"""Make NVAPI's answers visible in the session log, and let the one display be
found by its adapter name.

1. patch-dxmt-nvapi.py names every entry point a game asks for with
   fprintf(stderr), which never reaches the session log from a PE DLL (no
   GoT log since 2026-09-26 has a single "[nvapi] query" line). DXMT's own
   Logger does (it writes through ntdll's __wine_dbg_output, like "info:  Found
   config env"), so the trace goes through it now.
2. The entry points a game's adapter / display / driver check uses are
   returned through a logging wrapper: "[nvapi] NvAPI_X -> <status>" for the
   first calls of each (and later failures), with the values that matter
   (display names, display ids, driver version and branch, GPU count, LUID).
   Per-frame entry points (latency, sleep, UAV overlap) are not wrapped.
3. NvAPI_DISP_GetDisplayIdByDisplayName compared the name with user32's
   GetMonitorInfo szDevice only; on this port that was "WinDisc" for the
   virtual monitor since the build 222 switch (win32u now says "\\\\.\\DISPLAY1"
   again, see docs/got-gpu-check.md), so a game passing DXGI's or
   EnumDisplayDevices' name got NVAPI_NVIDIA_DEVICE_NOT_FOUND. When the name
   is the primary adapter's EnumDisplayDevices name it now answers the primary
   display id, the same fallback NvAPI_GetAssociatedNvidiaDisplayHandle has.

Applied by tools/build-dxmt-nvapi.sh to its copy of nvapi.cpp after
patch-dxmt-nvapi.py and patch-nvapi-strings.py; the dxmt submodule is not
modified. Only NVIDIA-reporting launches load NVAPI (DXMT_ENABLE_NVEXT=1).
Usage: patch-nvapi-trace.py <copy of nvapi.cpp>. Idempotent; anchor-checked.
"""
import re, sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: NVAPI trace"
if marker in src:
    print("patch-nvapi-trace: already patched")
    sys.exit(0)
# dxmt db546ee has willfaust/dxmt#11: the entry points patch-dxmt-nvapi.py
# added are upstream, without its fprintf query trace (and the display-id
# fallback is inside NvAPI_DISP_GetDisplayIdByDisplayName itself).
upstream = "madeira-bcd: NVAPI entry points" not in src and "FindDevice(uint64_t registry_id)" in src
if "madeira-bcd: NVAPI entry points" not in src and not upstream:
    sys.exit("patch-nvapi-trace: run tools/patch-dxmt-nvapi.py first")

# 1. The query trace through the Logger.
old_trace = 'fprintf(stderr, "[nvapi] query 0x%08x %s\\n", (unsigned)id, name);'
qi = 'extern "C" __cdecl void *nvapi_QueryInterface(NvU32 id) {'
if upstream:
    if src.count(qi) != 1:
        sys.exit("patch-nvapi-trace: nvapi_QueryInterface anchor not found")
    src = src.replace(qi, qi + """
  {
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
      char line[160];  /* """ + marker + """: the Logger reaches the session log */
      snprintf(line, sizeof(line), "[nvapi] query 0x%08x %s", (unsigned)id, name);
      Logger::info(line);
    }
  }""")
else:
    if src.count(old_trace) != 1:
        sys.exit("patch-nvapi-trace: query trace anchor not found")
    src = src.replace(old_trace, (
        'char line[160];  /* ' + marker + ': the Logger reaches the session log */\n'
        '      snprintf(line, sizeof(line), "[nvapi] query 0x%08x %s", (unsigned)id, name);\n'
        '      Logger::info(line);'))

# 2./3. Wrappers.
wrappers = r'''
/* madeira-bcd: NVAPI trace (tools/patch-nvapi-trace.py). */
static const char *madeira_nv_name(NvU32 id) {
  for (auto &iface : nvapi_interface_table)
    if (iface.id == id)
      return iface.func;
  return "?";
}

/* The first calls of an entry point, and its later failures, are logged. */
static bool madeira_nv_should_log(std::atomic<unsigned> &calls, NvAPI_Status s) {
  unsigned k = calls.fetch_add(1, std::memory_order_relaxed);
  return k < 6 || (s != NVAPI_OK && k < 40);
}

template <auto Fn, NvU32 Id> struct madeira_nv_trace;
template <typename... A, NvAPI_Status (*Fn)(A...), NvU32 Id>
struct madeira_nv_trace<Fn, Id> {
  static NvAPI_Status __cdecl call(A... a) {
    static std::atomic<unsigned> calls{0};
    NvAPI_Status s = Fn(a...);
    if (madeira_nv_should_log(calls, s))
      Logger::info(str::format("[nvapi] ", madeira_nv_name(Id), " -> ", int(s)));
    return s;
  }
};

static std::string madeira_nv_str(const char *s, size_t cap) {
  if (!s)
    return "(null)";
  size_t n = 0;
  while (n < cap && s[n])
    n++;
  return std::string(s, n) + (n == cap ? "[no NUL]" : "");
}

/* The adapter name EnumDisplayDevices gives the primary display. */
static bool madeira_nv_is_primary_adapter_name(const char *name) {
  DISPLAY_DEVICEA dd = {};
  dd.cb = sizeof(dd);
  for (DWORD i = 0; name && EnumDisplayDevicesA(nullptr, i, &dd, 0); i++) {
    if ((dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE) && !_stricmp(dd.DeviceName, name))
      return true;
    dd = {};
    dd.cb = sizeof(dd);
  }
  return false;
}

static NvAPI_Status __cdecl madeira_nv_DISP_GetDisplayIdByDisplayName(const char *displayName, NvU32 *displayId) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_DISP_GetDisplayIdByDisplayName(displayName, displayId);
  bool fallback = false;
  /* One display on this port: its adapter's name is that display, whatever
   * GetMonitorInfo calls the monitor. */
  if (s == NVAPI_NVIDIA_DEVICE_NOT_FOUND && displayId && madeira_nv_is_primary_adapter_name(displayName)) {
    *displayId = WMTGetPrimaryDisplayId();
    s = NVAPI_OK;
    fallback = true;
  }
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_DISP_GetDisplayIdByDisplayName(\"", madeira_nv_str(displayName, 64),
                             "\") -> ", int(s), s == NVAPI_OK && displayId ? str::format(" id=0x", std::hex, *displayId) : "",
                             fallback ? " (primary adapter name; GetMonitorInfo did not match)" : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_GetAssociatedNvidiaDisplayHandle(const char *szDisplayName, NvDisplayHandle *pNvDispHandle) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_GetAssociatedNvidiaDisplayHandle(szDisplayName, pNvDispHandle);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_GetAssociatedNvidiaDisplayHandle(\"", madeira_nv_str(szDisplayName, 64),
                             "\") -> ", int(s), s == NVAPI_OK && pNvDispHandle ? str::format(" handle=", (void *)*pNvDispHandle) : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_SYS_GetDriverAndBranchVersion(NvU32 *pDriverVersion, NvAPI_ShortString szBuildBranchString) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_SYS_GetDriverAndBranchVersion(pDriverVersion, szBuildBranchString);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_SYS_GetDriverAndBranchVersion -> ", int(s),
                             s == NVAPI_OK ? str::format(" version=", *pDriverVersion, " branch=\"",
                                                         madeira_nv_str(szBuildBranchString, NVAPI_SHORT_STRING_MAX), "\"") : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_GetDisplayDriverVersion(NvDisplayHandle hNvDisplay, NV_DISPLAY_DRIVER_VERSION *pVersion) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_GetDisplayDriverVersion(hNvDisplay, pVersion);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_GetDisplayDriverVersion(", (void *)hNvDisplay, ") -> ", int(s),
                             s == NVAPI_OK ? str::format(" version=", pVersion->drvVersion, " branch=\"",
                                                         madeira_nv_str(pVersion->szBuildBranchString, NVAPI_SHORT_STRING_MAX),
                                                         "\" adapter=\"", madeira_nv_str(pVersion->szAdapterString, NVAPI_SHORT_STRING_MAX), "\"")
                                           : pVersion ? str::format(" (struct version 0x", std::hex, pVersion->version, ")") : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_EnumPhysicalGPUs(NvPhysicalGpuHandle nvGPUHandle[NVAPI_MAX_PHYSICAL_GPUS], NvU32 *pGpuCount) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_EnumPhysicalGPUs(nvGPUHandle, pGpuCount);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_EnumPhysicalGPUs -> ", int(s),
                             s == NVAPI_OK ? str::format(" count=", *pGpuCount, " first=", (void *)nvGPUHandle[0]) : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_GPU_GetConnectedDisplayIds(NvPhysicalGpuHandle hPhysicalGpu, NV_GPU_DISPLAYIDS *pDisplayIds,
                                                                  NvU32 *pDisplayIdCount, NvU32 flags) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_GPU_GetConnectedDisplayIds(hPhysicalGpu, pDisplayIds, pDisplayIdCount, flags);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_GPU_GetConnectedDisplayIds(", (void *)hPhysicalGpu, ", flags=0x", std::hex, flags,
                             ") -> ", std::dec, int(s), pDisplayIdCount ? str::format(" count=", *pDisplayIdCount) : "",
                             s == NVAPI_OK && pDisplayIds && pDisplayIdCount && *pDisplayIdCount
                                 ? str::format(" first=0x", std::hex, pDisplayIds[0].displayId) : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_DISP_GetGDIPrimaryDisplayId(NvU32 *displayId) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_DISP_GetGDIPrimaryDisplayId(displayId);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_DISP_GetGDIPrimaryDisplayId -> ", int(s),
                             s == NVAPI_OK ? str::format(" id=0x", std::hex, *displayId) : ""));
  return s;
}

static NvAPI_Status __cdecl madeira_nv_GPU_GetAdapterIdFromPhysicalGpu(NvPhysicalGpuHandle hPhysicalGpu, void *pOSAdapterId) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_GPU_GetAdapterIdFromPhysicalGpu(hPhysicalGpu, pOSAdapterId);
  if (madeira_nv_should_log(calls, s)) {
    LUID l = s == NVAPI_OK ? *reinterpret_cast<LUID *>(pOSAdapterId) : LUID{};
    Logger::info(str::format("[nvapi] NvAPI_GPU_GetAdapterIdFromPhysicalGpu(", (void *)hPhysicalGpu, ") -> ", int(s),
                             " luid=", std::hex, l.HighPart, ":", l.LowPart));
  }
  return s;
}

static NvAPI_Status __cdecl madeira_nv_GPU_GetFullName(NvPhysicalGpuHandle hPhysicalGpu, NvAPI_ShortString szName) {
  static std::atomic<unsigned> calls{0};
  NvAPI_Status s = dxmt::NvAPI_GPU_GetFullName(hPhysicalGpu, szName);
  if (madeira_nv_should_log(calls, s))
    Logger::info(str::format("[nvapi] NvAPI_GPU_GetFullName -> ", int(s),
                             s == NVAPI_OK ? str::format(" \"", madeira_nv_str(szName, NVAPI_SHORT_STRING_MAX), "\"") : ""));
  return s;
}

'''

if src.count(qi) != 1:
    sys.exit("patch-nvapi-trace: nvapi_QueryInterface anchor not found")
src = src.replace(qi, wrappers + qi)

detailed = {
    "NvAPI_DISP_GetDisplayIdByDisplayName": "madeira_nv_DISP_GetDisplayIdByDisplayName",
    "NvAPI_GetAssociatedNvidiaDisplayHandle": "madeira_nv_GetAssociatedNvidiaDisplayHandle",
    "NvAPI_SYS_GetDriverAndBranchVersion": "madeira_nv_SYS_GetDriverAndBranchVersion",
    "NvAPI_GetDisplayDriverVersion": "madeira_nv_GetDisplayDriverVersion",
    "NvAPI_EnumPhysicalGPUs": "madeira_nv_EnumPhysicalGPUs",
    "NvAPI_GPU_GetConnectedDisplayIds": "madeira_nv_GPU_GetConnectedDisplayIds",
    "NvAPI_DISP_GetGDIPrimaryDisplayId": "madeira_nv_DISP_GetGDIPrimaryDisplayId",
    "NvAPI_GPU_GetAdapterIdFromPhysicalGpu": "madeira_nv_GPU_GetAdapterIdFromPhysicalGpu",
    "NvAPI_GPU_GetFullName": "madeira_nv_GPU_GetFullName",
}
# Status only. Per-frame entry points stay unwrapped.
status_only = [
    "NvAPI_Initialize", "NvAPI_EnumLogicalGPUs", "NvAPI_GetPhysicalGPUsFromDisplay",
    "NvAPI_EnumNvidiaDisplayHandle", "NvAPI_Disp_GetHdrCapabilities", "NvAPI_DISP_GetMonitorCapabilities",
    "NvAPI_GPU_GetArchInfo", "NvAPI_GPU_GetAllClockFrequencies", "NvAPI_GPU_GetGpuCoreCount",
    "NvAPI_GPU_GetBusId", "NvAPI_GPU_GetBusType", "NvAPI_GPU_GetMemoryInfo", "NvAPI_GPU_GetMemoryInfoEx",
    "NvAPI_GPU_GetPstates20", "NvAPI_GPU_GetLogicalGpuInfo", "NvAPI_GetLogicalGPUFromPhysicalGPU",
    "NvAPI_GetPhysicalGPUsFromLogicalGPU", "NvAPI_GetAssociatedDisplayOutputId", "NvAPI_GPU_GetPCIIdentifiers",
    "NvAPI_GPU_GetThermalSettings", "NvAPI_D3D_GetCurrentSLIState",
]
if upstream:   # patch-nvapi-gpu-info.py logged these two itself; upstream's do not
    status_only += ["NvAPI_GPU_GetPhysicalFrameBufferSize", "NvAPI_GPU_GetVirtualFrameBufferSize"]

body_start = src.index(qi)
head, body = src[:body_start], src[body_start:]
for fn, wrapper in detailed.items():
    pat = re.compile(r"(  case (0x[0-9a-fA-F]{8}):\n    return \(void \*\)&)" + fn + ";\n")
    if len(pat.findall(body)) != 1:
        sys.exit("patch-nvapi-trace: QueryInterface case for %s not found" % fn)
    body = pat.sub(lambda m: m.group(1) + wrapper + ";\n", body)
for fn in status_only:
    pat = re.compile(r"(  case (0x[0-9a-fA-F]{8}):\n    return \(void \*\)&)" + fn + ";\n")
    if len(pat.findall(body)) != 1:
        sys.exit("patch-nvapi-trace: QueryInterface case for %s not found" % fn)
    body = pat.sub(lambda m: m.group(1) + "madeira_nv_trace<&dxmt::" + fn + ", " + m.group(2) + "u>::call;\n", body)
src = head + body

open(path, "w").write(src)
print("patch-nvapi-trace: %d entry points traced in %s" % (len(detailed) + len(status_only), path))
