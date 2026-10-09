#!/usr/bin/env python3
"""Opt-in A/B switches for the Sony ports' corrupted meshes; no Wine runs.

Horizon Zero Dawn (skinned characters collapse every few frames) and Ghost of
Tsushima (objects with another instance's transform for a frame) are the only
games that show it; GTA V Enhanced and RDR2 do not. Each switch is off by
default and changes nothing then:
  - madeira.cfg ignore-volatile-metadata = 1: build/ntdll-unix/virtual_ios.c
    clears an x64 image's VolatileMetadataPointer when it is mapped, so FEX
    orders the whole image by its TSO settings. Compiled here against Wine's
    headers and run on synthetic images: off, ARM64, no load config, a short
    load config, relocated and preferred-base pointers, the log cap;
  - d3d12-wave-ops = 0 (WaveOps FALSE) and d3d12-binding-tier = 3;
  - d3d12-raw-typed-views = 1: raw and structured buffer views also carry a
    texture-buffer view (the format choice is run here), a UAV with a counter
    keeps its counter view;
  - skin-check captures the draw's indices too (kinds 22/23, run here).
Needs python3 and a C compiler (CC, default cc).
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
vsrc = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
dsrc = (root / 'madeira-d3d12/src/pe/madeira_d3d12.c').read_text()
fails = []


def check(what, ok):
    print(('PASS: ' if ok else 'FAIL: ') + what)
    if not ok:
        fails.append(what)


def function(src, sig):
    a = src.index(sig)
    return src[a:src.index('\n}\n', a) + 3]


cc = os.environ.get('CC', 'cc')

# ---- ignore-volatile-metadata (unix side) ----
strip = function(vsrc, 'static void ios_ignore_volatile_metadata( char *base, SIZE_T total_size, IMAGE_NT_HEADERS *nt,')
harness = r'''
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "windef.h"
#include "winbase.h"
#include "winternl.h"
static int cfg_on, cfg_reads;
static int madeira_cfg_bool(const char *key, int def)
{ cfg_reads++; return strcmp(key, "ignore-volatile-metadata") ? def : cfg_on; }
static char out[1 << 16]; static size_t outn;
static int fake_dprintf(int fd, const char *f, ...)
{ va_list ap; int w; (void)fd; va_start(ap, f); w = vsnprintf(out + outn, sizeof out - outn, f, ap); va_end(ap); if (w > 0) outn += w; return w; }
static const char *debugstr_us(const UNICODE_STRING *us) { (void)us; return "L\"game.exe\""; }
#define dprintf fake_dprintf
''' + strip + r'''
#undef dprintf
#define SZ 0x20000
#define LC 0x3000
#define MD 0x5000
static unsigned char img[SZ];
static IMAGE_NT_HEADERS64 *mk(WORD machine, ULONGLONG image_base, ULONGLONG md_va, DWORD lc_size)
{
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)img;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(img + 0x80);
    IMAGE_LOAD_CONFIG_DIRECTORY64 *lc = (IMAGE_LOAD_CONFIG_DIRECTORY64 *)(img + LC);
    DWORD *md = (DWORD *)(img + MD);
    memset(img, 0, sizeof img);
    dos->e_magic = IMAGE_DOS_SIGNATURE; dos->e_lfanew = 0x80;
    nt->Signature = IMAGE_NT_SIGNATURE; nt->FileHeader.Machine = machine;
    nt->OptionalHeader.Magic = IMAGE_NT_OPTIONAL_HDR64_MAGIC;
    nt->OptionalHeader.ImageBase = image_base; nt->OptionalHeader.SizeOfImage = SZ;
    nt->OptionalHeader.NumberOfRvaAndSizes = 16;
    nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG].VirtualAddress = LC;
    nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG].Size = lc_size;
    lc->Size = lc_size; lc->VolatileMetadataPointer = md_va;
    md[0] = 24; md[1] = 1; md[2] = 0x6000; md[3] = 4 * 5424; md[4] = 0x9000; md[5] = 8 * 7;
    return nt;
}
static ULONGLONG vmp(void) { return ((IMAGE_LOAD_CONFIG_DIRECTORY64 *)(img + LC))->VolatileMetadataPointer; }
static int fail(const char *w) { printf("%s\nFAIL: %s\n", out, w); return 1; }
#define CALL(nt, pref) ios_ignore_volatile_metadata((char *)img, SZ, (IMAGE_NT_HEADERS *)(nt), (pref), NULL)

int main(int argc, char **argv)
{
    const char *s = argc > 1 ? argv[1] : "";
    IMAGE_NT_HEADERS64 *nt;
    int i;
    if (offsetof(IMAGE_LOAD_CONFIG_DIRECTORY64, VolatileMetadataPointer) != 0x100) return fail("VolatileMetadataPointer is not at 0x100");
    if (!strcmp(s, "off")) {
        cfg_on = 0;
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x140);
        CALL(nt, 0x140000000ull); CALL(nt, 0x140000000ull);
        if (vmp() != 0x7f0000000ull + MD || outn) return fail("off: the pointer changed or a line was logged");
        if (cfg_reads != 1) return fail("off: the setting is not read once and cached");
        printf("PASS: off, an image keeps its volatile metadata and nothing is logged\n");
        return 0;
    }
    cfg_on = 1;
    if (!strcmp(s, "relocated")) {
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x140);
        CALL(nt, 0x140000000ull);
        if (vmp()) return fail("a relocated pointer was not cleared");
        if (!strstr(out, "[fex-vmeta] madeira-bcd: L\"game.exe\" at ") ||
            !strstr(out, ": volatile metadata (5424 accesses, 7 ranges) cleared; FEX orders the whole image by its TSO settings (ignore-volatile-metadata)"))
            return fail("no [fex-vmeta] line with the table sizes");
        printf("PASS: on, a relocated image's pointer is cleared and its table sizes are logged\n");
        return 0;
    }
    if (!strcmp(s, "preferred")) {
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x140000000ull + MD, 0x140);   /* ml949: header rewritten, directory not applied */
        CALL(nt, 0x140000000ull);
        if (vmp() || !strstr(out, "(5424 accesses, 7 ranges) cleared")) return fail("a preferred-base pointer was not cleared or not measured");
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x123456789000ull, 0x140);   /* outside the image: cleared, sizes unknown */
        outn = 0; out[0] = 0;
        CALL(nt, 0x140000000ull);
        if (vmp() || !strstr(out, "(0 accesses, 0 ranges) cleared")) return fail("a pointer outside the image was not cleared safely");
        printf("PASS: on, a pointer at the preferred base is measured there; one outside the image is cleared without reading it\n");
        return 0;
    }
    if (!strcmp(s, "skip")) {
        nt = mk(IMAGE_FILE_MACHINE_ARM64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x140);
        CALL(nt, 0x140000000ull);
        if (vmp() != 0x7f0000000ull + MD) return fail("an ARM64 image was changed");
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x100);   /* too short to hold the field */
        CALL(nt, 0x140000000ull);
        if (vmp() != 0x7f0000000ull + MD) return fail("a load config without the field was changed");
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x140);
        nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG].VirtualAddress = SZ - 8;   /* runs past the image */
        CALL(nt, 0x140000000ull);
        if (vmp() != 0x7f0000000ull + MD) return fail("a load config past the image was touched");
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0, 0x140);
        CALL(nt, 0x140000000ull);
        nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x140);
        nt->OptionalHeader.NumberOfRvaAndSizes = IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG;
        CALL(nt, 0x140000000ull);
        if (vmp() != 0x7f0000000ull + MD || outn) return fail("an image without a load config entry was changed or logged");
        printf("PASS: on, ARM64 images, short or out-of-image load configs and images without metadata are left alone, silently\n");
        return 0;
    }
    if (!strcmp(s, "cap")) {
        for (i = 0; i < 40; i++) { nt = mk(IMAGE_FILE_MACHINE_AMD64, 0x7f0000000ull, 0x7f0000000ull + MD, 0x140); CALL(nt, 0x140000000ull); if (vmp()) return fail("not cleared"); }
        { int n = 0; const char *p = out; while ((p = strstr(p, "[fex-vmeta]"))) { n++; p++; } if (n != 32) return fail("the line is not capped at 32"); }
        printf("PASS: on, every image is cleared and the line stops after 32\n");
        return 0;
    }
    return 2;
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-nixxes-') as directory:
    c = Path(directory) / 'v.c'
    exe = Path(directory) / 'v'
    c.write_text(harness)
    build = subprocess.run([cc, '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-function', '-DWINE_UNIX_LIB',
                            '-I' + str(root / 'wine/include'), '-fsanitize=address,undefined', '-o', str(exe), str(c)],
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    for scenario in ('off', 'relocated', 'preferred', 'skip', 'cap'):
        run = subprocess.run([str(exe), scenario], capture_output=True, text=True)
        print(run.stdout, end='')
        check('ignore-volatile-metadata scenario ' + scenario, run.returncode == 0)

mapper = function(vsrc, 'static NTSTATUS map_image_into_view( struct file_view *view, const UNICODE_STRING *nt_name, int fd,')
reloc = mapper.index('rel = process_relocation_block( ptr + rel->VirtualAddress, rel, delta );')
call = mapper.index('ios_ignore_volatile_metadata( ptr, total_size, nt, (ULONG_PTR)image_info->base, nt_name );')
prot = mapper.index('/* set the image protections */', reloc)   # the flat-mapped path above has its own
copy = mapper.index('Eagerly JIT-copy all EXEC sections right here')
check('the strip runs after relocation and before the protections and the JIT-pool copy', reloc < call < prot < copy)
check('ignore-volatile-metadata is a madeira.cfg switch, off by default',
      'enabled = madeira_cfg_bool( "ignore-volatile-metadata", 0 );' in strip)
check("Wine's IMAGE_LOAD_CONFIG_DIRECTORY64 has VolatileMetadataPointer at 0x100",
      re.search(r'ULONGLONG VolatileMetadataPointer;\s+/\* 100 \*/', (root / 'wine/include/winnt.h').read_text()) is not None)

# ---- capability switches ----
check('d3d12-wave-ops = 0 reports WaveOps FALSE; anything else keeps TRUE',
      'v = mad_cfg_int_pe("d3d12-wave-ops", 1) ? 1 : 0;' in dsrc and 'o->WaveOps = mad_wave_ops_answer();' in dsrc
      and 'o->WaveOps = TRUE;' not in dsrc)
check('d3d12-binding-tier = 3 reports tier 3; anything else keeps tier 2',
      'v = mad_cfg_int_pe("d3d12-binding-tier", 2) == 3 ? 3 : 2;' in dsrc
      and 'return v == 3 ? D3D12_RESOURCE_BINDING_TIER_3 : D3D12_RESOURCE_BINDING_TIER_2;' in dsrc
      and 'o->ResourceBindingTier = mad_binding_tier_answer();' in dsrc
      and 'o->ResourceBindingTier = D3D12_RESOURCE_BINDING_TIER_2;' not in dsrc)

# ---- raw-typed-views ----
sib = function(dsrc, 'static int mad_raw_typed_sibling(struct mad_device *d, struct mad_resource *r, UINT64 first, UINT64 num,')
check('d3d12-raw-typed-views is off by default', 'g_raw_typed = mad_cfg_int_pe("d3d12-raw-typed-views", 0) ? 1 : 0;' in dsrc)
fmt = r'''
#include <stdio.h>
typedef unsigned long long UINT64;
enum { R32 = 42, RG32 = 18, RGBA32 = 3 };
static int pick(UINT64 stride, int uav, UINT64 first, UINT64 num, UINT64 *f_first, UINT64 *f_num)
{
    int fmt = R32; UINT64 f = 4;
    if (!stride || (stride & 3) || !num) return 0;
    if (!uav && !(stride & 15)) { fmt = RGBA32; f = 16; }
    else if (!uav && !(stride & 7)) { fmt = RG32; f = 8; }
    *f_first = first * stride / f; *f_num = num * stride / f;
    return fmt;
}
int main(void)
{
    UINT64 a, b; int bad = 0;
    bad |= pick(4, 0, 10, 100, &a, &b) != R32 || a != 10 || b != 100;          /* raw SRV */
    bad |= pick(16, 0, 3, 5, &a, &b) != RGBA32 || a != 3 || b != 5;           /* structured float4 */
    bad |= pick(32, 0, 3, 5, &a, &b) != RGBA32 || a != 6 || b != 10;
    bad |= pick(8, 0, 3, 5, &a, &b) != RG32 || a != 3 || b != 5;
    bad |= pick(24, 0, 1, 2, &a, &b) != RG32 || a != 3 || b != 6;
    bad |= pick(12, 0, 2, 4, &a, &b) != R32 || a != 6 || b != 12;             /* no 96-bit texel in Metal */
    bad |= pick(20, 0, 1, 1, &a, &b) != R32 || a != 5 || b != 5;
    bad |= pick(16, 1, 1, 1, &a, &b) != R32 || a != 4 || b != 4;              /* every UAV: R32 */
    bad |= pick(6, 0, 1, 1, &a, &b) != 0 || pick(0, 0, 1, 1, &a, &b) != 0 || pick(4, 0, 1, 0, &a, &b) != 0;
    printf(bad ? "FAIL\n" : "ok\n");
    return bad;
}
'''
check('the sibling format follows the stride as in the source (16 -> RGBA32, 8 -> RG32, else R32; UAVs R32)',
      'if (!stride || (stride & 3) || !num) return 0;' in sib
      and 'if (!uav && !(stride & 15)) { fmt = DXGI_FORMAT_R32G32B32A32_UINT; f = 16; }' in sib
      and 'else if (!uav && !(stride & 7)) { fmt = DXGI_FORMAT_R32G32_UINT; f = 8; }' in sib
      and 'mad_typed_buffer_view(d, r, fmt, first * stride / f, num * stride / f, uav, e, &why)' in sib)
with tempfile.TemporaryDirectory(prefix='madeira-rawtyped-') as directory:
    c = Path(directory) / 'f.c'
    exe = Path(directory) / 'f'
    c.write_text(fmt)
    build = subprocess.run([cc, '-std=gnu11', '-Wall', '-Werror', '-o', str(exe), str(c)], capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    check('sibling formats and element ranges for raw, structured and UAV strides', run.returncode == 0)
srv = function(dsrc, 'static void STDMETHODCALLTYPE device_CreateShaderResourceView(ID3D12Device *This,')
uav = function(dsrc, 'static void STDMETHODCALLTYPE device_CreateUnorderedAccessView(ID3D12Device *This,')
check('an SRV gets a sibling only for raw or structured buffer views, before the plain descriptor',
      srv.index('(desc->Buffer.StructureByteStride || (desc->Buffer.Flags & D3D12_BUFFER_SRV_FLAG_RAW)) && mad_raw_typed_on() &&')
      < srv.index('mad_set_buffer_descriptor(e, r->gpu_address + first * stride, num * stride);'))
check('a UAV with a counter keeps its counter view; the counter bookkeeping still runs after a sibling',
      'if (!counter && desc && desc->ViewDimension == D3D12_UAV_DIMENSION_BUFFER &&' in uav
      and uav.index('mad_raw_typed_sibling((struct mad_device *)This, r, first, num, stride, 1, e)) ;')
      < uav.index('else mad_set_buffer_descriptor(e, r->gpu_address + first * stride, num * stride);')
      < uav.index('/* remember this view\'s counter (or forget a stale one) */'))

# ---- skin-check index capture ----
draw = function(dsrc, 'static void mad_skin_draw(struct mad_exec *e, const struct mad_cmd *c) {')
check('skin-check captures an indexed draw\'s indices within the per-frame capture budget',
      'if (g_skin_cap_on && c->kind == MC_DRAW_INDEXED && e->ib && e->ib->buffer && c->u.drawi.icount &&' in draw
      and 'InterlockedIncrement(&g_skin_caps_frame) <= 96) {' in draw
      and 'mad_skin_capture(e, lab, e->ib, off, (UINT)len, lead ? 23 : 22, isz | (tail ? 16u : 0u));' in draw)
pr = function(dsrc, 'static void mad_skin_print(const struct mad_skcap *c) {')
blk = pr[pr.index('} else if (c->kind == 22 || c->kind == 23) {'):pr.index('} else {   /* 21: the start of a shader input */')]
body = blk[blk.index('{') + 1:]
ib = r'''
#include <stdio.h>
#include <string.h>
typedef unsigned int UINT; typedef unsigned int UINT32; typedef unsigned short UINT16;
struct cap { UINT len, kind, stride; const char *label; };
static char out[4096];
#define d3d12_log(...) snprintf(out, sizeof out, __VA_ARGS__)
static void pr(const struct cap *c, const unsigned char *p, UINT32 hash) {
    UINT i;
    {''' + body + r'''
    }
}
int main(void)
{
    unsigned short a[] = { 0xdead, 0, 1, 2, 2, 3, 3, 7, 5, 0xbeef };
    struct cap c = { sizeof a, 23, 2 | 16, "L" };   /* 10 u16: a lead and a trailing u16 that are not the draw's, 8 indices */
    pr(&c, (const unsigned char *)a, 0x1234);
    if (!strstr(out, "L: 8 indices captured, min 0 max 7, 1 of 2 triangles degenerate, hash 00001234: 0 1 2 2 3 3")) { printf("%s\nFAIL\n", out); return 1; }
    { unsigned b[] = { 4, 5, 6, 9, 9, 1 }; struct cap d = { sizeof b, 22, 4, "M" };
      pr(&d, (const unsigned char *)b, 1);
      if (!strstr(out, "M: 6 indices captured, min 1 max 9, 1 of 2 triangles degenerate")) { printf("%s\nFAIL\n", out); return 1; } }
    { unsigned short e2[] = { 0x7777, 0 }; struct cap z = { 4, 23, 2 | 16, "Z" };
      pr(&z, (const unsigned char *)e2, 0);
      if (!strstr(out, "Z: 0 indices captured, min 0 max 0, 0 of 0 triangles degenerate")) { printf("%s\nFAIL\n", out); return 1; } }
    printf("ok\n");
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-skin-ib-') as directory:
    c = Path(directory) / 'i.c'
    exe = Path(directory) / 'i'
    c.write_text(ib)
    build = subprocess.run([cc, '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-variable', '-fsanitize=address,undefined',
                            '-o', str(exe), str(c)], capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    print(run.stdout, end='')
    check('the index summary skips the u16s that are not the draw\'s and counts degenerate triangles', run.returncode == 0)

if fails:
    raise SystemExit('FAILED: %d check(s)' % len(fails))
print('ALL PASS')
