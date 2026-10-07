#!/usr/bin/env python3
"""Memory budgets from the device's RAM when the jetsam limit is above it.

An iPad with 7644 MB of RAM ran God of War (another person's device, build
269, log 2026-10-01 10:39:48) with a measured process limit of 8192 MB
(os_proc_available_memory + phys_footprint): the limit sits ABOVE the RAM the
device has, so nothing in Madeira ever thought it was short. The footprint
reached 8175 MB with 4.4 GB in the compressor and the game drew black frames
at 0-7 FPS -- starved, never killed. The owner's 12 GB iPhone (limit 8192,
RAM 11695 MB) has 3.5 GB to spare above that limit; an 8 GB device has none.

Two native consumers size themselves from that limit:
  * the video memory budget DXGI reports (ml1042, base) and its dynamic trim
    (ml1075/ml1103, starts vram-trim-mb below the limit);
  * MadeiraCtl op 7, the headroom DXMT's automatic BC mip clamp reads (also
    from the committed 64-bit PE d3d11.dll, through the native side).
They now use min(limit, hw.memsize - reserve). The reserve is what iOS and its
daemons keep for themselves: madeira.cfg ram-reserve-mb, default 2048; 0
turns this off. On the owner's iPhone 11695 - 2048 = 9647 MB is above the
8192 MB limit, so nothing changes there. On the iPad the cap is ~5.6 GB: the
video budget drops to its 1 GB floor, the trim and the mip clamp start ~2.5
GB earlier. 8 GB iPhones (15 Pro, 16 Pro ...) get the same treatment when
their limit is above their RAM minus the reserve.

The RAM Wine reports to the game (ml992 in virtual_ios.c) is not changed.

[ram-cap] lines: the cap once, and once when a budget is actually lowered.
Native only; not in the i386 farm key. Idempotent; fails by name if an
anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: RAM cap"

PAIRS = [
    ("""#include <os/proc.h>
#include <mach/mach.h>
static uint64_t madeira_ml1042_video_budget(uint64_t metal_recommended);
""",
     """#include <os/proc.h>
#include <mach/mach.h>
#include <sys/sysctl.h>
/* madeira-bcd: RAM cap (tools/patch-winemetal-ram-cap.py) -- the memory a
 * budget may plan with is min(jetsam limit, hw.memsize - ram-reserve-mb). */
static uint64_t madeira_ram_cap(void) {
  static uint64_t cap = 1;   /* 1 = not computed yet */
  if (cap == 1) {
    uint64_t mem = 0; size_t len = sizeof mem;
    long long reserve = madeira_cfg_int("ram-reserve-mb", 2048);
    cap = 0;
    if (reserve > 0 && !sysctlbyname("hw.memsize", &mem, &len, NULL, 0) && mem > ((uint64_t)reserve << 20))
      cap = mem - ((uint64_t)reserve << 20);
    fprintf(stderr, "[ram-cap] madeira-bcd hw.memsize %llu MB, iOS reserve %lld MB -> memory cap %llu MB "
                    "(madeira.cfg ram-reserve-mb; 0 = off)\\n",
            (unsigned long long)(mem >> 20), reserve, (unsigned long long)(cap >> 20));
  }
  return cap;
}
/* os_proc_available_memory(), lowered so that footprint + the result never
 * exceeds the cap. Stays 0 when the limit is unknown. */
static uint64_t madeira_avail_memory(uint64_t foot) {
  uint64_t avail = (uint64_t)os_proc_available_memory(), cap = madeira_ram_cap();
  if (avail && cap && foot + avail > cap) {
    static int said;
    if (!said++)
      fprintf(stderr, "[ram-cap] madeira-bcd jetsam limit %llu MB is above this device's cap %llu MB: "
                      "video budget, its trim and the mip clamp use the cap\\n",
              (unsigned long long)((foot + avail) >> 20), (unsigned long long)(cap >> 20));
    avail = cap > foot ? cap - foot : (1ull << 20);
  }
  return avail;
}
/* The process limit a budget plans with: min(jetsam limit, cap). */
static uint64_t madeira_mem_limit(uint64_t foot) {
  uint64_t limit = (uint64_t)os_proc_available_memory() + foot, cap = madeira_ram_cap();
  if (cap && limit > cap) {
    madeira_avail_memory(foot);   /* logs the first lowering */
    limit = cap;
  }
  return limit;
}
static uint64_t madeira_ml1042_video_budget(uint64_t metal_recommended);
"""),
    # ml1075 dynamic budget
    ("""    if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &cnt) == KERN_SUCCESS) foot = vmi.phys_footprint;
    limit = (uint64_t)os_proc_available_memory() + foot;
    /* ml1103:""",
     """    if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &cnt) == KERN_SUCCESS) foot = vmi.phys_footprint;
    limit = madeira_mem_limit(foot); /* madeira-bcd: RAM cap */
    /* ml1103:"""),
    # ml1042 initial budget
    ("""    if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &cnt) == KERN_SUCCESS) foot = vmi.phys_footprint;
    limit = (uint64_t)os_proc_available_memory() + foot;
    const uint64_t guest_ram""",
     """    if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &cnt) == KERN_SUCCESS) foot = vmi.phys_footprint;
    limit = madeira_mem_limit(foot); /* madeira-bcd: RAM cap */
    const uint64_t guest_ram"""),
    # MadeiraCtl op 7 (mip clamp headroom)
    ("""    if (wmtr_enabled()) break;
    avail = (uint64_t)os_proc_available_memory();
    if (!avail) break;
    a->len = avail;
    a->ptr = task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &cnt) == KERN_SUCCESS
                 ? (uint64_t)vmi.phys_footprint : 0;
    a->ret = 1;
""",
     """    if (wmtr_enabled()) break;
    { /* madeira-bcd: RAM cap -- footprint first, then the capped headroom */
      uint64_t foot = task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &cnt) == KERN_SUCCESS
                          ? (uint64_t)vmi.phys_footprint : 0;
      avail = madeira_avail_memory(foot);
      if (!avail) break;
      a->len = avail;
      a->ptr = foot;
    }
    a->ret = 1;
"""),
]


def main():
    s = PATH.read_text()
    if MARKER in s:
        print("winemetal_unix.c: already patched")
        return 0
    if "madeira_avail_memory(" in s and '"ram-reserve-mb"' in s:
        # willfaust/dxmt#7 (644a354) + bb18171 (default reserve 1536 MB, not 2048)
        print("winemetal_unix.c: RAM cap is upstream (willfaust/dxmt#7); nothing to do")
        return 0
    for old, new in PAIRS:
        if s.count(old) != 1:
            sys.exit(f"patch-winemetal-ram-cap: anchor found {s.count(old)} times (want 1) in {PATH}:\n{old}")
        s = s.replace(old, new)
    PATH.write_text(s)
    print("winemetal_unix.c: patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
