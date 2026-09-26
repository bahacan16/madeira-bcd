#!/usr/bin/env python3
"""Let a game opt in to AVX/AVX2 on the iOS ARM64EC FEX build.

FEX runs AVX on 128-bit NEON hosts by splitting each 256-bit operation in two,
and everywhere else FEX enables it by default. The iOS path of
FetchHostFeatures (Source/Windows/Common/CPUFeatures.cpp) builds its
HostFeatures by hand and never sets SupportsAVX, and it returns before the
HostFeatures config override is read, so there is no way to turn it on.

Titles compiled for AVX (Ghost of Tsushima) then die on their first VEX
instruction with STATUS_ILLEGAL_INSTRUCTION. Titles that check CPUID (RDR2)
take their SSE paths, which upstream measured and chose, so the default stays
off: MADEIRA_FEX_AVX=1 in the environment turns AVX on for that launch. CPUID,
XCR0 and IsProcessorFeaturePresent all derive from SupportsAVX, so the guest
sees one consistent answer.

Usage: patch-fex-ios-avx.py FEX/Source/Windows/Common/CPUFeatures.cpp
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: MADEIRA_FEX_AVX"
if marker in src:
    print("already patched")
    sys.exit(0)

anchor = """  HostFeatures.SupportsAFP = true;
  HostFeatures.CPUMIDRs.push_back(0u);
  HostFeatures.HostType = HostType;
  return HostFeatures;
#else"""
if src.count(anchor) != 1:
    sys.exit("patch-fex-ios-avx: iOS FetchHostFeatures anchor not found")

src = src.replace(anchor, """  HostFeatures.SupportsAFP = true;
  HostFeatures.CPUMIDRs.push_back(0u);
  HostFeatures.HostType = HostType;
  /* madeira-bcd: MADEIRA_FEX_AVX=1 opts this launch in to AVX/AVX2 through
   * FEX's 128-bit emulation (tools/patch-fex-ios-avx.py). */
  if (const char* Avx = getenv("MADEIRA_FEX_AVX"); Avx && Avx[0] == '1') {
    HostFeatures.SupportsAVX = true;
    HostFeatures.SupportsAES256 = HostFeatures.SupportsAES;
  }
  return HostFeatures;
#else""")

inc = "#include <windows.h>\n"
if src.count(inc) != 1:
    sys.exit("patch-fex-ios-avx: include anchor not found")
src = src.replace(inc, "#include <cstdlib>\n" + inc)

open(path, "w").write(src)
print("patched " + path)
