#!/usr/bin/env python3
"""typed-view-shadow, vis-trace and readback-far (build 487, Horizon Zero Dawn).

Build 486's probe showed the GPU reads a texture buffer that starts off a
16-byte boundary from the start rounded down, and the shader converter ignores
a typed buffer's element offset. typed-view-shadow gives a typed SRV of UPLOAD
memory that starts off a 16-byte boundary an aligned copy of its bytes,
refreshed at every ExecuteCommandLists. vis-trace logs every present's draws and
the depth pyramid the game culls with as the GPU wrote it; readback-far fills
that readback with 4096 instead (the game then culls nothing).

Checks: all three off by default and every hook guarded; the copy is made,
refreshed, forgotten with its resource and refused past its limits; the
readback is recognised, filled, recorded and logged once the GPU is done; the
settings catalog has the keys; Horizon Zero Dawn's list turns on the shadow and
the trace (not the experiment). The helpers are compiled and run against stubs.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
pe = (root / 'madeira-d3d12/src/pe/madeira_d3d12.c').read_text()
catalog = (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()
recs = (root / 'app/Madeira/GameRecommendations.swift').read_text()
ok = True


def check(what, cond):
    global ok
    print(('ok   ' if cond else 'FAIL ') + what)
    ok &= bool(cond)


def body(sig):
    """A function's text, braces matched outside strings, characters and comments."""
    start = pe.index(sig)
    while pe.index(';', start) < pe.index('{', start):
        start = pe.index(sig, start + 1)
    i, depth, quote = pe.index('{', start), 0, None
    while i < len(pe):
        ch = pe[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif pe.startswith('/*', i):
            i = pe.index('*/', i) + 2
            continue
        elif pe.startswith('//', i):
            i = pe.index('\n', i)
            continue
        elif ch in '"\'':
            quote = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return pe[start:i + 1]
        i += 1
    raise ValueError(sig)


def block(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


# defaults and hooks
check('typed-view-shadow, vis-trace and readback-far are off by default',
      'mad_cfg_int_pe("typed-view-shadow", 0)' in pe and 'mad_cfg_int_pe("vis-trace", 0)' in pe and
      'mad_cfg_int_pe("readback-far", 0)' in pe)
tbv = body('static int mad_typed_buffer_view(struct mad_device *d, struct mad_resource *r, DXGI_FORMAT fmt,')
check('a view is copied only with an element offset, the switch on, an SRV of UPLOAD memory; others are counted',
      'if (elem_off && mad_tvs_on()) {' in tbv and
      'if (!uav && r->cpu && r->heap == D3D12_HEAP_TYPE_UPLOAD) tex = mad_tvs_view(d, r, &ti, byte_off, num, bytes, &sh_gpu);' in tbv and
      'if (!tex) InterlockedIncrement(&g_tvs_other);' in tbv and
      'if (tex) elem_off = 0;\n        else tex = MTLBuffer_newTexture(r->buffer, &ti, aligned, bpr);' in tbv)
check('the descriptor names the copy, and the view remembers it',
      'r->tview[k].eoff = (UINT8)elem_off; r->tview[k].sh_gpu = sh_gpu;' in tbv and
      'e->gpu_va = r->tview[k].sh_gpu ? r->tview[k].sh_gpu : r->gpu_address + r->tview[k].off;' in tbv)
ecl = block(pe, 'static void STDMETHODCALLTYPE queue_ExecuteCommandLists(', '\n}\n')
check('every ExecuteCommandLists refreshes the copies (before a worker or the replay sees the lists) and loads vis-trace',
      ecl.index('mad_tvs_refresh();') < ecl.index('if (q && q->sub_thread)') and 'if (g_vt_on < 0) mad_vt_load();' in ecl)
rel = block(pe, 'static ULONG STDMETHODCALLTYPE res_Release(', '\n}\n')
check('a released resource stops being refreshed before its memory goes',
      'if (r->cpu) mad_tvs_forget(r);' in rel and rel.index('mad_tvs_forget(r)') < rel.index('if (r->own_mem)'))
check('view-census reports the copies', '[view-census] typed-view-shadow: %ld views read an aligned copy' in pe)
check('vis-trace hooks: draws, dispatches and copies (by queue type) counted only when on',
      'case MC_DRAW: case MC_DRAW_INDEXED: exec_draw(&e, c); if (g_vt_on > 0) mad_vt_draw(&e, c); break;' in pe and
      'case MC_DISPATCH: exec_dispatch(&e, c); if (g_vt_on > 0) { InterlockedIncrement(&g_vt_disp); InterlockedIncrement(&g_vt_disp_q[q->type & 3]); } break;' in pe and
      'if (g_vt_on > 0) InterlockedIncrement(&g_vt_copy_q[q->type & 3]);' in pe)
check('readbacks: looked at only with vis-trace or readback-far on; a filled one encodes no copy',
      'if ((g_vt_on > 0 || g_rbf_on > 0) && mad_vt_copy(e, c)) return;   /* madeira-bcd vis-trace / readback-far */' in pe)
check('Queue::Wait: calls, fast-path passes and passes before the awaited batch was committed are counted (vis-trace on)',
      'if ((UINT64)f->committed < value && f->value < value) InterlockedIncrement(&g_vt_qw_early);' in pe and
      pe.index('InterlockedIncrement(&g_vt_qw_early)') < pe.index('/* madeira-bcd: fence-strict. "submitted" is set by queue_Signal'))
check('a line per present, after the present is counted',
      's->presents++;\n    mad_perf_present();   /* ml1108 */\n    if (g_vt_on > 0) mad_vt_present(s->queue->device, s->presents);' in pe)
for key in ('typed-view-shadow', 'vis-trace', 'readback-far'):
    check('settings catalog: ' + key, 'ConfigOption(key: "%s"' % key in catalog)
hzd = block(recs, 'static let horizonZeroDawn = GameRecommendation(', 'avx: false')
check('Horizon Zero Dawn (v11): shadow, fence-strict and vis-trace on, readback-far not',
      'version: 11' in hzd and 'typed-view-shadow = 1' in hzd and 'fence-strict = 1' in hzd and 'vis-trace = 1' in hzd and
      'readback-far' not in hzd)
configs = [c.split('"""')[0] for c in recs.split('config: """')[1:]]
others = [c for c in configs if 'typed-view-shadow = 1' not in c]
check('no other game list has them', len(configs) == 6 and len(others) == 5 and
      not any(k in c for c in others for k in ('typed-view-shadow', 'vis-trace', 'readback-far', 'fence-strict')))

harness = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
typedef uint32_t UINT32; typedef unsigned UINT; typedef long LONG; typedef uint64_t UINT64; typedef int64_t LONG64;
typedef unsigned long long obj_handle_t;
typedef struct { int x; } SRWLOCK;
#define SRWLOCK_INIT { 0 }
static void AcquireSRWLockExclusive(SRWLOCK *l) { (void)l; }
static void ReleaseSRWLockExclusive(SRWLOCK *l) { (void)l; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++*p; }
static LONG InterlockedExchange(volatile LONG *p, LONG v) { LONG o = *p; *p = v; return o; }
static LONG InterlockedExchangeAdd(volatile LONG *p, LONG v) { LONG o = *p; *p += v; return o; }
static LONG64 InterlockedExchange64(volatile LONG64 *p, LONG64 v) { LONG64 o = *p; *p = v; return o; }
static LONG64 InterlockedExchangeAdd64(volatile LONG64 *p, LONG64 v) { LONG64 o = *p; *p += v; return o; }
static void MemoryBarrier(void) { }
static char logbuf[32768]; static size_t loglen;
static void d3d12_log(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    loglen += (size_t)vsnprintf(logbuf + loglen, sizeof logbuf - loglen, fmt, ap);
    va_end(ap);
}
static long long cfg_vt, cfg_rbf;
static long long mad_cfg_int_pe(const char *k, long long d) { return !strcmp(k, "vis-trace") ? cfg_vt : !strcmp(k, "readback-far") ? cfg_rbf : d; }
enum WMTPixelFormat { WMTPixelFormatR32Float = 55, WMTPixelFormatR32Uint = 53 };
enum { WMTResourceStorageModeShared = 0 };
struct WMTTextureInfo { uint32_t width; UINT64 gpu_resource_id; };
struct WMTBufferInfo { UINT64 length; int options; struct { void *ptr; } memory; UINT64 gpu_address; };
struct mad_device { void *mtl_device; };
struct mad_resource { void *cpu; UINT64 size; enum WMTPixelFormat tex_pf; UINT tex_mips; };
static int chunks, residents, refuse_tex; static UINT64 next_id = 1, last_off;
static obj_handle_t MTLDevice_newBuffer(void *dev, struct WMTBufferInfo *bi) {
    (void)dev; bi->memory.ptr = calloc(1, (size_t)bi->length); bi->gpu_address = 0x100000000ull * (UINT64)++chunks; return (obj_handle_t)chunks;
}
static void mad_resident(struct mad_device *d, obj_handle_t b) { (void)d; (void)b; residents++; }
static void NSObject_release(obj_handle_t h) { (void)h; }
static obj_handle_t MTLBuffer_newTexture(obj_handle_t b, struct WMTTextureInfo *ti, UINT64 off, UINT64 bpr) {
    (void)b; (void)bpr; if (refuse_tex) return 0; last_off = off; ti->gpu_resource_id = next_id++; return (obj_handle_t)ti->gpu_resource_id;
}
static int mad_grow(void **arr, unsigned *cap, unsigned need, size_t elem) {
    if (need > *cap) { unsigned n = need * 2; void *p = realloc(*arr, n * elem); if (!p) return 0; *arr = p; *cap = n; }
    return 1;
}
#define MAD_TVS_CHUNK (4u << 20)
#define MAD_TVS_MAX_VIEW (64u << 10)
#define MAD_TVS_MAX_BYTES (64u << 20)
#define MAD_TVS_MAX_N 16384u
struct mad_tvs { struct mad_resource *r; const unsigned char *src; unsigned char *dst; UINT32 len; };
static struct mad_tvs *g_tvs; static unsigned g_tvs_n, g_tvs_cap;
static SRWLOCK g_tvs_lock = SRWLOCK_INIT;
static obj_handle_t g_tvs_buf; static unsigned char *g_tvs_cpu; static UINT64 g_tvs_gpu; static UINT32 g_tvs_used;
static volatile LONG64 g_tvs_bytes;
static volatile LONG g_tvs_views, g_tvs_copies, g_tvs_other, g_tvs_full;
static volatile LONG g_tv_eoff_views, g_sd_strict_waits;
''' + body('static obj_handle_t mad_tvs_view(struct mad_device *d, struct mad_resource *r, struct WMTTextureInfo *ti,') + '\n' + \
    body('static void mad_tvs_refresh(void) {') + '\n' + body('static void mad_tvs_forget(struct mad_resource *r) {') + r'''
/* vis-trace */
enum { MC_DRAW = 1, MC_DRAW_INDEXED = 2 };
struct mad_queue { UINT64 batches, last_serial, batch_serial[64]; UINT type; };
struct mad_cmd { int kind; union { struct { struct mad_resource *tex, *buf; UINT level, slice, plane, w, h, d, row, rows; UINT64 off; } bt;
                                    struct { UINT icount, inst; } drawi; struct { UINT vcount, icount; } draw; } u; };
struct mad_exec { struct mad_queue *q; obj_handle_t renc; UINT enc_nrt; struct mad_resource *enc_depth; };
static UINT64 gpu_done;
static UINT64 mad_gpu_completed(struct mad_device *d) { (void)d; return gpu_done; }
''' + block(pe, 'static int g_vt_on = -1, g_rbf_on = -1;', 'static void mad_vt_load(void) {') + \
    body('static void mad_vt_load(void) {') + '\n' + body('static void mad_vt_draw(const struct mad_exec *e, const struct mad_cmd *c) {') + '\n' + \
    body('static int mad_vt_copy(struct mad_exec *e, const struct mad_cmd *c) {') + '\n' + \
    body('static void mad_vt_present(struct mad_device *d, UINT64 presents) {') + r'''
static int bad;
#define EXPECT(c, what) do { if (!(c)) { printf("FAIL %s\n", what); bad = 1; } else printf("ok   %s\n", what); } while (0)
int main(void) {
    struct mad_device d = { 0 };
    static unsigned char upmem[1 << 20];
    struct mad_resource up = { upmem, sizeof upmem, 0, 0 };
    struct WMTTextureInfo ti;
    UINT64 gpu = 0, gpu2 = 0;
    unsigned i;
    for (i = 0; i < sizeof upmem; i++) upmem[i] = (unsigned char)(i * 7);
    /* typed-view-shadow */
    ti.width = 308; ti.gpu_resource_id = 0;
    EXPECT(mad_tvs_view(&d, &up, &ti, 4, 307, 4, &gpu) != 0 && ti.width == 307 && gpu == 0x100000000ull && last_off == 0 &&
           !memcmp(g_tvs_cpu, upmem + 4, 1228) && g_tvs_n == 1 && chunks == 1 && residents == 1 && g_tvs_views == 1,
           "a 307-element R32 view at +4: its bytes copied to the start of a new resident chunk, the texture 307 wide");
    ti.width = 9; ti.gpu_resource_id = 0;
    EXPECT(mad_tvs_view(&d, &up, &ti, 2000, 8, 4, &gpu2) != 0 && gpu2 == 0x100000000ull + 1232 && last_off == 1232 && g_tvs_n == 2,
           "the next copy starts at the next 16-byte boundary (1228 -> 1232)");
    g_tvs_copies = 0; mad_tvs_refresh();
    EXPECT(g_tvs_copies == 0, "refresh: nothing changed, nothing copied");
    upmem[4 + 100] ^= 0xff; mad_tvs_refresh();
    EXPECT(g_tvs_copies == 1 && g_tvs_cpu[100] == upmem[104], "refresh: the CPU rewrote the range, the copy follows");
    {   /* forgotten with its resource */
        static unsigned char other[256]; struct mad_resource o = { other, sizeof other, 0, 0 };
        UINT64 g3 = 0; unsigned char *dst;
        ti.width = 9; EXPECT(mad_tvs_view(&d, &o, &ti, 4, 8, 4, &g3) != 0 && g_tvs_n == 3, "a third view, of another resource");
        dst = g_tvs[2].dst; mad_tvs_forget(&o); other[4] ^= 0xff; mad_tvs_refresh();
        EXPECT(g_tvs_n == 2 && dst[0] != other[4], "forget: the released resource's copy is no longer refreshed, the others are");
    }
    ti.width = 77; loglen = 0;
    EXPECT(mad_tvs_view(&d, &up, &ti, 4, (64u << 10) / 4 + 1, 4, &gpu) == 0 && ti.width == 77 && g_tvs_full == 1,
           "a view over 64 KB is refused, the texture's width left as it was");
    ti.width = 5;
    EXPECT(mad_tvs_view(&d, &up, &ti, sizeof upmem - 8, 307, 4, &gpu) != 0 && ti.width == 2,
           "a view past the end of its resource copies what is there");
    refuse_tex = 1; ti.width = 33; { UINT32 used = g_tvs_used; unsigned n = g_tvs_n;
    EXPECT(mad_tvs_view(&d, &up, &ti, 4, 8, 4, &gpu) == 0 && ti.width == 33 && g_tvs_used == used && g_tvs_n == n,
           "Metal refuses the texture: nothing kept, the caller makes the view as before"); }
    refuse_tex = 0;
    g_tvs_used = MAD_TVS_CHUNK - 8; ti.width = 4;
    EXPECT(mad_tvs_view(&d, &up, &ti, 4, 4, 4, &gpu) != 0 && chunks == 2 && gpu == 0x200000000ull && last_off == 0,
           "a full chunk: the copy goes to a new one");

    /* vis-trace and readback-far */
    {
        static unsigned char rb[8192]; static float tex_dummy;
        struct mad_resource pyr = { &tex_dummy, 0, WMTPixelFormatR32Float, 6 }, dst = { rb, sizeof rb, 0, 1 };
        struct mad_queue q = { 0 };
        struct mad_exec e = { &q, 0, 0, NULL };
        struct mad_cmd c; float *f; int k;
        (void)tex_dummy;
        cfg_rbf = 1; cfg_vt = 1; mad_vt_load();
        memset(&c, 0, sizeof c);
        c.u.bt.tex = &pyr; c.u.bt.buf = &dst; c.u.bt.level = 5; c.u.bt.w = 5; c.u.bt.h = 2; c.u.bt.d = 1; c.u.bt.row = 256; c.u.bt.off = 512;
        memset(rb, 0xab, sizeof rb);
        EXPECT(mad_vt_copy(&e, &c) == 1, "readback-far: a 5x2 mip-5 readback of an R32_FLOAT 6-mip texture is handled (no GPU copy)");
        f = (float *)(rb + 512);
        EXPECT(f[0] == 4096.0f && f[4] == 4096.0f && ((float *)(rb + 768))[4] == 4096.0f && rb[512 + 20] == 0xab && rb[511] == 0xab &&
               rb[768 + 20] == 0xab && g_vt_rbf == 1, "readback-far: both rows filled with 4096, the row padding and the bytes around untouched");
        c.u.bt.level = 0; memset(rb, 0xab, sizeof rb);
        EXPECT(mad_vt_copy(&e, &c) == 0 && rb[512] == 0xab, "level 0 is not a pyramid readback: copied as before");
        c.u.bt.level = 5; pyr.tex_pf = WMTPixelFormatR32Uint;
        EXPECT(mad_vt_copy(&e, &c) == 0 && rb[512] == 0xab, "another format: copied as before");
        pyr.tex_pf = WMTPixelFormatR32Float; pyr.tex_mips = 1;
        EXPECT(mad_vt_copy(&e, &c) == 0, "a texture without mips: copied as before");
        pyr.tex_mips = 6; c.u.bt.off = sizeof rb - 260;
        EXPECT(mad_vt_copy(&e, &c) == 0, "a destination too small for the rows: left to the GPU copy");
        /* vis-trace without the experiment */
        g_rbf_on = 0; c.u.bt.off = 1024; q.batches = 7; q.type = 3;
        for (k = 0; k < 10; k++) ((float *)(rb + 1024 + (k / 5) * 256))[k % 5] = (float)(k + 1);
        EXPECT(mad_vt_copy(&e, &c) == 0 && g_vt_rb_n == 1 && g_vt_rb[0].batch == 8 && g_vt_rb[0].frame == 1,
               "vis-trace: the readback is copied by the GPU and recorded with its open batch and the present it belongs to");
        loglen = 0; logbuf[0] = 0;
        mad_vt_present(&d, 1);
        EXPECT(strstr(logbuf, "[vis-trace] present #1: draws 0") != NULL && strstr(logbuf, "pyramid mip") == NULL && g_vt_rb_n == 1,
               "present 1: the batch is still open, nothing printed yet");
        q.batches = 8; q.batch_serial[8] = 42; gpu_done = 41; loglen = 0; logbuf[0] = 0;
        mad_vt_present(&d, 2);
        EXPECT(strstr(logbuf, "pyramid mip") == NULL && g_vt_rb_n == 1, "present 2: committed but not finished on the GPU, still waiting");
        gpu_done = 42; loglen = 0; logbuf[0] = 0;
        mad_vt_present(&d, 3);
        EXPECT(strstr(logbuf, "[vis-trace] pyramid mip 5 (5x2) of present #1, queue type 3: 1 2 3 4 5 | 6 7 8 9 10") != NULL && g_vt_rb_n == 0,
               "present 3: the GPU is done, the values are printed with the present they belong to, row by row");
        q.batches = 8; c.u.bt.w = 11; c.u.bt.h = 5; c.u.bt.level = 4;
        for (k = 0; k < 55; k++) ((float *)(rb + 1024 + (k / 11) * 256))[k % 11] = (float)(100 + k);
        mad_vt_copy(&e, &c);
        for (k = 4; k < 13; k++) mad_vt_present(&d, (UINT64)k);
        EXPECT(strstr(logbuf, "pyramid mip 4 (11x5) of present #4, queue type 3 (GPU NOT DONE after 8 presents): 100 101 102 103 104 105 "
                              "106 107 108 109 110 | 111 112") != NULL && strstr(logbuf, "| 144 145 146 147 148 149 150 151 152 153 154\n") != NULL,
               "an 11x5 mip: every value, rows split; a batch never finished is printed after 8 presents, flagged");
        c.u.bt.w = 22; c.u.bt.h = 10; c.u.bt.level = 3; g_vt_rb_n = 0;
        EXPECT(mad_vt_copy(&e, &c) == 0 && g_vt_rb_n == 1 && g_vt_rb[0].w == 22, "the 22x10 mip is recorded too (the mips can be checked against each other)");
        c.u.bt.w = 44; c.u.bt.h = 20; c.u.bt.level = 2; g_vt_rb_n = 0;
        EXPECT(mad_vt_copy(&e, &c) == 0 && g_vt_rb_n == 0, "the 44x20 mip is counted, not recorded");
        g_vt_rb_n = 0;
        {
            struct mad_cmd dc; memset(&dc, 0, sizeof dc);
            struct mad_resource depth;
            dc.kind = MC_DRAW_INDEXED; dc.u.drawi.icount = 300; dc.u.drawi.inst = 4;
            e.renc = 1; e.enc_nrt = 0; e.enc_depth = &depth; mad_vt_draw(&e, &dc);
            e.enc_nrt = 5; e.enc_depth = &depth; mad_vt_draw(&e, &dc);
            dc.kind = MC_DRAW; dc.u.draw.vcount = 6; dc.u.draw.icount = 0; e.enc_nrt = 1; mad_vt_draw(&e, &dc);
            g_tv_eoff_views += 2; g_tvs_views += 1; g_vt_qw = 5; g_vt_qw_fast = 4; g_vt_qw_early = 1; g_sd_strict_waits += 3;
            g_vt_disp_q[0] = 7; g_vt_disp_q[2] = 9; g_vt_copy_q[0] = 1; g_vt_copy_q[2] = 2; g_vt_copy_q[3] = 3;
            loglen = 0; logbuf[0] = 0;
            mad_vt_present(&d, 20);
            EXPECT(strstr(logbuf, "[vis-trace] present #20: draws 3 (depth-only 1, 3+ targets 1), indices 0.00M, dispatches 0, pyramid copies 2, "
                                  "offset views +2, shadowed +1; Queue::Wait 5 (fast path 4, before the awaited batch was committed 1; "
                                  "fence-strict waits +3); dispatches by queue: direct 7, compute 9; copies by queue: direct 1, compute 2, copy 3")
                   != NULL, "the per-present counts, reset after each line");
            if (bad) printf("%s", logbuf);
            loglen = 0; logbuf[0] = 0; mad_vt_present(&d, 21);
            EXPECT(strstr(logbuf, "present #21: draws 0 (depth-only 0, 3+ targets 0), indices 0.00M, dispatches 0, pyramid copies 0, offset views +0, "
                                  "shadowed +0; Queue::Wait 0 (fast path 0, before the awaited batch was committed 0; fence-strict waits +0); "
                                  "dispatches by queue: direct 0, compute 0; copies by queue: direct 0, compute 0, copy 0") != NULL,
                   "and the next present starts from zero");
        }
    }
    printf("%s\n", bad ? "harness FAILED" : "harness ok");
    return bad;
}
'''

cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
if not cc:
    check('a host C compiler', False)
else:
    with tempfile.TemporaryDirectory(prefix='madeira-vt-') as tmp:
        src = Path(tmp) / 'vt.c'
        exe = Path(tmp) / 'vt'
        src.write_text(harness)
        b = subprocess.run([cc, '-std=gnu11', '-Wall', '-Wno-unused-function', '-Wno-unused-variable', str(src), '-o', str(exe)],
                           capture_output=True, text=True)
        check('the helpers compile on the host', b.returncode == 0)
        if b.returncode:
            print(b.stderr[-3000:])
        else:
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            print(run.stdout, end='')
            check('the helpers behave', run.returncode == 0)

print('PASS: typed-view-shadow, vis-trace, readback-far' if ok else 'FAIL: typed-view-shadow, vis-trace, readback-far')
raise SystemExit(0 if ok else 1)
