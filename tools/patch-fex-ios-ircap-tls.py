#!/usr/bin/env python3
"""Keep FEX's IR-capture mark out of the game's thread-locals.

PassManager.cpp keeps the ml623 IR-capture mark in `thread_local uint64_t
IRCapRIP` everywhere but the WOW64 module, and Core.cpp clears it at the start
of every block compile (FEX_MadeiraIRCapClear). Implicit TLS is banned in
xtajit64 (Core.cpp says so; the WOW64 module already uses an atomic here): in
the ARM64EC module the access does not reach xtajit64's own TLS slot but the
main executable's TLS[0] block. IRCapRIP sits at offset 8 of xtajit64's TLS
template, so every compile wrote 0 over the game's thread-locals at +0x8..+0xf.

God of War keeps its allocator-stack index at TLS[0]+0xc (-1 = empty in its
template). Build 238 ([jumbo-tls], log 2026-09-30 10:28) caught the index at 0
before the game's first push and back at 0 right after it; the next push then
overwrote the first heap, and a static constructor took a NULL allocator
(read of 0x40, builds 234-238).

The patch makes IRCapRIP a process-wide atomic in the ARM64EC module too, as in
the WOW64 module: a diagnostic mark that two concurrent compiles may share.

Usage: patch-fex-ios-ircap-tls.py FEX/FEXCore/Source/Interface/IR/PassManager.cpp
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: no implicit TLS for IRCapRIP"
if marker in src:
    print("already patched")
    sys.exit(0)
# willfaust/FEX#7 (307fb4f, FEX 3bec2ac): upstream drops the WOW64-only
# condition itself ("Not thread_local in either Windows module").
if ("#if defined(FEX_IOS_HOST) && defined(_WIN32)\n// Not thread_local in either Windows module" in src
        and "std::atomic<uint64_t> IRCapRIP" in src):
    print("PassManager.cpp: IRCapRIP is already a process-wide atomic upstream (willfaust/FEX#7); nothing to do")
    sys.exit(0)

old = "#if defined(FEX_IOS_HOST) && defined(_WIN32) && !defined(ARCHITECTURE_arm64ec)\n// Not thread_local in the WOW64 module"
if src.count(old) != 1:
    sys.exit("patch-fex-ios-ircap-tls: IRCapRIP declaration anchor not found")
src = src.replace(old, "/* madeira-bcd: no implicit TLS for IRCapRIP (tools/patch-fex-ios-ircap-tls.py): in the ARM64EC\n"
                       " * module a thread_local here lands in the main executable's TLS[0] block. */\n"
                       "#if defined(FEX_IOS_HOST) && defined(_WIN32)\n// Not thread_local in the WOW64 module")
open(path, "w").write(src)
print("PassManager.cpp: IRCapRIP is a process-wide atomic in the ARM64EC module too")
