#!/usr/bin/env python3
"""IDXGIAdapter::CheckInterfaceSupport's UMD version from D3DKMT, for DXMT's
DXGI adapter (opt-in at run time: env.MADEIRA_KMT_ADAPTER = 1).

DXMT answers CheckInterfaceSupport(IDXGIDevice / ID3D10Device*) with
umd_version = ~0 ("we can't really reconstruct the version numbers returned
by Windows drivers from Metal"). Windows' DXGI returns the user-mode driver
version the kernel reports (D3DKMTQueryAdapterInfo,
KMTQAITYPE_UMD_DRIVER_VERSION) -- the registry DriverVersion "a.b.c.d" as
a<<48 | b<<32 | c<<16 | d -- and engines that check the graphics driver read
it there ("...or update your graphics driver", GTA V Enhanced,
ERR_GFX_D3D_NOD3D12, build 314).

With MADEIRA_KMT_ADAPTER=1 win32u answers that query for the adapter's LUID
(build/win32u-unix/d3dkmt_ios.c) with the registry GPU's DriverVersion
(32.0.15.8157 = 581.57 for the NVIDIA identity), so this asks D3DKMT on the adapter's
own KMT handle (local_kmt_, opened from the same LUID in the constructor),
exactly as Windows does. Without the switch, or when the query fails, the
value stays ~0 (upstream). The first 4 calls are logged ("[dxgi-src]
CheckInterfaceSupport <guid> -> ..." and, with the switch, the version).

Applied by tools/build-dxgi-dll.sh to a COPY of dxgi_adapter.cpp; the dxmt
submodule is not modified (and the i386 farm, which builds DXMT itself, is not
affected: this name is outside its tools/patch-dxmt-*.py cache key). Idempotent;
exits 1 with a message if an anchor is missing.
Usage: patch-dxgi-umd-version.py <copy of dxmt/src/dxgi/dxgi_adapter.cpp>
"""
import sys

MARKER = "madeira-bcd: UMD version from D3DKMT"


def main(path):
    src = open(path).read()
    if MARKER in src:
        print("patch-dxgi-umd-version: already patched")
        return 0
    if "GetUmdDriverVersion()" in src and "KMTQAITYPE_UMD_DRIVER_VERSION" in src:
        # willfaust/dxmt#13 (75e3bb4): always asks D3DKMT (no MADEIRA_KMT_ADAPTER
        # gate here; win32u's D3DKMTQueryAdapterInfo still answers only with it).
        print("patch-dxgi-umd-version: the D3DKMT UMD version is upstream (willfaust/dxmt#13); nothing to do")
        return 0

    edits = [
        ("includes",
         "#include <mutex>\n",
         "#include <mutex>\n#include <atomic>\n"),
        ("helper after GetAdapterLuid",
         "LUID GetAdapterLuid(WMT::Device device) {\n"
         "    // NOTE: use big-endian registryID, be consistent with MVK\n"
         "  return std::bit_cast<LUID>(__builtin_bswap64(device.registryID()));\n"
         "}\n",
         "LUID GetAdapterLuid(WMT::Device device) {\n"
         "    // NOTE: use big-endian registryID, be consistent with MVK\n"
         "  return std::bit_cast<LUID>(__builtin_bswap64(device.registryID()));\n"
         "}\n"
         "\n"
         "/* " + MARKER + " (tools/patch-dxgi-umd-version.py).\n"
         " * With env.MADEIRA_KMT_ADAPTER = 1, win32u answers\n"
         " * D3DKMTQueryAdapterInfo(KMTQAITYPE_UMD_DRIVER_VERSION) for this adapter's\n"
         " * LUID with the registry GPU's DriverVersion in Windows' encoding\n"
         " * (a<<48|b<<32|c<<16|d), which is what Windows' DXGI hands out here.\n"
         " * Without the switch, or if the query fails, ~0 as upstream. */\n"
         "struct madeira_kmt_queryadapterinfo {\n"
         "  D3DKMT_HANDLE hAdapter;\n"
         "  UINT Type; /* KMTQUERYADAPTERINFOTYPE */\n"
         "  void *pPrivateDriverData;\n"
         "  UINT PrivateDriverDataSize;\n"
         "};\n"
         "extern \"C\" DECLSPEC_IMPORT NTSTATUS WINAPI\n"
         "D3DKMTQueryAdapterInfo(madeira_kmt_queryadapterinfo *desc);\n"
         "\n"
         "static bool madeira_kmt_adapter_switch() {\n"
         "  char v[8] = {};\n"
         "  DWORD n = GetEnvironmentVariableA(\"MADEIRA_KMT_ADAPTER\", v, sizeof(v));\n"
         "  if (n == 0 || n >= sizeof(v))\n"
         "    return false;\n"
         "  return !strcmp(v, \"1\") || !_stricmp(v, \"on\") || !_stricmp(v, \"true\") ||\n"
         "         !_stricmp(v, \"yes\");\n"
         "}\n"
         "\n"
         "static UINT64 madeira_dxgi_umd_version(D3DKMT_HANDLE kmt) {\n"
         "  static std::atomic<int> logged{0};\n"
         "  if (!madeira_kmt_adapter_switch())\n"
         "    return ~0ull;\n"
         "\n"
         "  UINT64 version = 0;\n"
         "  NTSTATUS status = (NTSTATUS)0xC000000D; /* STATUS_INVALID_PARAMETER: no KMT adapter */\n"
         "  if (kmt) {\n"
         "    madeira_kmt_queryadapterinfo query = {kmt, 18 /* KMTQAITYPE_UMD_DRIVER_VERSION */,\n"
         "                                          &version, sizeof(version)};\n"
         "    status = D3DKMTQueryAdapterInfo(&query);\n"
         "  }\n"
         "  if (logged.fetch_add(1) < 4) {\n"
         "    char line[200];\n"
         "    snprintf(line, sizeof(line),\n"
         "             \"[dxgi-src] CheckInterfaceSupport: UMD version %u.%u.%u.%u from D3DKMT \"\n"
         "             \"(MADEIRA_KMT_ADAPTER=1, hAdapter %#x, status %#lx)%s\",\n"
         "             (unsigned)(version >> 48) & 0xffff, (unsigned)(version >> 32) & 0xffff,\n"
         "             (unsigned)(version >> 16) & 0xffff, (unsigned)version & 0xffff, (unsigned)kmt,\n"
         "             (unsigned long)status, (status == 0 && version) ? \"\" : \" -- kept ~0\");\n"
         "    Logger::info(line);\n"
         "  }\n"
         "  return (status == 0 && version) ? version : ~0ull;\n"
         "}\n"),
        ("CheckInterfaceSupport",
         "    // We can't really reconstruct the version numbers\n"
         "    // returned by Windows drivers from Metal\n"
         "    if (SUCCEEDED(hr) && umd_version)\n"
         "      umd_version->QuadPart = ~0ull;\n",
         "    // We can't really reconstruct the version numbers\n"
         "    // returned by Windows drivers from Metal -- madeira-bcd: with\n"
         "    // MADEIRA_KMT_ADAPTER=1 D3DKMT has the registry's (see above).\n"
         "    if (SUCCEEDED(hr) && umd_version)\n"
         "      umd_version->QuadPart = madeira_dxgi_umd_version(local_kmt_);\n"
         "\n"
         "    static std::atomic<int> madeira_calls{0};\n"
         "    if (madeira_calls.fetch_add(1) < 4)\n"
         "      Logger::info(str::format(\"[dxgi-src] CheckInterfaceSupport \", guid, \" -> \",\n"
         "                               SUCCEEDED(hr) ? \"S_OK\" : \"DXGI_ERROR_UNSUPPORTED\"));\n"),
    ]

    for name, old, new in edits:
        if src.count(old) != 1:
            sys.stderr.write("patch-dxgi-umd-version: anchor for %s found %d times (expected 1); "
                             "dxgi_adapter.cpp changed upstream, update the patch\n" % (name, src.count(old)))
            return 1
        src = src.replace(old, new)

    open(path, "w").write(src)
    print("patch-dxgi-umd-version: CheckInterfaceSupport UMD version from D3DKMT added to", path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__.strip().splitlines()[-1] + "\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
