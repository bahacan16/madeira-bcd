#!/usr/bin/env python3
"""IDXGIFactory7 (and EnumAdapterByLuid) for DXMT's DXGI factory.

GTA V Enhanced asks CreateDXGIFactory* / QueryInterface for IDXGIFactory7
(a4966eed-76db-44da-84c1-ee9a7afb20a8). DXMT's MTLDXGIFactory stops at
IDXGIFactory6, so the game keeps a NULL factory ("GRAPHICS INFO / Factory :
None" in its crash report) and aborts with ERR_GFX_D3D_NOD3D12 (build 296,
log 2026-10-01 20:16: "DXGIFactory: Unknown interface query a4966eed-...").

IDXGIFactory7 adds RegisterAdaptersChangedEvent / UnregisterAdaptersChangedEvent.
Metal's adapter list never changes under a running process, so the event is
never signalled; registration succeeds with a nonzero cookie, as Windows does
(DXVK answers E_NOTIMPL, which games accept too). The base class becomes
IDXGIFactory7 so the two methods sit at the vtable slots the interface
defines -- answering Factory7 from the old class would hand the caller the
C++ destructor as RegisterAdaptersChangedEvent.

EnumAdapterByLuid was "not implemented" (DXGI_ERROR_NOT_FOUND for every LUID).
D3D12 engines and Streamline find the device's adapter with
ID3D12Device::GetAdapterLuid + EnumAdapterByLuid; madeira_d3d12 reports the
LUID DXMT derives from the Metal registry ID, so the lookup now finds it.

A once-per-process "[dxgi-src]" line names this build, so a log shows which
dxgi.dll a game ran with.

Applied by tools/build-dxgi-dll.sh to a COPY of dxgi_factory.cpp; the dxmt
submodule is not modified (and the i386 farm, which builds DXMT itself, is not
affected). Idempotent; exits 1 with a message if an anchor is missing.
Usage: patch-dxgi-factory7.py <copy of dxmt/src/dxgi/dxgi_factory.cpp>
"""
import sys

MARKER = "madeira-bcd: IDXGIFactory7"


def main(path):
    src = open(path).read()
    if MARKER in src:
        print("patch-dxgi-factory7: already patched")
        return 0
    if "MTLDXGIObject<IDXGIFactory7>" in src and "GetAdapterLuid(device)" in src:
        # willfaust/dxmt#12 (a18c93a, dxmt db546ee); upstream's committed
        # arm64ec dxgi.dll is built from it, so dxgi-src.dll adds nothing.
        print("patch-dxgi-factory7: IDXGIFactory7 and EnumAdapterByLuid are upstream (willfaust/dxmt#12); nothing to do")
        return 0

    edits = [
        ("includes",
         '#include "Metal.hpp"\n',
         '#include "Metal.hpp"\n#include <atomic>\n'),
        ("adapter lookup declaration",
         "Com<IMTLDXGIAdapter> CreateAdapter(WMT::Device Device,\n"
         "                                   IDXGIFactory2 *pFactory, Config &config);\n",
         "Com<IMTLDXGIAdapter> CreateAdapter(WMT::Device Device,\n"
         "                                   IDXGIFactory2 *pFactory, Config &config);\n"
         "LUID GetAdapterLuid(WMT::Device device);   /* dxgi_adapter.cpp */\n"
         "\n"
         "/* " + MARKER + " (tools/patch-dxgi-factory7.py). Names this build\n"
         " * once per process, so a log shows which dxgi.dll a game ran with. */\n"
         "#ifndef MADEIRA_DXGI_SRC_REV\n"
         "#define MADEIRA_DXGI_SRC_REV \"unknown\"\n"
         "#endif\n"
         "static void madeira_dxgi_src_note(REFIID riid) {\n"
         "  static std::atomic<bool> noted{false};\n"
         "  if (noted.exchange(true))\n"
         "    return;\n"
         "  Logger::info(str::format(\"[dxgi-src] madeira-bcd dxgi.dll from DXMT source \",\n"
         "                           MADEIRA_DXGI_SRC_REV, \" (IDXGIFactory7, EnumAdapterByLuid);\"\n"
         "                           \" first factory request \", riid));\n"
         "}\n"),
        ("factory base class",
         "class MTLDXGIFactory : public MTLDXGIObject<IDXGIFactory6> {",
         "class MTLDXGIFactory : public MTLDXGIObject<IDXGIFactory7> {"),
        ("QueryInterface",
         "        riid == __uuidof(IDXGIFactory5) || riid == __uuidof(IDXGIFactory6)) {",
         "        riid == __uuidof(IDXGIFactory5) || riid == __uuidof(IDXGIFactory6) ||\n"
         "        riid == __uuidof(IDXGIFactory7)) {"),
        ("EnumAdapterByLuid",
         "  HRESULT STDMETHODCALLTYPE EnumAdapterByLuid(LUID luid, REFIID iid,\n"
         "                                              void **adapter) override {\n"
         "    ERR(\"DXGIFactory::EnumAdapterByLuid: not implemented\");\n"
         "    return DXGI_ERROR_NOT_FOUND;\n"
         "  }\n",
         "  /* madeira-bcd: the adapter whose LUID (Metal registry ID, see\n"
         "   * GetAdapterLuid) matches; was \"not implemented\" for every LUID. */\n"
         "  HRESULT STDMETHODCALLTYPE EnumAdapterByLuid(LUID luid, REFIID iid,\n"
         "                                              void **adapter) override {\n"
         "    InitReturnPtr(adapter);\n"
         "    if (adapter == nullptr)\n"
         "      return DXGI_ERROR_INVALID_CALL;\n"
         "\n"
         "    auto devices = WMT::CopyAllDevices();\n"
         "    for (unsigned i = 0; i < devices.count(); i++) {\n"
         "      auto device = devices.object(i);\n"
         "      LUID l = GetAdapterLuid(device);\n"
         "      if (l.LowPart == luid.LowPart && l.HighPart == luid.HighPart) {\n"
         "        Com<IMTLDXGIAdapter> found = CreateAdapter(device, this, Config::getInstance());\n"
         "        return found->QueryInterface(iid, adapter);\n"
         "      }\n"
         "    }\n"
         "\n"
         "    static std::atomic<bool> logged{false};\n"
         "    if (!logged.exchange(true))\n"
         "      WARN(\"DXGIFactory::EnumAdapterByLuid: no adapter with LUID \", luid.HighPart, \":\", luid.LowPart);\n"
         "    return DXGI_ERROR_NOT_FOUND;\n"
         "  }\n"),
        ("IDXGIFactory7 methods",
         "private:\n  UINT flags_;\n",
         "  /* " + MARKER + ". Metal's adapters never change while a process\n"
         "   * runs, so the event is never signalled; registration succeeds with a\n"
         "   * nonzero cookie, as on Windows. */\n"
         "  HRESULT STDMETHODCALLTYPE RegisterAdaptersChangedEvent(HANDLE hEvent,\n"
         "                                                         DWORD *pdwCookie) override {\n"
         "    if (hEvent == nullptr || pdwCookie == nullptr)\n"
         "      return DXGI_ERROR_INVALID_CALL;\n"
         "\n"
         "    static std::atomic<DWORD> next_cookie{0};\n"
         "    DWORD cookie = ++next_cookie;\n"
         "    if (cookie == 0)\n"
         "      cookie = ++next_cookie;\n"
         "    *pdwCookie = cookie;\n"
         "\n"
         "    static std::atomic<bool> logged{false};\n"
         "    if (!logged.exchange(true))\n"
         "      Logger::info(str::format(\"[dxgi-src] RegisterAdaptersChangedEvent: cookie \", cookie,\n"
         "                               \", never signalled (adapters do not change)\"));\n"
         "    return S_OK;\n"
         "  }\n"
         "\n"
         "  HRESULT STDMETHODCALLTYPE UnregisterAdaptersChangedEvent(DWORD dwCookie) override {\n"
         "    return S_OK;\n"
         "  }\n"
         "\n"
         "private:\n  UINT flags_;\n"),
        ("CreateDXGIFactory2 note",
         "                                                void **ppFactory) {\n"
         "  try {\n",
         "                                                void **ppFactory) {\n"
         "  madeira_dxgi_src_note(riid);\n"
         "  try {\n"),
    ]

    for name, old, new in edits:
        if src.count(old) != 1:
            sys.stderr.write("patch-dxgi-factory7: anchor for %s found %d times (expected 1); "
                             "dxgi_factory.cpp changed upstream, update the patch\n" % (name, src.count(old)))
            return 1
        src = src.replace(old, new)

    open(path, "w").write(src)
    print("patch-dxgi-factory7: IDXGIFactory7 + EnumAdapterByLuid added to", path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__.strip().splitlines()[-1] + "\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
