#!/usr/bin/env python3
"""skin-check and view-census: opt-in diagnostics for compute-skinned meshes.

Horizon Zero Dawn (builds 480 and 481) skins characters in compute and draws
them from buffers created with ALLOW_UNORDERED_ACCESS; every few frames a face,
hair or a whole body comes out collapsed to the origin, and gpu-sync = 1 did not
change it. skin-check logs, in windows of check frames, the draws that read such
buffers, the dispatches that wrote them (every DXBC range and what it resolved
to), 32 KB GPU-ordered copies of those streams summarised after the queue's
fence wait, and the shaders' bytecode. view-census names typed-buffer views
that fall back to plain descriptors or are cut short, and views past their
resource, without changing how any view is made.

Checks: both off by default and every hook guarded by them; captures force the
synchronous Signal path and are printed only by the queue that recorded them;
the typed-view reason codes leave the old results untouched; settings catalog
entries; Horizon Zero Dawn's list turns both on. The line helper, the capture
summary, the per-queue flush and the typed-view reasons are compiled and run
against stubs.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
    start = pe.index(sig)
    while pe.index(';', start) < pe.index('{', start):
        start = pe.index(sig, start + 1)
    brace = pe.index('{', start)
    depth = 0
    for i in range(brace, len(pe)):
        if pe[i] == '{':
            depth += 1
        elif pe[i] == '}':
            depth -= 1
            if depth == 0:
                return pe[start:i + 1]
    raise AssertionError('unterminated: ' + sig)


check('skin-check off by default', 'mad_cfg_int_pe("skin-check", 0)' in pe)
check('skin-check-every defaults to 300 presents, at least 30', 'mad_cfg_int_pe("skin-check-every", 300)' in pe and
      'g_skin_every = ev < 30 ? 30 : ev;' in pe)
check('skin-check-capture defaults to on (copies), 0 logs only', 'mad_cfg_int_pe("skin-check-capture", 1)' in pe)
check('view-census off by default', 'mad_cfg_int_pe("view-census", 0)' in pe)

draw = body('static void exec_draw(struct mad_exec *e, const struct mad_cmd *c)')
check('draw: the hook runs only with the switch on, before the render pass begins',
      'if (g_skin_check > 0) mad_skin_draw(e, c);' in draw and
      draw.index('if (g_skin_check > 0) mad_skin_draw(e, c);') < draw.index('if (!exec_begin_render(e)) { MAD_SKIP(e); return; }'))
check('draw: the vertex-stage ranges are logged only for a logged draw, and tab_on is cleared on every path',
      'e->sk->tab_on = e->sk->draw_gpu == 2;' in draw and '{ if (e->sk) e->sk->tab_on = 0; MAD_SKIP(e); return; }' in draw and
      'if (e->sk && e->sk->tab_on) { e->sk->tab_on = 0; mad_skin_vs_line(e); }' in draw)
disp = body('static void exec_dispatch(struct mad_exec *e, const struct mad_cmd *c)')
check('dispatch: tables built with tab_on, cleared whether or not they built',
      disp.count('e->sk->tab_on = e->sk->tab_cs = 0;') == 2 and 'if (e->sk) e->sk->tab_on = e->sk->tab_cs = 1;' in disp)
check('dispatch: logged after it was encoded, last',
      disp.rstrip().endswith('if (e->sk) mad_skin_dispatch(e, c);   /* madeira-bcd skin-check */\n}'))
ex = body('static void mad_exec_list(struct mad_queue *q, struct mad_list *l, obj_handle_t cb)')
check('replay state only in check frames, freed at the end of the list (before ring-share parks its chunks)',
      'if (g_skin_now) mad_skin_begin(&e);' in ex and
      'if (e.sk) mad_skin_end(&e);   /* madeira-bcd skin-check */\n    if (l->nrings && q->device->gpu_event && mad_ring_share_on()) mad_ring_park(q, l);\n}' in ex)
bt = body('static int mad_air_build_tables_ex_body(struct mad_exec *e, const struct mad_rootsig *rs, const UINT64 *root,')
check('bridge: each resolved range is named only while tab_on is set, before it is encoded',
      'if (e->sk && e->sk->tab_on) mad_skin_tok(e, rg, &de, direct);' in bt and
      bt.index('mad_skin_tok(e, rg, &de, direct)') < bt.index('if (rg->type == MADEIRA_IR_AIR_CBV) {'))
sa = body('static int mad_signal_async(struct mad_queue *q, ID3D12Fence *fence, UINT64 value)')
check('pending captures force the synchronous Signal (its fence wait completes them)',
      'if (!d->gpu_event || d->ncap || g_sk_n) return 0;' in sa)
sr = body('static HRESULT mad_signal_run(struct mad_queue *q, ID3D12Fence *fence, UINT64 value)')
check('the signalling queue prints its own captures after the wait',
      sr.index('mad_cb_retire(q->device, q->pending[i])') < sr.index('if (g_sk_n) mad_skin_flush(q);') < sr.index('return ID3D12Fence_Signal(fence, value);'))
cs = body('static HRESULT device_CreateComputePipelineState_impl(ID3D12Device *This,')
check('compute bytecode kept by its fault-report hash, only with the switch',
      'if (mad_skin_on()) mad_bc_keep(hh, b, p->cs_len);' in cs)
gp = body('static HRESULT device_CreateGraphicsPipelineState_impl(ID3D12Device *This,')
check('vertex bytecode hashed and kept only with the switch',
      'if (mad_skin_on()) {   /* madeira-bcd skin-check' in gp and
      'p->vs_hash = mad_fnv64(desc->VS.pShaderBytecode, desc->VS.BytecodeLength);' in gp)
check('Present drives the windows', 'if (mad_skin_on()) mad_skin_present(s->presents);' in pe)
sd = body('static void mad_skin_draw(struct mad_exec *e, const struct mad_cmd *c)')
check('a GPU-written stream is a bound buffer with ALLOW_UNORDERED_ACCESS that the pipeline fetches',
      '((mask >> sl) & 1) && r && r->buffer && (r->desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)' in sd)
check('outside check frames the draw hook only notes the first such draw',
      sd.index('if (!s) return;') < sd.index('s->ndraw++;') and 'InterlockedExchange(&g_skin_seen, 1)' in sd)
check('log budgets per window and in all, reset when a window starts',
      'InterlockedExchange(&g_skin_win_lines, 0); InterlockedExchange(&g_skin_win_caps, 0);' in pe and
      'g_skin_win_lines) <= MAD_SKIN_WIN_LINES && InterlockedIncrement(&g_skin_lines) <= MAD_SKIN_LINE_BUDGET' in pe)
check('per-frame limits: 64 draws, 96 copies of at most 32 KB',
      'if (nd > 64)' in sd and 'InterlockedIncrement(&g_skin_caps_frame) > 96' in sd and 'if (len > 32768) len = 32768;' in sd)
check('the writer of a stream is looked up among this frame\'s dispatches only',
      'w->frame == g_skin_frame && w->r == r && w->off <= e->vb[sl].off' in sd)
cap = body('static int mad_skin_capture(struct mad_exec *e, const char *label, struct mad_resource *r, UINT64 off, UINT len, UINT kind, UINT stride)')
check('copies stay inside the resource and the 4 MB buffer, and remember their queue',
      'if (off + len > r->size) len = (UINT)(r->size - off) & ~3u;' in cap and 'g_sk_used + len <= MAD_SK_BYTES' in cap and
      'c->q = e->q;' in cap and 'g_sk_n < MAD_SK_N' in cap)
tbv = body('static int mad_typed_buffer_view(struct mad_device *d, struct mad_resource *r, DXGI_FORMAT fmt,')
check('typed views: every refusal names its reason, the cut-short view is still made',
      all(('*why = %d' % k) in tbv for k in (1, 2, 3, 4, 5, 7, 8, 9)) and '*why = width ? 6 : 5;' in tbv)
srv = body('static void STDMETHODCALLTYPE device_CreateShaderResourceView(ID3D12Device *This,')
uav = body('static void STDMETHODCALLTYPE device_CreateUnorderedAccessView(ID3D12Device *This,')
for name, f, u in (('SRV', srv, '0'), ('UAV', uav, '1')):
    check(name + ': a typed view still returns only when it was made',
          'ok = mad_typed_buffer_view((struct mad_device *)This, r, desc->Format, first, num, ' + u + ', e, &vwhy);' in f and
          'if (ok) return;' in f)
    check(name + ': plain views past their resource are counted only with view-census',
          'if (g_view_census && (first + num) * stride > r->size && mad_vc_on()) mad_vc_past(r, ' + u + ', first, num, stride);' in f)
check('UAV counter: still bound only when its view was made', 'if (cok) {' in uav)
cbv = body('static void STDMETHODCALLTYPE device_CreateConstantBufferView(ID3D12Device *This,')
check('CBV: checked only with view-census, before the descriptor is written',
      cbv.index('if (g_view_census && mad_vc_on()) mad_vc_cbv(') < cbv.index('mad_set_buffer_descriptor(e, desc->BufferLocation, desc->SizeInBytes);'))

for key, kind, default in (('skin-check', 'int', '0'), ('skin-check-every', 'int', '300'),
                           ('skin-check-capture', 'bool', '1'), ('view-census', 'bool', '0')):
    m = re.search(r'key: "' + re.escape(key) + r'".*?kind: \.(\w+), defaultValue: "([^"]*)"', catalog)
    check('catalog: ' + key + ' (' + kind + ', default ' + default + ')', m is not None and m.group(1) == kind and m.group(2) == default)
hzd = recs[recs.index('static let horizonZeroDawn = GameRecommendation('):]
hzd = hzd[:hzd.index('""",')]
check('Horizon Zero Dawn\'s list turns both on', 'skin-check = 1' in hzd and 'view-census = 1' in hzd)

harness = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include <math.h>
typedef uint32_t UINT32; typedef unsigned UINT; typedef long LONG; typedef uint64_t UINT64; typedef uint8_t UINT8;
typedef int DXGI_FORMAT; typedef void *obj_handle_t;
typedef struct { int x; } SRWLOCK, CRITICAL_SECTION;
#define SRWLOCK_INIT { 0 }
static void AcquireSRWLockExclusive(SRWLOCK *l) { (void)l; }
static void ReleaseSRWLockExclusive(SRWLOCK *l) { (void)l; }
static void EnterCriticalSection(CRITICAL_SECTION *l) { (void)l; }
static void LeaveCriticalSection(CRITICAL_SECTION *l) { (void)l; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++*p; }
static char logbuf[16384]; static size_t loglen;
static void d3d12_log(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    loglen += (size_t)vsnprintf(logbuf + loglen, sizeof logbuf - loglen, fmt, ap);
    va_end(ap);
}
#define MAD_SKIN_CAP_BUDGET 8000
#define MAD_SKIN_WIN_CAPS 1000
#define MAD_SK_N 256u
static volatile LONG g_skin_cap_lines, g_skin_win_caps;
static SRWLOCK g_sk_lock = SRWLOCK_INIT;
static unsigned char *g_sk_cpu; static UINT g_sk_used;
static struct mad_skcap { char label[120]; UINT off, len, kind, stride; const void *q; } g_sk_cap[MAD_SK_N];
static volatile unsigned g_sk_n;
''' + body('static void mad_sk_cat(char *o, size_t cap, int *n, const char *fmt, ...)') + '\n' + \
    body('static void mad_skin_print(const struct mad_skcap *c)') + '\n' + body('static void mad_skin_flush(const void *q)') + r'''
/* typed views */
enum WMTPixelFormat { WMTPixelFormatInvalid = 0, WMTPixelFormatR32Uint = 53, WMTPixelFormatR32Sint = 54, WMTPixelFormatRGBA32Float = 125,
                      WMTPixelFormatR32Float = 55, WMTPixelFormatDepth32Float = 252 };
enum WMTTextureUsage { WMTTextureUsageShaderRead = 1, WMTTextureUsageShaderWrite = 2, WMTTextureUsageShaderAtomic = 32 };
enum { WMTTextureTypeTextureBuffer = 9, WMTResourceStorageModeShared = 0, WMTResourceStorageModePrivate = 2 };
struct WMTTextureInfo { int pixel_format; uint32_t width, height, depth, array_length; int type; uint32_t mipmap_level_count, sample_count;
                        enum WMTTextureUsage usage; int options; UINT64 gpu_resource_id; };
struct mad_descriptor { UINT64 gpu_va, texture_view_id, metadata; };
struct mad_tview { UINT fmt; UINT64 off, num; UINT8 uav; obj_handle_t tex; UINT64 id; };
struct mad_resource { obj_handle_t buffer; void *cpu; UINT64 size, gpu_address; struct mad_tview *tview; unsigned ntview, tview_cap; };
struct mad_device { CRITICAL_SECTION view_lock; };
static LONG g_tview_live, g_tview_made, g_typed_atomic_views;
static int metal_refuses, grow_fails;
static UINT64 next_id = 100;
static int mad_map_texture_format(DXGI_FORMAT f, int flags, enum WMTPixelFormat *out, int *is_depth) {
    (void)flags; *is_depth = 0;
    switch (f) { case 2: *out = WMTPixelFormatRGBA32Float; return 1; case 41: *out = WMTPixelFormatR32Float; return 1;
                 case 42: *out = WMTPixelFormatR32Uint; return 1; case 71: *out = WMTPixelFormatRGBA32Float; return 1; case 40: *out = WMTPixelFormatDepth32Float; *is_depth = 1; return 1;
                 default: return 0; }
}
static void mad_format_info(DXGI_FORMAT f, UINT *bytes, UINT *block) {
    *block = 1; *bytes = f == 2 ? 16 : f == 6 ? 12 : f == 71 ? 0 : 4;
    if (f == 71) *block = 4;
}
static int mad_typed_uav_atomic(void) { return 0; }
static obj_handle_t MTLBuffer_newTexture(obj_handle_t b, struct WMTTextureInfo *ti, UINT64 off, UINT64 bpr) {
    (void)b; (void)off; (void)bpr;
    if (metal_refuses) return NULL;
    ti->gpu_resource_id = next_id++;
    return (obj_handle_t)(uintptr_t)ti->gpu_resource_id;
}
static void NSObject_release(obj_handle_t h) { (void)h; }
static int mad_view_grow(struct mad_resource *r, void **arr, unsigned *cap, unsigned need, size_t elem) {
    (void)r;
    if (grow_fails) return 0;
    if (need > *cap) { *arr = realloc(*arr, need * elem); *cap = need; }
    return *arr != NULL;
}
static UINT64 last_vmap_id; static UINT32 last_vmap_a, last_vmap_b;
static void mad_vmap_put_locked(struct mad_device *d, UINT64 id, UINT32 a, UINT32 b) { (void)d; last_vmap_id = id; last_vmap_a = a; last_vmap_b = b; }
static void mad_view_census(const char *kind, struct mad_resource *r, unsigned n) { (void)kind; (void)r; (void)n; }
''' + body('static int mad_typed_buffer_view(struct mad_device *d, struct mad_resource *r, DXGI_FORMAT fmt,') + r'''
static int bad;
#define EXPECT(c, what) do { if (!(c)) { printf("FAIL %s\n", what); bad = 1; } else printf("ok   %s\n", what); } while (0)
static int qa, qb;
int main(void) {
    char t[24]; int n = 0;
    float v[4][4] = { { 0, 0, 0, 1 }, { 1, -2, 3, 1 }, { NAN, 0, 0, 1 }, { 2e5f, 0, 0, 1 } };
    unsigned char *buf = calloc(1, 4096);
    mad_sk_cat(t, sizeof t, &n, "%s", "0123456789");
    mad_sk_cat(t, sizeof t, &n, "%s", "0123456789abcdefghij");
    mad_sk_cat(t, sizeof t, &n, "%s", "more");
    EXPECT(n == (int)sizeof t - 1 && strlen(t) == sizeof t - 1 && !memcmp(t, "0123456789", 10), "line helper: stops at the end, never past it");

    g_sk_cpu = buf;
    memcpy(buf, v, sizeof v);
    memset(buf + 256, 0, 256);
    g_sk_cap[0] = (struct mad_skcap){ "A s1", 0, 64, 20, 16, &qa };
    g_sk_cap[1] = (struct mad_skcap){ "B in", 256, 256, 21, 0, &qb };
    g_sk_cap[2] = (struct mad_skcap){ "C s2", 512, 40, 20, 20, &qa };
    g_sk_n = 3; g_sk_used = 560;
    mad_skin_flush(&qa);
    EXPECT(strstr(logbuf, "[skin-cap] A s1: 4 vertices, position 0: 1, NaN/Inf: 1, over 1e5: 1, max |x| 2e+05") != NULL,
           "stream summary: zero, NaN and huge vertices counted, largest magnitude");
    EXPECT(strstr(logbuf, "v0 (0 0 0 1) v1 (1 -2 3 1)") != NULL, "stream summary: the first two vertices");
    EXPECT(strstr(logbuf, "[skin-cap] C s2: 2 vertices, position 0: 2") != NULL, "a 20-byte stream of zeros: both vertices at 0");
    EXPECT(strstr(logbuf, "B in") == NULL && g_sk_n == 1 && !strcmp(g_sk_cap[0].label, "B in") && g_sk_used == 512,
           "flush: only the signalling queue's captures, the rest kept and the buffer end recomputed");
    loglen = 0; logbuf[0] = 0;
    mad_skin_flush(&qb);
    EXPECT(strstr(logbuf, "[skin-cap] B in: 64 words, zero 64, NaN/Inf 0") != NULL && g_sk_n == 0 && g_sk_used == 0,
           "input summary, then the buffer is empty");
    g_sk_cap[0] = (struct mad_skcap){ "D", 0, 16, 20, 16, &qa }; g_sk_n = 1; g_sk_used = 16; loglen = 0; logbuf[0] = 0;
    mad_skin_flush(NULL);
    EXPECT(g_sk_n == 0 && strstr(logbuf, "1 captures dropped") != NULL && strstr(logbuf, "[skin-cap] D") == NULL,
           "flush(NULL): a queue that never waited loses its captures, with a line");

    {
        struct mad_device d = { { 0 } };
        struct mad_resource buf32 = { (obj_handle_t)1, NULL, 32u << 20, 0x100000000ull, NULL, 0, 0 };
        struct mad_resource tiny = { (obj_handle_t)2, NULL, 4, 0x200000000ull, NULL, 0, 0 };
        struct mad_resource tex = { NULL, NULL, 0, 0, NULL, 0, 0 };
        struct mad_descriptor e;
        int why;
        why = -1; EXPECT(mad_typed_buffer_view(&d, &buf32, 2, 0, 65536, 1, &e, &why) == 1 && why == -1 &&
                         e.texture_view_id && (e.metadata >> 63), "a normal view: made, no reason");
        why = -1; EXPECT(mad_typed_buffer_view(&d, &buf32, 2, 0, 65536, 1, &e, &why) == 1 && why == -1 && buf32.ntview == 1,
                         "the same view again: the cached one");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &tex, 2, 0, 16, 0, &e, &why) == 0 && why == 1, "a texture: reason 1");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &buf32, 6, 0, 16, 0, &e, &why) == 0 && why == 2, "RGB32 (no Metal format): reason 2");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &buf32, 40, 0, 16, 0, &e, &why) == 0 && why == 3, "a depth format: reason 3");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &buf32, 71, 0, 16, 0, &e, &why) == 0 && why == 4, "a block format: reason 4");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &tiny, 2, 65536, 16, 1, &e, &why) == 0 && why == 5,
                        "a view far past a 4-byte buffer: reason 5 (and no wrap-around width)");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &tiny, 41, 0, 16, 0, &e, &why) == 1 && why == 9 && last_vmap_a == 16,
                        "a view longer than its buffer: made, cut short (reason 9), the descriptor keeps its count");
        why = 0; EXPECT(mad_typed_buffer_view(&d, &tiny, 2, 0, 16, 0, &e, &why) == 0 && why == 5,
                        "an element wider than the buffer: no width, reason 5");
        metal_refuses = 1;
        why = 0; EXPECT(mad_typed_buffer_view(&d, &buf32, 41, 8, 8, 0, &e, &why) == 0 && why == 7, "Metal refuses: reason 7");
        metal_refuses = 0; grow_fails = 1;
        why = 0; EXPECT(mad_typed_buffer_view(&d, &buf32, 41, 16, 8, 0, &e, &why) == 0 && why == 8, "the view list cannot grow: reason 8");
        grow_fails = 0;
        EXPECT(mad_typed_buffer_view(&d, &buf32, 41, 24, 8, 0, &e, NULL) == 1, "no reason pointer: still works");
    }
    printf("%s\n", bad ? "harness FAILED" : "harness ok");
    return bad;
}
'''

cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
if not cc:
    check('a host C compiler', False)
else:
    with tempfile.TemporaryDirectory(prefix='madeira-skin-') as tmp:
        src = Path(tmp) / 'skin.c'
        exe = Path(tmp) / 'skin'
        src.write_text(harness)
        b = subprocess.run([cc, '-std=gnu11', '-Wall', '-Wno-unused-function', '-Wno-unused-variable', str(src), '-o', str(exe), '-lm'],
                           capture_output=True, text=True)
        check('the helpers compile on the host', b.returncode == 0)
        if b.returncode:
            print(b.stderr[-3000:])
        else:
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            print(run.stdout, end='')
            check('the helpers behave', run.returncode == 0)

if not ok:
    sys.exit(1)
print('PASS: skin-check')
