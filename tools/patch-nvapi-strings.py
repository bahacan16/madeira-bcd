#!/usr/bin/env python3
"""NUL-terminate the strings DXMT's NVAPI hands back.

dxmt/src/nvapi/nvapi.cpp copies its strings with
memcpy(dst, s.c_str(), s.size()) -- without the terminating NUL. The caller's
NvAPI_ShortString (char[64]) keeps whatever was on its stack after the text,
so NvAPI_SYS_GetDriverAndBranchVersion's branch ("r<sdk>_000"),
NvAPI_GetDisplayDriverVersion's branch and adapter strings,
NvAPI_GetInterfaceVersionString and NvAPI_GPU_GetFullName return a different
string from run to run. Ghost of Tsushima reads the driver version through
NVAPI and fails its check only on some launches ("[NxApp] Failed to get GPU
Driver Info", then "No installed graphics card"): 2026-09-28 20:39 passed and
20:41 failed on the same build; three launches on 2026-10-01 failed. Whether
this is that cause is not proven; the missing terminator is a bug either way.

Every `memcpy(X, Y.c_str(), Y.size());` becomes a bounded copy that always
terminates (the destinations are NvAPI_ShortString, 64 bytes). Applied by
tools/build-dxmt-nvapi.sh to its copy of nvapi.cpp after
tools/patch-dxmt-nvapi.py; the dxmt submodule is not modified. Named
patch-nvapi-* so the i386 farm cache key (tools/patch-dxmt-*.py) stays.
Usage: patch-nvapi-strings.py <copy of nvapi.cpp>
"""
import re
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: NUL-terminated NVAPI strings"
if marker in src:
    print("already patched")
    sys.exit(0)
if "CopyShortString(" in src:   # willfaust/dxmt#11 (37ec901)
    print("NUL-terminated NVAPI strings are upstream (willfaust/dxmt#11, CopyShortString); nothing to do")
    sys.exit(0)

pattern = re.compile(r"memcpy\(([^,;]+), (\w+)\.c_str\(\), \2\.size\(\)\);")
src, n = pattern.subn(r"madeira_nvapi_copy(\1, \2);", src)
if n < 4:
    sys.exit(f"patch-nvapi-strings: expected at least 4 unterminated string copies, found {n}")

helper = """/* madeira-bcd: NUL-terminated NVAPI strings (tools/patch-nvapi-strings.py) --
 * the destinations are NvAPI_ShortString (64 bytes); always terminate. */
static void madeira_nvapi_copy(char *dst, const std::string &s) {
  size_t n = s.size() < NVAPI_SHORT_STRING_MAX - 1 ? s.size() : NVAPI_SHORT_STRING_MAX - 1;
  memcpy(dst, s.c_str(), n);
  dst[n] = 0;
}

"""
anchor = "NVAPI_INTERFACE\nNvAPI_Initialize() {"
if src.count(anchor) != 1:
    sys.exit("patch-nvapi-strings: NvAPI_Initialize anchor not found")
src = src.replace(anchor, helper + anchor)
if re.search(r"\.c_str\(\), \w+\.size\(\)\)", src):
    sys.exit("patch-nvapi-strings: an unterminated copy is left")
open(path, "w").write(src)
print(f"patched {path}: {n} string copies now terminated")
