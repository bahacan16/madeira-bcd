#!/usr/bin/env python3
"""Stop the SRW orphan heuristic from reaping locks it cannot prove are dead.

NOT OUR FIX. This is the Madeira Build 79 corresponding-source package's
`13-srw-orphan-fix/source.patch` (byte-identical to the earlier
`05-srw-orphan-guard.patch`), written by Madeira's developer and applied
here verbatim.

sync.c watches anonymous SRW locks that show waiters but carry no live FEX
ownership stamp, and after three strikes it called

    ios_srw_reap_exclusive( lock, 0xDEADull );

-- releasing the lock on the owner's behalf and waking the waiters. A
missing stamp is not proof the owner is gone, though: an SRWLOCK held by
native code, or by a thread FEX has not stamped yet, looks exactly the
same. Reaping it hands the lock to a waiter while the real owner is still
inside the critical section. The strike counter and its [lock-orphan]
line stay; only the reap goes.

Applied as a CI patch because the wine submodule points at willfaust/wine,
which we cannot push to -- the same reason the FEX patches live here.
"""
import sys

OLD = """        if (susp[j].strikes >= 3)
        {
            ios_srw_reap_exclusive( lock, 0xDEADull );
            susp[j].lock = 0;"""

NEW = """        if (susp[j].strikes >= 3)
        {
            /* A missing FEX stamp does not establish the owner of an anonymous
             * SRW is dead. Keep this heuristic diagnostic-only; leave the
             * real owner responsible for release and waiter notification. */
            susp[j].lock = 0;"""


def main(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        s = f.read()
    if "Keep this heuristic diagnostic-only" in s:
        print("::notice::wine SRW orphan reap already disarmed")
        return 0
    n = s.count(OLD)
    if n != 1:
        print(f"::error::SRW orphan anchor not found ({n} matches) in {path} -- pinned wine changed, review needed")
        return 1
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(s.replace(OLD, NEW))
    print("::notice::wine SRW orphan reap disarmed (heuristic is now diagnostic-only)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
