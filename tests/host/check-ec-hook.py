#!/usr/bin/env python3
"""Bounded, read-only code diagnostics with a reserved graphics-jump budget."""
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


start = native.index('static size_t ios_ec_hook_jump_target(')
report_start = native.index('static void ios_ec_hook_report(', start)
report = native[start:native.index('\n}', report_start) + 2] + '\n'

sync = native.index('/* Compare code before any copy overwrites .hexpthk changes.')
copy = native.index('memcpy(jit_rw_dest, base, size);', sync)
block = native[sync:copy]
check('ios_ec_hook_report( idx,' in block and 'code_ranges[r]' in block,
      'all executable sections are compared before synchronization, including thunks')
check('ios_patch_length <= rgn_end - requested' in block, 'requested byte ranges are bounds checked')

harness = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <strings.h>
#include <sys/mman.h>
typedef int BOOL; typedef unsigned long ULONG_PTR; typedef unsigned long SIZE_T;
struct teb { struct { void *UniqueProcess, *UniqueThread; } ClientId; };
static struct teb the_teb = {{0, (void *)0x414}};
static struct teb *NtCurrentTeb(void) { return &the_teb; }
struct ios_jit_mapping { void *pe_base, *jit_base; size_t size; };
static struct ios_jit_mapping ios_jit_mappings[1];
static const char *module_name = "kernelbase.dll";
static const char *ios_pe_module_name(const void *p, size_t n) { (void)p; (void)n; return module_name; }
static BOOL virtual_check_buffer_for_read(const void *p, SIZE_T n) { (void)n; return p != 0; }
static int ios_image_section_describe(unsigned long long va, char *buf, size_t len, unsigned long long *b)
{ (void)b; snprintf(buf, len, "rva %#llx, .text", va - (unsigned long long)(uintptr_t)ios_jit_mappings[0].pe_base); return 1; }
''' + report + r'''
static unsigned char pe[0x10000], cp[0x10000], tgt[16] = {0xff, 0x25};
int main(int argc, char **argv)
{
    int i;
    memset(pe, 0x1f, sizeof pe); memcpy(cp, pe, sizeof pe);
    ios_jit_mappings[0].pe_base = pe; ios_jit_mappings[0].jit_base = cp; ios_jit_mappings[0].size = sizeof pe;
    if (argc > 1 && argv[1][0] == 'r')            /* cap: 40 patches, 4 per call */
    {
        for (i = 0; i < 40; i++) pe[0x100 * i / 2 + 0x10] = 0xcc;
        for (i = 0; i < 12; i++) ios_ec_hook_report(0, pe, cp, sizeof pe);
        memcpy(pe, cp, sizeof pe);
        for (i = 0; i < 40; i++) {
            int at = 0x20 + 0x100 * i; int32_t rel = 0x7000 - at - 5;
            pe[at] = 0xe9; memcpy(pe + at + 1, &rel, 4);
        }
        for (i = 0; i < 12; i++) ios_ec_hook_report(0, pe, cp, sizeof pe);
        module_name = "DXGI.DLL";
        for (i = 0; i < 12; i++) ios_ec_hook_report(0, pe, cp, sizeof pe);
        return 0;
    }
    if (argc > 1 && argv[1][0] == 'e') {
        size_t page = sysconf(_SC_PAGESIZE);
        unsigned char *p = mmap(0, page * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
        if (p == MAP_FAILED || mprotect(p + page, page, PROT_NONE)) return 2;
        p[page - 1] = 0xff;
        ios_ec_hook_report(0, p + page - 1, cp, 1);
        p[page - 5] = 0xff; p[page - 4] = 0x25;
        ios_ec_hook_report(0, p + page - 5, cp, 5);
        munmap(p, page * 2); return 0;
    }
    ios_ec_hook_report(0, cp, cp, sizeof pe);    /* unpatched: silent */
    pe[0xce8] = 0xff; pe[0xce9] = 0x25; memset(pe + 0xcea, 0, 4);
    { unsigned long long t = (uintptr_t)tgt; memcpy(pe + 0xcee, &t, 8); }
    ios_ec_hook_report(0, pe, cp, sizeof pe);
    memcpy(pe, cp, sizeof pe);
    pe[0x500] = 0xe9; { int32_t rel = 0x1000 - 0x505; memcpy(pe + 0x501, &rel, 4); }
    ios_ec_hook_report(0, pe, cp, sizeof pe);
    memcpy(pe, cp, sizeof pe);
    pe[0x800] = cp[0x800] = 0x48;
    pe[0x801] = 0x8b; cp[0x801] = 0xb8;
    { unsigned long long t = (uintptr_t)tgt; memcpy(cp + 0x802, &t, 8); }
    cp[0x80a] = 0xff; cp[0x80b] = 0xe0;
    module_name = "d3d12.dll";
    ios_ec_hook_report(0, pe, cp, sizeof pe);
    memcpy(cp, pe, sizeof pe);
    cp[0x900] = 0xe9; { int32_t rel = 0x1000 - 0x905; memcpy(cp + 0x901, &rel, 4); }
    ios_jit_mappings[0].jit_base = cp + 0x4000;  /* RX and RW addresses differ */
    printf("expected-pool-target=%#llx\n", (unsigned long long)(uintptr_t)(cp + 0x5000));
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
        result = subprocess.run([exe], capture_output=True, text=True)
        check(result.returncode == 0, 'production decoder and reporting harness completes')
        out = result.stderr.splitlines()
        check(len(out) == 4, 'four patches reported, the unpatched range silent (%d lines)' % len(out))
        if len(out) == 4:
            check('rva 0xce8' in out[0] and 'PE bytes: ff 25 00 00 00 00' in out[0] and 'pool before sync: 1f 1f' in out[0],
                  'jmp [rip+0] patch: place, written bytes and copy bytes')
            check('bytes there: ff 25' in out[0] and '(rva ' in out[0], 'jmp [rip+0] patch: the slot target is followed')
            check('rva 0x500' in out[1] and 'PE bytes: e9 fb 0a 00 00' in out[1] and 'rva 0x1000' in out[1],
                  'jmp rel32 patch: target pe+0x1000')
            check('rva 0x800' in out[2] and 'view=pool' in out[2] and 'scope=graphics-jump' in out[2]
                  and 'pool before sync: 48 b8' in out[2],
                  'pool-only patch with a shared 48 opcode prefix is recognized')
            expected = result.stdout.strip().split('=', 1)[1]
            check('rva 0x900' in out[3] and 'view=pool' in out[3] and f'jump -> {expected} ' in out[3],
                  'pool-relative jump uses the RX address, not the PE or RW view')
        env = dict(os.environ, MADEIRA_EC_HOOK_TRACE='0')
        out = subprocess.run([exe], capture_output=True, text=True, env=env).stderr.splitlines()
        check(out == [], 'MADEIRA_EC_HOOK_TRACE=0 prints nothing')
        out = subprocess.run([exe, 'reserved'], capture_output=True, text=True).stderr.splitlines()
        check(sum('scope=difference' in l for l in out) == 8, 'ordinary differences stop after 8 lines')
        check(sum('scope=other-jump' in l for l in out) == 16, 'other jumps have their own 16-line budget')
        check(sum('scope=graphics-jump' in l for l in out) == 32,
              '32 graphics-jump records survive after startup consumes both other budgets')
        r = subprocess.run([exe, 'end'], capture_output=True, text=True)
        check(r.returncode == 0, 'truncated jumps at an unreadable page boundary do not overread')

wf = (root / '.github/workflows/build-ipa.yml').read_text()
check('python3 tests/host/check-ec-hook.py' in wf, 'the CI workflow runs this check')

print('%d failure(s)' % len(failures))
sys.exit(1 if failures else 0)
