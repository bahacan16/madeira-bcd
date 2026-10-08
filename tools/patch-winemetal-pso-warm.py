#!/usr/bin/env python3
"""Pipeline cache warm-up for madeira-d3d12 (MadeiraCtl op 8).

madeira-d3d12 builds a lazy pipeline at its first draw. Metal keeps what it
compiled in its on-disk cache across sessions and builds: a pipeline this
device has built before takes 0.3-0.5 ms, a new one 35-47 ms (GTA V Enhanced,
builds 447-451), and new ones came in bursts that froze the game for 100-400
ms. With madeira.cfg / a game's config pso-warm = N (1-4), madeira-d3d12 hands
every lazy pipeline here when the game creates it. Op 8 copies the pipeline
info (and the vertex descriptor), retains the functions it names and returns
at once; DXMT's own builder (_MTLDevice_newRenderPipelineState, its VD variant,
_MTLDevice_newComputePipelineState: the descriptor the first draw will build)
runs on one of N serial utility-QoS queues and the pipeline is released at
once. Only the compile is kept, in Metal's cache, so the first draw finds it;
memory stays as lazy creation left it. [pso-warm] lines count compiled ones,
ones already in the cache and the backlog. Without pso-warm, or in remote mode,
op 8 answers 0 and madeira-d3d12 turns the warm-up off. The i386 entry does
not forward op 8 (it carries pointers). Native only. Idempotent; fails by name
if an anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: pipeline cache warm-up"

ANCHOR_FN = "static NTSTATUS _madeira_ctl(void *args) {\n"
HELPERS = r'''/* madeira-bcd: pipeline cache warm-up (tools/patch-winemetal-pso-warm.py).
 * MadeiraCtl op 8 from madeira-d3d12 (pso-warm = N): compile one lazy pipeline
 * on a utility-QoS queue with DXMT's own builder, release it at once, keep
 * only Metal's cached compile. See the script for the numbers. */
static dispatch_queue_t madeira_pso_warm_q[4];
static unsigned madeira_pso_warm_nq;
static _Atomic unsigned madeira_pso_warm_rr;
static _Atomic long madeira_pso_warm_sent, madeira_pso_warm_done, madeira_pso_warm_hits, madeira_pso_warm_failed;
static _Atomic uint64_t madeira_pso_warm_ns_compiled, madeira_pso_warm_ns_hits, madeira_pso_warm_said_ns;

static int madeira_pso_warm_setup(void) {
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    long long n = madeira_cfg_int("pso-warm", 0);
    if (n <= 0) return;
    if (n > 4) n = 4;
    dispatch_queue_attr_t attr = dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL, QOS_CLASS_UTILITY, 0);
    for (long long i = 0; i < n; i++) madeira_pso_warm_q[i] = dispatch_queue_create("madeira.pso-warm", attr);
    madeira_pso_warm_nq = (unsigned)n;
    fprintf(stderr, "[pso-warm] madeira-bcd pipeline cache warm-up on: %lld utility queue(s) (madeira.cfg pso-warm)\n", n);
  });
  return madeira_pso_warm_nq > 0;
}

/* One finished warm-up: tally it, and report every 500 and whenever the
 * backlog empties (at most every 10 s). A build under 3 ms was a cache hit. */
static void madeira_pso_warm_note(uint64_t ns, int ok) {
  long done = atomic_fetch_add_explicit(&madeira_pso_warm_done, 1, memory_order_relaxed) + 1;
  long sent = atomic_load_explicit(&madeira_pso_warm_sent, memory_order_relaxed);
  uint64_t now = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
  if (!ok)
    atomic_fetch_add_explicit(&madeira_pso_warm_failed, 1, memory_order_relaxed);
  else if (ns < 3000000) {
    atomic_fetch_add_explicit(&madeira_pso_warm_hits, 1, memory_order_relaxed);
    atomic_fetch_add_explicit(&madeira_pso_warm_ns_hits, ns, memory_order_relaxed);
  } else
    atomic_fetch_add_explicit(&madeira_pso_warm_ns_compiled, ns, memory_order_relaxed);
  if ((done % 500) == 0 ||
      (done >= sent && now - atomic_load_explicit(&madeira_pso_warm_said_ns, memory_order_relaxed) > 10000000000ull)) {
    long hits = atomic_load_explicit(&madeira_pso_warm_hits, memory_order_relaxed);
    long failed = atomic_load_explicit(&madeira_pso_warm_failed, memory_order_relaxed);
    long compiled = done - hits - failed;
    double msc = (double)atomic_load_explicit(&madeira_pso_warm_ns_compiled, memory_order_relaxed) / 1e6;
    double msh = (double)atomic_load_explicit(&madeira_pso_warm_ns_hits, memory_order_relaxed) / 1e6;
    atomic_store_explicit(&madeira_pso_warm_said_ns, now, memory_order_relaxed);
    fprintf(stderr, "[pso-warm] madeira-bcd %ld of %ld pipelines warmed: %ld compiled (avg %.1f ms), "
                    "%ld already in Metal's cache (avg %.2f ms), %ld failed; %ld waiting\n",
            done, sent, compiled, compiled > 0 ? msc / compiled : 0.0, hits, hits ? msh / hits : 0.0, failed,
            sent > done ? sent - done : 0);
  }
}

/* kind 0: render pipeline, 1: render pipeline + vertex descriptor, 2: compute. */
static uint32_t madeira_pso_warm_submit(obj_handle_t device, const void *info, const void *vd, unsigned kind) {
  size_t isz = kind == 2 ? sizeof(struct WMTComputePipelineInfo) : sizeof(struct WMTRenderPipelineInfo);
  size_t vsz = kind == 1 ? sizeof(struct WMTVertexDescriptorInfo) : 0;
  unsigned char *copy;
  uintptr_t f0, f1 = 0;
  if (!device || !info || kind > 2 || (kind == 1 && !vd)) return 0;
  copy = malloc(isz + vsz);
  if (!copy) return 0;
  memcpy(copy, info, isz);
  if (vsz) memcpy(copy + isz, vd, vsz);
  if (kind == 2)
    f0 = (uintptr_t)((const struct WMTComputePipelineInfo *)copy)->compute_function;
  else {
    f0 = (uintptr_t)((const struct WMTRenderPipelineInfo *)copy)->vertex_function;
    f1 = (uintptr_t)((const struct WMTRenderPipelineInfo *)copy)->fragment_function;
  }
  /* The game may release the pipeline (and its functions) before the queue
   * gets to it. */
  if (f0) [(id)f0 retain];
  if (f1) [(id)f1 retain];
  [(id)device retain];
  atomic_fetch_add_explicit(&madeira_pso_warm_sent, 1, memory_order_relaxed);
  dispatch_async(madeira_pso_warm_q[atomic_fetch_add_explicit(&madeira_pso_warm_rr, 1, memory_order_relaxed) %
                                    madeira_pso_warm_nq], ^{
    @autoreleasepool {
      uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
      obj_handle_t pso = 0;
      if (kind == 2) {
        struct unixcall_mtldevice_newcomputepso p;
        memset(&p, 0, sizeof p);
        p.device = device; p.info.ptr = copy;
        _MTLDevice_newComputePipelineState(&p);
        pso = p.ret_pso;
      } else if (kind == 1) {
        struct unixcall_mtldevice_newrenderpso_vd p;
        memset(&p, 0, sizeof p);
        p.device = device; p.info.ptr = copy; p.vd.ptr = copy + isz;
        _MTLDevice_newRenderPipelineStateVD(&p);
        pso = p.ret_pso;
      } else {
        struct unixcall_mtldevice_newrenderpso p;
        memset(&p, 0, sizeof p);
        p.device = device; p.info.ptr = copy;
        _MTLDevice_newRenderPipelineState(&p);
        pso = p.ret_pso;
      }
      madeira_pso_warm_note(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0, pso != 0);
      if (pso) [(id)pso release];
      if (f0) [(id)f0 release];
      if (f1) [(id)f1 release];
      [(id)device release];
      free(copy);
    }
  });
  return 1;
}

'''

ANCHOR_CASE = """  case 6:     /* ml1136: requested fence-chain mode (0 = none) */
    a->ret = (uint32_t)g_madeira_fence_req;
    break;
"""
CASE = """  case 8: {   /* madeira-bcd: pipeline cache warm-up (tools/patch-winemetal-pso-warm.py); len = kind
               * (0 render, 1 render + vertex descriptor, 2 compute), ptr = { device, info, vd } */
    const uint64_t *r = (const uint64_t *)(uintptr_t)a->ptr;
    if (!r || wmtr_enabled() || !madeira_pso_warm_setup()) break;
    a->ret = madeira_pso_warm_submit((obj_handle_t)r[0], (const void *)(uintptr_t)r[1],
                                     (const void *)(uintptr_t)r[2], (unsigned)a->len);
    break;
  }
"""


def main():
    src = PATH.read_text()
    if MARKER in src:
        print("patch-winemetal-pso-warm: already applied")
        return 0
    if src.count(ANCHOR_FN) != 1:
        sys.exit("patch-winemetal-pso-warm: _madeira_ctl anchor not found once")
    if src.count(ANCHOR_CASE) != 1:
        sys.exit("patch-winemetal-pso-warm: _madeira_ctl op 6 anchor not found once")
    for fn in ("_MTLDevice_newRenderPipelineState(void *obj) {", "_MTLDevice_newRenderPipelineStateVD(void *obj) {",
               "_MTLDevice_newComputePipelineState(void *obj) {"):
        at = src.find(fn)
        if at < 0 or at > src.index(ANCHOR_FN):
            sys.exit(f"patch-winemetal-pso-warm: {fn.split('(')[0]} must be defined before _madeira_ctl")
    src = src.replace(ANCHOR_FN, HELPERS + ANCHOR_FN)
    src = src.replace(ANCHOR_CASE, ANCHOR_CASE + CASE)
    PATH.write_text(src)
    print("patch-winemetal-pso-warm: MadeiraCtl op 8 warms lazy pipelines on utility queues with madeira.cfg pso-warm = N")
    return 0


if __name__ == "__main__":
    sys.exit(main())
