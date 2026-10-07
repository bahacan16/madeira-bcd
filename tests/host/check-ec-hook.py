#!/usr/bin/env python3
"""[ec-hook]: report a program's inline patch of a pool-copied image's code.

GTA V Enhanced (licensed, build 427, 2026-10-07 13:02): the game stops at
"Initializing" because its Social Club UI never starts. Its
SocialClubD3D12Renderer.dll hooks IDXGISwapChain::Present (dxgi.dll rva 0x4ce8,
vtable slot 8) by writing a jump into dxgi.dll's code ("Add SMC interval:
73AB904000"), but dxgi.dll is ARM64EC and runs from its JIT-pool copy, and the
sync after the protection restore skips .text, so the patch never reaches the
code that runs. ios_ec_hook_report logs each such patch: where, the bytes
written, the copy's bytes, and where a jump in them leads.

Checks, without a Wine run:
  - NtProtectVirtualMemory's sync calls the report on the .text range it skips,
    after the partial copy, with the PE view and the copy's RW alias;
  - the report compiled against stubs: a jmp rel32 and a jmp [rip+0] patch are
    reported with their target, an unpatched range prints nothing, the switch
    MADEIRA_EC_HOOK_TRACE=0 silences it, and output stops after 32 lines;
  - the CI workflow runs this check.
Needs python3 and a C compiler.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
native = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
failures = []


def check(cond, what):
    print(('PASS ' if cond else 'FAIL ') + what)
    if not cond:
        failures.append(what)


start = native.index('static void ios_ec_hook_report(')
report = native[start:native.index('\n}', start) + 2] + '\n'

sync = native.index('iOS JIT IAT sync: partial copy')
call = native.find('ios_ec_hook_report( idx,', sync)
check(call != -1 and call - sync < 600, 'the .text-skipping sync calls ios_ec_hook_report right after its partial copy')
check('(const unsigned char *)jit_rw_dest + overlap_start' in native[call:call + 400],
      'the report compares the PE view with the copy RW alias over the skipped range')
check(native.index('static void ios_ec_hook_report(') < native.index('NTSTATUS WINAPI NtProtectVirtualMemory('),
      'the report is defined before NtProtectVirtualMemory')
check('MADEIRA_EC_HOOK_TRACE' in report and 'lines < 32' in report, 'switch and line cap present')

harness = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
typedef int BOOL; typedef unsigned long ULONG_PTR; typedef unsigned long SIZE_T;
struct teb { struct { void *UniqueProcess, *UniqueThread; } ClientId; };
static struct teb the_teb = {{0, (void *)0x414}};
static struct teb *NtCurrentTeb(void) { return &the_teb; }
struct ios_jit_mapping { void *pe_base, *jit_base; };
static struct ios_jit_mapping ios_jit_mappings[1];
static BOOL virtual_check_buffer_for_read(const void *p, SIZE_T n) { (void)n; return p != 0; }
static int ios_image_section_describe(unsigned long long va, char *buf, size_t len, unsigned long long *b)
{ (void)b; snprintf(buf, len, "rva %#llx, .text", va - (unsigned long long)(uintptr_t)ios_jit_mappings[0].pe_base); return 1; }
''' + report + r'''
static unsigned char pe[0x2000], cp[0x2000], tgt[16] = {0xff, 0x25};
int main(int argc, char **argv)
{
    int i;
    memset(pe, 0x1f, sizeof pe); memcpy(cp, pe, sizeof pe);
    ios_jit_mappings[0].pe_base = pe; ios_jit_mappings[0].jit_base = cp;
    if (argc > 1 && argv[1][0] == 'c')            /* cap: 40 patches, 4 per call */
    {
        for (i = 0; i < 40; i++) pe[0x100 * i / 2 + 0x10] = 0xcc;
        for (i = 0; i < 12; i++) ios_ec_hook_report(0, pe, cp, sizeof pe);
        return 0;
    }
    ios_ec_hook_report(0, cp, cp, sizeof pe);    /* unpatched: silent */
    pe[0xce8] = 0xff; pe[0xce9] = 0x25; memset(pe + 0xcea, 0, 4);
    { unsigned long long t = (uintptr_t)tgt; memcpy(pe + 0xcee, &t, 8); }
    ios_ec_hook_report(0, pe, cp, sizeof pe);
    memcpy(pe, cp, sizeof pe);
    pe[0x500] = 0xe9; { int32_t rel = 0x1000 - 0x505; memcpy(pe + 0x501, &rel, 4); }
    ios_ec_hook_report(0, pe, cp, sizeof pe);
    return 0;
}
'''

with tempfile.TemporaryDirectory() as tmp:
    src, exe = os.path.join(tmp, 'h.c'), os.path.join(tmp, 'h')
    Path(src).write_text(harness)
    cc = os.environ.get('CC', 'cc')
    r = subprocess.run([cc, '-Wall', '-Werror', '-Wno-unused-function', '-o', exe, src], capture_output=True, text=True)
    check(r.returncode == 0, 'report compiles against stubs' + ('' if r.returncode == 0 else ': ' + r.stderr[:400]))
    if r.returncode == 0:
        out = subprocess.run([exe], capture_output=True, text=True).stderr.splitlines()
        check(len(out) == 2, 'two patches reported, the unpatched range silent (%d lines)' % len(out))
        if len(out) == 2:
            check('rva 0xce8' in out[0] and 'wrote: ff 25 00 00 00 00' in out[0] and 'copy runs: 1f 1f' in out[0],
                  'jmp [rip+0] patch: place, written bytes and copy bytes')
            check('bytes there: ff 25' in out[0] and '(rva ' in out[0], 'jmp [rip+0] patch: the slot target is followed')
            check('rva 0x500' in out[1] and 'wrote: e9 fb 0a 00 00' in out[1] and 'rva 0x1000' in out[1],
                  'jmp rel32 patch: target pe+0x1000')
        env = dict(os.environ, MADEIRA_EC_HOOK_TRACE='0')
        out = subprocess.run([exe], capture_output=True, text=True, env=env).stderr.splitlines()
        check(out == [], 'MADEIRA_EC_HOOK_TRACE=0 prints nothing')
        out = subprocess.run([exe, 'cap'], capture_output=True, text=True).stderr.splitlines()
        check(len(out) == 32, 'at most 32 lines per session (%d)' % len(out))

wf = (root / '.github/workflows/build-ipa.yml').read_text()
check('python3 tests/host/check-ec-hook.py' in wf, 'the CI workflow runs this check')

print('%d failure(s)' % len(failures))
sys.exit(1 if failures else 0)
