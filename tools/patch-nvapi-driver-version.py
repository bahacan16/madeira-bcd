#!/usr/bin/env python3
"""A real NVIDIA driver version for NVAPI callers.

DXMT's NVAPI answers NvAPI_SYS_GetDriverAndBranchVersion and
NvAPI_GetDisplayDriverVersion with 99999 (999.99) and the branch
"r<sdk>_000", while the registry, D3DKMT and DXGI name the driver
32.0.15.8157 (build/win32u-unix/sysparams_ios.c). GTA V Enhanced requires
572.60 or newer; the working Proton run reported 575.57. Both calls now
answer 581.57 (58157) on branch "r580_00", the same driver the registry names.

Applied by tools/build-dxmt-nvapi.sh after patch-nvapi-strings.py; the dxmt
submodule is not modified.
Usage: patch-nvapi-driver-version.py <copy of nvapi.cpp>
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: NVIDIA driver 581.57"
if marker in src:
    print("already patched")
    sys.exit(0)
if "kDriverVersion = 58157;" in src and 'kDriverBranch = "r580_00";' in src:   # willfaust/dxmt#11
    print("NVIDIA driver 581.57 / r580_00 is upstream (willfaust/dxmt#11); nothing to do")
    sys.exit(0)

for old, new, n in (
    ('std::string build_str = std::format("r{}_000", NVAPI_SDK_VERSION);',
     'std::string build_str = "r580_00"; /* madeira-bcd: NVIDIA driver 581.57 (tools/patch-nvapi-driver-version.py) */', 2),
    ("*pDriverVersion = 99999;", "*pDriverVersion = 58157;", 1),
    ("pVersion->drvVersion = 99999;", "pVersion->drvVersion = 58157;", 1),
):
    if src.count(old) != n:
        sys.exit("patch-nvapi-driver-version: expected %d x %r, found %d" % (n, old, src.count(old)))
    src = src.replace(old, new)

open(path, "w").write(src)
print("patch-nvapi-driver-version: NVAPI reports driver 581.57 (r580_00) in %s" % path)
