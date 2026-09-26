#!/usr/bin/env python3
"""Make the data exports of Wine's msvcr* DLLs visible to x64 importers.

Madeira runs ARM64EC DLLs from a copy in the JIT pool. Their code reaches its
own globals PC-relative, so the live .data is the pool copy's, and the PE
mapping's .data -- the address the loader binds an importer's data import to
-- stays the snapshot taken at load (build/ntdll-unix/server_ios.c, ml1131b).
The VC++ 2005 CRT startup in Crysis's Crysis64.exe reads msvcr80's _acmdln
through its import table, finds the PE copy's NULL, and faults.

At the end of DLL_PROCESS_ATTACH, once msvcrt_init_args() and friends have
filled _acmdln, __argv, _environ, _pgmptr and the rest, copy every writable,
non-executable section of the running image over the PE mapping's. It runs
before any importer's entry point, so the importer starts from the same values
the DLL sees. Values an importer later writes (_fmode, _commode) stay on its
side, as before. Built only for arm64ec, only into the DLLs
tools/build-wine-extra-dlls.sh builds (msvcr70-110 share dlls/msvcrt/main.c).

Usage: patch-wine-msvcrt-datasync.py wine/dlls/msvcrt/main.c
"""
import sys

path = sys.argv[1]
src = open(path).read()
if "madeira_sync_data_exports" in src:
    print("already patched"); sys.exit(0)

func = r'''
#ifdef __arm64ec__
/* madeira-bcd: see tools/patch-wine-msvcrt-datasync.py. */
static void madeira_sync_data_exports( HINSTANCE pe_image )
{
    extern IMAGE_DOS_HEADER __ImageBase;
    char *live = (char *)&__ImageBase;   /* PC-relative: the copy this code runs from */
    char *pe = (char *)pe_image;
    IMAGE_NT_HEADERS *nt;
    IMAGE_SECTION_HEADER *sec;
    unsigned int i;

    if (live == pe) return;
    nt = (IMAGE_NT_HEADERS *)(pe + ((IMAGE_DOS_HEADER *)pe)->e_lfanew);
    sec = IMAGE_FIRST_SECTION( nt );
    for (i = 0; i < nt->FileHeader.NumberOfSections; i++)
    {
        DWORD size = sec[i].Misc.VirtualSize;
        char *dst = pe + sec[i].VirtualAddress;
        MEMORY_BASIC_INFORMATION mbi;
        if (!size || (sec[i].Characteristics & IMAGE_SCN_MEM_EXECUTE) ||
            !(sec[i].Characteristics & IMAGE_SCN_MEM_WRITE)) continue;
        /* No VirtualProtect: on a pool-copied image Madeira's protect path
         * syncs the PE side INTO the running copy, which would first wipe
         * everything this DllMain just initialised (build 151: msvcr80's
         * lock table, then _lock recursing into a stack overflow). Copy only
         * while every page is already writable. */
        if (!VirtualQuery( dst, &mbi, sizeof(mbi) ) ||
            !(mbi.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE)) ||
            (char *)mbi.BaseAddress + mbi.RegionSize < dst + size) continue;
        memcpy( dst, live + sec[i].VirtualAddress, size );
    }
}
#endif

'''
anchor = "BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved)"
if src.count(anchor) != 1:
    sys.exit("patch-wine-msvcrt-datasync: DllMain anchor not found")
src = src.replace(anchor, func + anchor)

call_anchor = '    TRACE("finished process init\\n");\n'
if src.count(call_anchor) != 1:
    sys.exit("patch-wine-msvcrt-datasync: process-init anchor not found")
src = src.replace(call_anchor, call_anchor.replace('    TRACE', '''#ifdef __arm64ec__
    madeira_sync_data_exports( hinstDLL );
#endif
    TRACE''', 1))
open(path, "w").write(src)
print("patched " + path)
