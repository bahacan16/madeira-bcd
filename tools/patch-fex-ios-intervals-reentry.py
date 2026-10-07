#!/usr/bin/env python3
"""Keep InvalidationTracker from waiting on its own IntervalsLock.

God of War on madeira-bcd builds 226-231 (logs 2026-09-30 08:01 and 08:28,
[guest-stk]), symbolized against the committed xtajit64.dll:

  HandleMemoryProtectionNotification      <- holds IntervalsLock exclusively
    LogMan::Msg::EFmt("[iOS-xrem] ...")   <- logs under the lock
      MFmtImpl -> FEXCore::Allocator::aligned_alloc
        rpmalloc heap_get_page_generic -> VirtualAlloc
          ntdll NtAllocateVirtualMemory -> NotifyMemoryAlloc
            HandleMemoryProtectionNotification
              std::unique_lock(IntervalsLock)   <- same thread, waits forever

IntervalsLock is a std::shared_mutex: not recursive, so any allocation made
while it is held (a log line, an IntervalList insert) that grows FEX's heap
comes back through the memory notification and parks the thread on itself.

The patch records which thread holds IntervalsLock exclusively and makes the
memory notifications that can arrive from inside such an allocation
(HandleMemoryProtectionNotification, InvalidateAlignedInterval,
InvalidateContainingSection) return at once on that thread. What they skip is
FEX's own heap growing or shrinking, never guest code: they only nest while
FEX code holds the lock. They must not log or allocate either (rpmalloc is in
the middle of mapping a page), so they only count; the count is reported from
HandleImageMap, outside the lock.

Usage: patch-fex-ios-intervals-reentry.py FEX/Source/Windows/Common
"""
import os
import sys

d = sys.argv[1]
marker = "madeira-bcd: IntervalsLock re-entry"

# willfaust/FEX#8 (1992e74, FEX 3bec2ac): the same change upstream, as
# InvalidationTracker::OwnedSharedMutex. Nothing to do on such a pin.
_h = open(os.path.join(d, "InvalidationTracker.h")).read()
if marker not in _h and "class OwnedSharedMutex" in _h and "HeldByThisThread()" in _h:
    print("InvalidationTracker: IntervalsLock re-entry is upstream (willfaust/FEX#8); nothing to do")
    sys.exit(0)


def patch(name, edits):
    path = os.path.join(d, name)
    src = open(path).read()
    if marker in src:
        print(f"{name}: already patched")
        return
    for old, new in edits:
        if src.count(old) != 1:
            sys.exit(f"patch-fex-ios-intervals-reentry: anchor not found once in {name}:\n{old}")
        src = src.replace(old, new)
    open(path, "w").write(src)
    print(f"{name}: patched")


patch("InvalidationTracker.h", [
    ("#include <shared_mutex>\n", "#include <shared_mutex>\n#include <atomic>\n"),
    ("  std::shared_mutex IntervalsLock;\n", """  /* madeira-bcd: IntervalsLock re-entry (tools/patch-fex-ios-intervals-reentry.py).
   * A std::shared_mutex that remembers which thread holds it exclusively, so a
   * memory notification raised by an allocation made under the lock (a log line
   * growing FEX's heap) can tell it would wait on its own thread. */
  struct IosOwnedSharedMutex {
    static uint64_t Self() {
#ifdef FEX_IOS_HOST
      // Darwin's per-thread TSD base; x18 (the TEB) is not reliable on iOS.
      uint64_t T;
      __asm__ volatile("mrs %0, TPIDRRO_EL0" : "=r"(T));
      return T & ~uint64_t(7);
#else
      return reinterpret_cast<uint64_t>(NtCurrentTeb());
#endif
    }
    void lock() {
      M.lock();
      Owner.store(Self(), std::memory_order_relaxed);
    }
    bool try_lock() {
      if (!M.try_lock()) {
        return false;
      }
      Owner.store(Self(), std::memory_order_relaxed);
      return true;
    }
    void unlock() {
      Owner.store(0, std::memory_order_relaxed);
      M.unlock();
    }
    void lock_shared() {
      M.lock_shared();
    }
    bool try_lock_shared() {
      return M.try_lock_shared();
    }
    void unlock_shared() {
      M.unlock_shared();
    }
    bool HeldByThisThread() const {
      return Owner.load(std::memory_order_relaxed) == Self();
    }
    std::shared_mutex M;
    std::atomic<uint64_t> Owner {0};
  };
  IosOwnedSharedMutex IntervalsLock;
"""),
])

GUARD = """  if (IntervalsLock.HeldByThisThread()) {
    // madeira-bcd: IntervalsLock re-entry -- FEX's own heap, raised under the lock; see the header.
    IosNestedSkips.fetch_add(1, std::memory_order_relaxed);
    return{ret};
  }
"""

patch("InvalidationTracker.cpp", [
    ('#include "InvalidationTracker.h"\n', '''#include "InvalidationTracker.h"

/* madeira-bcd: IntervalsLock re-entry (tools/patch-fex-ios-intervals-reentry.py).
 * Notifications skipped because this thread already held IntervalsLock. */
static std::atomic<uint64_t> IosNestedSkips {0};
static std::atomic<uint64_t> IosNestedSkipsReported {0};
'''),
    ("""void InvalidationTracker::HandleMemoryProtectionNotification(uint64_t Address, uint64_t Size, ULONG Prot) {
""", """void InvalidationTracker::HandleMemoryProtectionNotification(uint64_t Address, uint64_t Size, ULONG Prot) {
""" + GUARD.replace("{ret}", "")),
    ("""InvalidationTracker::InvalidateContainingSectionResult InvalidationTracker::InvalidateContainingSection(uint64_t Address, bool Free) {
""", """InvalidationTracker::InvalidateContainingSectionResult InvalidationTracker::InvalidateContainingSection(uint64_t Address, bool Free) {
""" + GUARD.replace("{ret}", " {Address, 0}")),
    ("""void InvalidationTracker::InvalidateAlignedInterval(uint64_t Address, uint64_t Size, bool Free) {
""", """void InvalidationTracker::InvalidateAlignedInterval(uint64_t Address, uint64_t Size, bool Free) {
""" + GUARD.replace("{ret}", "")),
    ("""  FEX_CONFIG_OPT(MonoHacks, MONOHACKS);
""", """  {
    // madeira-bcd: IntervalsLock re-entry -- report skipped nested notifications, outside the lock.
    const auto Skips = IosNestedSkips.load(std::memory_order_relaxed);
    auto Reported = IosNestedSkipsReported.load(std::memory_order_relaxed);
    if (Skips != Reported && IosNestedSkipsReported.compare_exchange_strong(Reported, Skips)) {
      LogMan::Msg::EFmt("[iv-reentry] madeira-bcd: {} memory notification(s) raised while this thread held IntervalsLock "
                        "were skipped (FEX's own heap; waiting would deadlock)",
                        Skips);
    }
  }

  FEX_CONFIG_OPT(MonoHacks, MONOHACKS);
"""),
])
