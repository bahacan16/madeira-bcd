#!/usr/bin/env python3
"""Opt-in safe mode for the orphan-lock reaper (ios_orphan_check, ml447).

The reaper releases an exclusively held SRW lock that has >= 3 parked waiters
and that no live thread stamps, "for 3 consecutive monitor cycles". Two faults:

  - The strikes are counted once per parked WAITER, not once per cycle: a lock
    with 3 waiters gets strikes 1/3, 2/3, 3/3 in one pass and is released at
    once (Horizon Zero Dawn 2026-10-09 16:20 l.32460-32463 and 19:44 l.60243-
    60245; the monitor runs every 250 ms above 2.4 GB).
  - Only FEX's code-buffer locks are ever stamped. Every other lock, the
    game's and FEX's own, is "unstamped", so any of them that is held while
    3 threads wait looks orphaned. Its live owner then fails in
    RtlReleaseSRWLockExclusive ("is not owned exclusive"): Horizon Zero Dawn
    stopped with its "Error" box right after the second such release (build
    479, 19:45); GTA V and RDR2 logs show the same false releases.

env MADEIRA_LOCK_ORPHAN:
  unset (or any other value)  the reaper as before (default; GTA unchanged)
  stamped                     strikes once per lock per cycle, and only locks
                              a live thread has stamped at some point (FEX's
                              code-buffer locks) can be released: their owner
                              stamps while holding, so a held lock nobody
                              stamps any more lost its owner
  0                           no orphan-lock releases at all

The dead-port reaper (a Mach-dead thread whose TEB still stamps a lock) is
untouched. Usage: patch-wine-lock-orphan.py wine/dlls/ntdll/unix/sync.c
(idempotent).
"""
from pathlib import Path
import sys

MARKER = "madeira-bcd lock-orphan"

HEAD = """void ios_orphan_check( const unsigned long long *live_stamps, int nstamps )
{
    static struct { unsigned long long lock; int strikes; } susp[8];
    int i, j, k;
"""
HEAD_NEW = HEAD + """    /* madeira-bcd lock-orphan (tools/patch-wine-lock-orphan.py): env
     * MADEIRA_LOCK_ORPHAN = stamped counts strikes once per lock per cycle and
     * releases only locks a live thread has stamped (FEX's code-buffer locks);
     * = 0 releases nothing; unset keeps the reaper as it was. */
    static int orphan_mode = -1;            /* 0 as before, 1 stamped, 2 off */
    static unsigned long long ever[64];     /* locks a live thread has stamped */
    static int never, ever_next;
    unsigned long long done[32];            /* locks already looked at in this cycle */
    int ndone = 0;

    if (orphan_mode < 0)
    {
        const char *e = getenv( "MADEIRA_LOCK_ORPHAN" );
        orphan_mode = !e ? 0 : !strcmp( e, "stamped" ) ? 1 : !strcmp( e, "0" ) ? 2 : 0;
        if (orphan_mode)
            dprintf( 2, "[lock-orphan] madeira-bcd MADEIRA_LOCK_ORPHAN=%s: %s\\n", e,
                     orphan_mode == 1 ? "only locks a live thread has stamped are released, after 3 monitor cycles"
                                      : "no orphan-lock releases" );
    }
    if (orphan_mode == 2) return;
    if (orphan_mode == 1)
        for (k = 0; k < nstamps; k++)
        {
            if (!live_stamps[k]) continue;
            for (j = 0; j < never; j++) if (ever[j] == live_stamps[k]) break;
            if (j < never) continue;
            ever[ever_next] = live_stamps[k];
            ever_next = (ever_next + 1) % 64;
            if (never < 64) never++;
        }
"""

LOCK = """        if (!a || ((ULONG_PTR)a & 3) != 2 || (ULONG_PTR)a < 0x10000 || (ULONG_PTR)a >= 0x8000000000ULL) continue;
        lock = (unsigned long long)(ULONG_PTR)a - 2;
"""
LOCK_NEW = LOCK + """        if (orphan_mode == 1)
        {
            /* madeira-bcd lock-orphan: each lock once per cycle (one strike, not
             * one per parked waiter), and never a lock nobody has stamped */
            for (k = 0; k < ndone; k++) if (done[k] == lock) break;
            if (k < ndone || ndone == 32) continue;
            done[ndone++] = lock;
            for (k = 0; k < never; k++) if (ever[k] == lock) break;
            if (k == never) continue;
        }
"""


def patch(source):
    if MARKER in source:
        return source
    for old in (HEAD, LOCK):
        if source.count(old) != 1:
            raise ValueError("lock-orphan anchor missing or ambiguous: " + old.splitlines()[0])
    return source.replace(HEAD, HEAD_NEW).replace(LOCK, LOCK_NEW)


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/unix/sync.c")
    source = path.read_text()
    try:
        result = patch(source)
    except ValueError as error:
        sys.exit(str(error))
    if result == source:
        print("already patched")
    else:
        path.write_text(result)
        print("patched")


if __name__ == "__main__":
    main()
