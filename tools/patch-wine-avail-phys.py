#!/usr/bin/env python3
"""Available physical memory as this app's headroom, opt-in (madeira.cfg or the
game's own file: avail-phys = 1).

get_performance_info (wine/dlls/ntdll/unix/system.c) sets
SystemPerformanceInformation.AvailablePages, GlobalMemoryStatusEx's
ullAvailPhys, from the whole phone's free + inactive pages. This app is killed
at its own memory limit, so Windows programs (Social Club's and Steam's
Chromium, the game) never saw memory run short. The patch passes the value
through ios_avail_phys (build/ntdll-unix/virtual_ios.c), which with the switch
lowers it to what remains before the limit and otherwise returns it unchanged.

Patches system.c in place; the submodule pin and the cache keys (wine revision)
do not change. Idempotent; exits 1 with a message if the anchor is missing.
tests/host/check-avail-phys.py checks it.
Usage: patch-wine-avail-phys.py wine/dlls/ntdll/unix/system.c
"""
import sys

MARKER = "madeira-bcd: avail-phys"
ANCHOR = "    info->AvailablePages      = freeram / page_size;\n"
NEW = ("#ifdef __APPLE__\n"
       "    {\n"
       "        /* " + MARKER + " (tools/patch-wine-avail-phys.py): with madeira.cfg\n"
       "         * avail-phys = 1, what remains before this app's memory limit when that\n"
       "         * is less than the phone's free memory (virtual_ios.c). */\n"
       "        extern unsigned long long ios_avail_phys( unsigned long long host_free );\n"
       "        freeram = ios_avail_phys( freeram );\n"
       "    }\n"
       "#endif\n" + ANCHOR)


def patch(src):
    if MARKER in src:
        return src, "already patched"
    if src.count(ANCHOR) != 1:
        raise ValueError("the AvailablePages line was found %d times (expected 1); system.c changed "
                         "upstream, update the patch" % src.count(ANCHOR))
    return src.replace(ANCHOR, NEW), "get_performance_info passes AvailablePages through ios_avail_phys"


def main(path):
    src = open(path).read()
    try:
        out, what = patch(src)
    except ValueError as e:
        sys.stderr.write("patch-wine-avail-phys: %s\n" % e)
        return 1
    if out != src:
        open(path, "w").write(out)
    print("patch-wine-avail-phys:", what, "in", path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__.strip().splitlines()[-1] + "\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
