#!/usr/bin/env python3
"""Add 30 and 40 FPS present-pacing modes to DXMT's winemetal unix side.

research/dxmt/src/winemetal/unix/winemetal_unix.c paces every present
(D3D11 through DXMT, and D3D12, whose runtime presents through the same
winemetal call) by g_madeira_vsync_mode: 1 = afterMinimumDuration(1/60),
0 = display maximum, 2 = raw. This adds

    3 = afterMinimumDuration(1/30)
    4 = afterMinimumDuration(1/40)   (needs the 120 Hz panel; the app holds it)

The drawable pool gives back-pressure, so the game itself runs at the cap,
exactly as mode 1 does for 60. Idempotent; fails by name if the anchor moves
(the dxmt pin changed). Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("research/dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: 30/40 FPS pacing"

ANCHOR = """  } else if (mode == 2) {
    /* Frame-skip gating lives in _MetalLayer_nextDrawable (nil return);"""

ADDITION = """  } else if (mode == 3 || mode == 4) {
    /* madeira-bcd: 30/40 FPS pacing (tools/patch-dxmt-frame-limits.py). */
    madeira_log_present_cadence(mode == 3 ? "presentDrawable30" : "presentDrawable40", 0.0);
    [(id<MTLCommandBuffer>)params->handle presentDrawable:(id<MTLDrawable>)params->arg
                                     afterMinimumDuration:(mode == 3 ? 1.0 / 30.0 : 1.0 / 40.0)];
"""

LOG_OLD = '"[iOS DXMT] vsync_mode=%d (1=locked60 0=max 2=raw)\\n"'
LOG_NEW = '"[iOS DXMT] vsync_mode=%d (1=locked60 0=max 2=raw 3=locked30 4=locked40)\\n"'


def main():
    s = PATH.read_text()
    if MARKER in s:
        print("winemetal_unix.c: 30/40 FPS pacing already present")
        return 0
    if s.count(ANCHOR) != 1:
        print(f"::error::{PATH}: present-pacing anchor not found once -- dxmt moved, review this patch")
        return 1
    s = s.replace(ANCHOR, ADDITION + ANCHOR)
    if s.count(LOG_OLD) == 1:
        s = s.replace(LOG_OLD, LOG_NEW)
    PATH.write_text(s)
    print("winemetal_unix.c: 30/40 FPS pacing modes 3 and 4 added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
