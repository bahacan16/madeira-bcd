#!/usr/bin/env python3
"""FEX's WinAPI shims: find the TEB without trusting x18.

Source/Windows/Common/Priv.h's GetCurrentTEB() is NtCurrentTeb(), a read of
x18. On the iOS host x18 is not preserved by Apple's runtime and reads 0 on
some threads (Module.cpp's IOSLoadTEB exists for this; the JIT emitters and
Module.S use the same TSD slot). God of War on build 239 (log 2026-09-30
10:50): FEX's TlsGetValue shim ran with x18 = 0 and read TEB->TlsSlots[1] at
address 0x1488 -- an access violation inside FEX, the intro video stops.

With the patch, the ARM64EC module's GetCurrentTEB() takes the TEB Wine
publishes in the thread's TSD slot (TPIDRRO_EL0 & ~7, + IosTebTsdOffset,
defined in FEXCore's Arm64Emitter.cpp) and falls back to x18 only while that
offset is not yet known or the slot is empty. The WOW64 module is unchanged.

Usage: patch-fex-ios-teb-tsd.py FEX/Source/Windows/Common/Priv.h
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: GetCurrentTEB without x18"
if marker in src:
    print("already patched")
    sys.exit(0)
# willfaust/FEX#6 (89d0407, FEX 3bec2ac): GetCurrentTEB reads the TSD slot
# upstream (IosTebTsdOffset + TPIDRRO_EL0), for both Windows modules.
_body = src[src.find("static inline __TEB* GetCurrentTEB()"):][:1200]
if "IosTebTsdOffset" in _body and "TPIDRRO_EL0" in _body:
    print("Priv.h: GetCurrentTEB already takes the TEB from the TSD slot upstream (willfaust/FEX#6); nothing to do")
    sys.exit(0)

old = """static inline __TEB* GetCurrentTEB() {
  return reinterpret_cast<__TEB*>(NtCurrentTeb());
}
"""
if src.count(old) != 1:
    sys.exit("patch-fex-ios-teb-tsd: GetCurrentTEB anchor not found")
src = src.replace(old, """/* madeira-bcd: GetCurrentTEB without x18 (tools/patch-fex-ios-teb-tsd.py). On the iOS host
 * x18 reads 0 on some threads; the TEB Wine publishes in the thread's TSD slot does not. */
#if defined(FEX_IOS_HOST)
extern "C" uint32_t IosTebTsdOffset;
static inline __TEB* GetCurrentTEB() {
  const uint32_t Off = IosTebTsdOffset;
  if (Off) {
    uintptr_t Tpidrro;
    __asm__ volatile("mrs %0, TPIDRRO_EL0" : "=r"(Tpidrro));
    auto* Teb = *reinterpret_cast<__TEB**>((Tpidrro & ~uintptr_t(7)) + Off);
    if (Teb) {
      return Teb;
    }
  }
  return reinterpret_cast<__TEB*>(NtCurrentTeb());
}
#else
static inline __TEB* GetCurrentTEB() {
  return reinterpret_cast<__TEB*>(NtCurrentTeb());
}
#endif
""")
if "#include <cstdint>" not in src:
    src = src.replace("#include <exception>\n", "#include <cstdint>\n#include <exception>\n", 1)
open(path, "w").write(src)
print("Priv.h: GetCurrentTEB reads the TEB from the TSD slot in the ARM64EC module")
