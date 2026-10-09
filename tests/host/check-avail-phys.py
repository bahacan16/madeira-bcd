#!/usr/bin/env python3
"""Opt-in available physical memory (madeira.cfg avail-phys = 1); no Wine runs.

  - tools/patch-wine-avail-phys.py on a copy of wine/dlls/ntdll/unix/system.c:
    get_performance_info passes freeram through ios_avail_phys right before
    AvailablePages is set (so TotalCommittedPages agrees with it), on Apple
    only; a second run changes nothing; a moved anchor is refused with exit 1
    and the file is left as it was;
  - ios_avail_phys, cut out of build/ntdll-unix/virtual_ios.c and compiled on
    the host with stubs: off (the default) returns the phone's free memory
    untouched and never asks iOS; on, returns min(headroom, phone's free), and
    the phone's figure when iOS answers 0 or a "no limit" sentinel larger than
    the machine; the config is read once; one log line per band change
    (1000 MB / 400 MB, Chromium's levels), at most 64;
  - the workflow patches system.c before ntdll-unix is built and runs this
    test; the catalog lists avail-phys, off by default.
The runtime part needs a host C compiler (CC, cc, gcc or clang).
"""
from pathlib import Path
import importlib.util, os, re, shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
ok = True
spec = importlib.util.spec_from_file_location("patch_avail_phys", root / "tools/patch-wine-avail-phys.py")
patch_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_mod)


def check(what, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + what)
    ok &= bool(cond)


def run(path):
    r = subprocess.run([sys.executable, str(root / "tools/patch-wine-avail-phys.py"), str(path)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


system = root / "wine/dlls/ntdll/unix/system.c"
if not system.exists():
    print("note: %s not checked out; patch checks skipped" % system)
else:
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        c = t / "system.c"
        original = system.read_text()
        if patch_mod.MARKER in original:   # the CI checkout is patched in place
            original = original.replace(patch_mod.NEW, patch_mod.ANCHOR)
        check("pristine system.c reconstructed", patch_mod.MARKER not in original)
        c.write_text(original)
        rc, out = run(c)
        check("patch applies", rc == 0 and "ios_avail_phys" in out)
        src = c.read_text()
        rc, out = run(c)
        check("second run: idempotent, file unchanged", rc == 0 and "already patched" in out and c.read_text() == src)
        fn = src[src.index("static void get_performance_info( SYSTEM_PERFORMANCE_INFORMATION *info )"):]
        fn = fn[:fn.index("\n}\n")]
        call = fn.index("freeram = ios_avail_phys( freeram );")
        check("freeram goes through ios_avail_phys after every host read, right before AvailablePages",
              fn.rindex("freeram =", 0, call) < call and fn.rindex("host_statistics64", 0, call) < call
              and call < fn.index("info->AvailablePages      = freeram / page_size;")
              < fn.index("info->TotalCommittedPages = (totalram + totalswap - freeram - freeswap) / page_size;"))
        block = fn[fn.rindex("#ifdef __APPLE__", 0, call):fn.index("#endif", call)]
        check("Apple only, declared with the 64-bit prototype",
              "extern unsigned long long ios_avail_phys( unsigned long long host_free );" in block)
        check("the patch only adds lines", all(line in src for line in original.splitlines()))
        bad = t / "moved.c"
        bad.write_text(original.replace("info->AvailablePages      = freeram / page_size;",
                                        "info->AvailablePages = freeram / page_size;"))
        before = bad.read_text()
        rc, out = run(bad)
        check("moved anchor: exit 1, file untouched", rc == 1 and bad.read_text() == before and "found 0 times" in out)

# --- ios_avail_phys on the host ---
v = (root / "build/ntdll-unix/virtual_ios.c").read_text()
start = v.index("unsigned long long ios_avail_phys( unsigned long long host_free )")
func = v[start:v.index("\n}\n", start) + 3]
check("ios_avail_phys reads avail-phys once with madeira_cfg_bool, off by default",
      'on = madeira_cfg_bool( "avail-phys", 0 );' in func and "if (on < 0)" in func)
cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
if not cc:
    print("note: no host C compiler; runtime checks skipped")
else:
    harness = r'''
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <stddef.h>
static int cfg_reads, cfg_on, ios_calls, lines;
static size_t ios_left;
static unsigned long long fake_memsize = 12ull << 30;
static char last[256];
static int madeira_cfg_bool(const char *key, int dflt) { cfg_reads++; return strcmp(key, "avail-phys") ? dflt : cfg_on; }
size_t os_proc_available_memory(void) { ios_calls++; return ios_left; }
static int sysctlbyname(const char *n, void *out, size_t *len, void *nv, size_t nl)
{ (void)nv; (void)nl; if (strcmp(n, "hw.memsize") || *len != 8) return -1; memcpy(out, &fake_memsize, 8); return 0; }
static int fake_dprintf(int fd, const char *fmt, ...)
{ va_list a; (void)fd; va_start(a, fmt); vsnprintf(last, sizeof last, fmt, a); va_end(a); lines++; return 0; }
#define dprintf fake_dprintf
''' + func + r'''
#define MB (1ull << 20)
int main(void)
{
    int bad = 0;
#define EXPECT(c, what) do { if (!(c)) { printf("FAIL %s\n", what); bad = 1; } } while (0)
#if OFF
    ios_left = 100 * MB;
    EXPECT(ios_avail_phys(3000 * MB) == 3000 * MB, "off: the phone's free memory, untouched");
    EXPECT(ios_avail_phys(2000 * MB) == 2000 * MB, "off: again");
    EXPECT(ios_calls == 0, "off: iOS is never asked");
    EXPECT(cfg_reads == 1, "off: config read once");
    EXPECT(lines == 0, "off: no log line");
#else
    cfg_on = 1;
    ios_left = 2500 * MB;
    EXPECT(ios_avail_phys(4000 * MB) == 2500 * MB, "on: headroom below the phone's free memory wins");
    EXPECT(lines == 2 && strstr(last, "2500 MB left") && strstr(last, "1000 MB or more"), "on: first band logged");
    ios_left = 5000 * MB;
    EXPECT(ios_avail_phys(4000 * MB) == 4000 * MB, "on: the phone's free memory when it is smaller");
    EXPECT(lines == 2, "on: same band, no line");
    ios_left = 900 * MB;
    EXPECT(ios_avail_phys(4000 * MB) == 900 * MB, "on: 900 MB");
    EXPECT(lines == 3 && strstr(last, "moderate"), "on: moderate band logged");
    ios_left = 300 * MB;
    EXPECT(ios_avail_phys(4000 * MB) == 300 * MB && lines == 4 && strstr(last, "critical"), "on: critical band logged");
    EXPECT(ios_avail_phys(4000 * MB) == 300 * MB && lines == 4, "on: no repeat while in the band");
    ios_left = 0;
    EXPECT(ios_avail_phys(4000 * MB) == 4000 * MB, "on: iOS answers 0 -> the phone's figure");
    ios_left = (size_t)0x7ffffddb00000ull;
    EXPECT(ios_avail_phys(4000 * MB) == 4000 * MB, "on: a no-limit sentinel -> the phone's figure");
    EXPECT(cfg_reads == 1, "on: config read once");
    for (int i = 0; i < 200; i++) { ios_left = (i & 1) ? 300 * MB : 2000 * MB; ios_avail_phys(4000 * MB); }
    EXPECT(lines <= 65, "on: at most 64 band lines");
#endif
    if (!bad) printf("ok\n");
    return bad;
}
'''
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "h.c").write_text(harness)
        for off in (1, 0):
            exe = t / ("off" if off else "on")
            r = subprocess.run([cc, "-std=gnu99", "-Wall", "-Wno-unused-function", "-DOFF=%d" % off,
                                "-o", str(exe), str(t / "h.c")], capture_output=True, text=True)
            if r.returncode:
                print(r.stderr[-2000:])
            out = subprocess.run([str(exe)], capture_output=True, text=True).stdout if exe.exists() else ""
            check("ios_avail_phys runtime, switch %s" % ("off" if off else "on"), r.returncode == 0 and out.strip() == "ok")
            if out.strip() != "ok":
                print(out)

# --- wiring ---
wf = (root / ".github/workflows/build-ipa.yml").read_text()
steps = re.findall(r"\n      - name: (.+)", wf)
p = next((i for i, n in enumerate(steps) if n.startswith("Patch wine with the opt-in available memory")), None)
b = steps.index("Build ntdll-unix") if "Build ntdll-unix" in steps else None
check("workflow patches system.c (and runs this test) before ntdll-unix is built",
      p is not None and b is not None and p < b
      and "python3 tests/host/check-avail-phys.py" in wf
      and "python3 tools/patch-wine-avail-phys.py wine/dlls/ntdll/unix/system.c" in wf)
cat = (root / "app/Madeira/ConfigCatalog.generated.swift").read_text()
check("catalog lists avail-phys, off by default",
      re.search(r'key: "avail-phys".*kind: \.bool, defaultValue: "0"', cat) is not None)

print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
