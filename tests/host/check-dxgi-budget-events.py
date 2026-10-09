#!/usr/bin/env python3
"""Video-memory budget-change events for DXMT's DXGI adapter
(tools/patch-dxgi-budget-events.py, env.MADEIRA_DXGI_BUDGET_EVENTS = 1); no
Wine runs.

Applies the patch to a copy of dxmt/src/dxgi/dxgi_adapter.cpp (or
$DXMT_SRC/src/dxgi/dxgi_adapter.cpp) and checks:
  - with the switch, Register signals the caller's (duplicated) event at once
    and starts one watcher thread, which signals every registered event when
    the budget moves by 32 MB; Unregister closes the duplicate; one registry
    for the process; QueryVideoMemoryInfo notes the query after a signal;
  - without the switch the old paths run unchanged (DXGI_ERROR_UNSUPPORTED, or
    madeira-dxgi-budget.txt's registration), and the switch is read with
    GetEnvironmentVariableA;
  - a second run changes nothing; moved anchors are refused with exit 1 and the
    file is left as it was;
  - tools/build-dxgi-dll.sh patches the copy after the UMD version patch and
    fails the build when the patch is missing from the result; the catalog
    lists the switch, off by default.
With an arm64ec llvm-mingw ($MINGW, or arm64ec-w64-mingw32-clang++ on PATH) it
also compiles the patched file.
"""
from pathlib import Path
import os, re, shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
dxmt = Path(os.environ.get("DXMT_SRC", root / "dxmt"))
adapter = dxmt / "src/dxgi/dxgi_adapter.cpp"
ok = True


def check(what, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + what)
    ok &= bool(cond)


def run(path):
    r = subprocess.run([sys.executable, str(root / "tools/patch-dxgi-budget-events.py"), str(path)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def body(src, start, end="\n  }\n"):
    i = src.index(start)
    return src[i:src.index(end, i)]


if not adapter.exists():
    print("note: %s not checked out (set DXMT_SRC); patch checks skipped" % adapter)
else:
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        cpp = t / "dxgi_adapter.cpp"
        shutil.copy(adapter, cpp)
        original = cpp.read_text()
        rc, out = run(cpp)
        check("patch applies", rc == 0 and "budget-change events added" in out)
        src = cpp.read_text()
        rc, out = run(cpp)
        check("second run: idempotent, file unchanged", rc == 0 and "already patched" in out and cpp.read_text() == src)

        check("helpers are inside namespace dxmt, before the adapter class",
              src.index("namespace dxmt {") < src.index("struct MadeiraBudgetEvents {")
              < src.index("class MTLDXGIAdatper"))
        check("switch: GetEnvironmentVariableA(MADEIRA_DXGI_BUDGET_EVENTS), read once",
              'GetEnvironmentVariableA("MADEIRA_DXGI_BUDGET_EVENTS"' in src
              and "static const bool on = [] {" in body(src, "static bool madeira_budget_events_switch()", "\n}\n"))

        reg = body(src, "RegisterVideoMemoryBudgetChangeNotificationEvent(\n      HANDLE event, DWORD *cookie) override {")
        check("Register: switch first, after the cookie check, before the old file switch",
              reg.index("if (!cookie)") < reg.index("if (madeira_budget_events_switch())")
              < reg.index("if (!budget_notify_enabled())")
              and "return madeira_budget_register(device_.handle, event, cookie);" in reg)
        check("Register without the switch: the old DXGI_ERROR_UNSUPPORTED path is intact",
              "return DXGI_ERROR_UNSUPPORTED;" in reg and "budget_events_.emplace_back(assigned, event);" in reg)

        r = body(src, "static HRESULT madeira_budget_register(", "\n}\n")
        check("register: the event is duplicated, the duplicate (else the caller's) stored and signalled",
              "DuplicateHandle(GetCurrentProcess(), event, GetCurrentProcess(), &dup" in r
              and "DUPLICATE_SAME_ACCESS" in r and "SetEvent(dup ? dup : event);" in r
              and "s->events.push_back({assigned, dup ? dup : event, dup != nullptr});" in r)
        check("register: a NULL event is refused", "if (!event)\n    return E_INVALIDARG;" in r)
        check("register: cookie assigned under the lock, never 0",
              r.index("std::lock_guard<std::mutex> guard(s->lock);") < r.index("DWORD assigned = ++s->next_cookie;")
              and "DWORD next_cookie = 0x4d420000;" in src and "*cookie = assigned;" in r)
        check("register: one watcher per process, started outside the lock, DLL pinned",
              "start = !s->watching;" in r and "s->watching = true;" in r
              and "\n  }\n  if (start) {" in r
              and "GET_MODULE_HANDLE_EX_FLAG_PIN" in r and "CreateThread(nullptr, 256 * 1024, madeira_budget_watch" in r)
        check("register: the device is retained once and kept", "device.retain();" in r and "s->device = device_handle;" in r)

        w = body(src, "static DWORD WINAPI madeira_budget_watch(void *) {", "\n}\n")
        check("watcher: polls every 500 ms, outside the lock",
              "Sleep(500);" in w and w.index("recommendedMaxWorkingSetSize()") < w.index("std::lock_guard<std::mutex> guard(s->lock);"))
        check("watcher: signals all events when the budget moved by 32 MB, then records it",
              "if (moved < (32ull << 20) && !again)\n      continue;" in w
              and "for (auto &e : s->events)\n      SetEvent(e.handle);" in w
              and w.index("SetEvent(e.handle)") < w.index("s->signalled = budget;"))
        check("watcher: an unanswered signal is sent again after 2 s, at most 30 times in a row",
              "if (s->unanswered.load() == 0)\n      quiet = retries = 0;" in w
              and "else if (++quiet >= 4 && retries < 30)\n      again = true;" in w
              and "retries = moved < (32ull << 20) ? retries + 1 : 0;" in w
              and w.index("again = true;") < w.index("SetEvent(e.handle)"))
        check("watcher: nothing to do without events or a budget", "if (s->events.empty() || !budget)\n      continue;" in w)
        check("watcher: its log is capped (64, then every 50th)", "if (n <= 64 || n % 50 == 0)" in w)

        u = body(src, "UnregisterVideoMemoryBudgetChangeNotification(DWORD cookie) override {")
        check("Unregister: the process registry first, then the old per-adapter list",
              u.index("if (madeira_budget_unregister(cookie))") < u.index("std::lock_guard<std::mutex> lock(budget_mutex_);"))
        un = body(src, "static bool madeira_budget_unregister(DWORD cookie) {", "\n}\n")
        check("unregister: only with the switch; closes only a duplicate it made",
              "if (!madeira_budget_events_switch())\n    return false;" in un
              and "if (it->own)\n      CloseHandle(it->handle);" in un and "s->events.erase(it);" in un)

        q = body(src, "HRESULT STDMETHODCALLTYPE QueryVideoMemoryInfo(")
        check("QueryVideoMemoryInfo still reports the winemetal budget and usage, then notes the query",
              q.index("pVideoMemoryInfo->Budget = device_.recommendedMaxWorkingSetSize();")
              < q.index("madeira_budget_queried(pVideoMemoryInfo->Budget, pVideoMemoryInfo->CurrentUsage);"))
        check("the query note does nothing without the switch",
              "if (!madeira_budget_events_switch())\n    return;" in body(src, "static void madeira_budget_queried(", "\n}\n"))
        check("the patch only adds: every original line is still there",
              all(line in src for line in original.splitlines()))

        bad = t / "moved.cpp"
        bad.write_text(original.replace("  UnregisterVideoMemoryBudgetChangeNotification(DWORD cookie) override {",
                                        "  UnregisterVideoMemoryBudgetChangeNotification(DWORD c) override {"))
        before = bad.read_text()
        rc, out = run(bad)
        check("moved anchors: exit 1, file untouched, anchor named",
              rc == 1 and bad.read_text() == before and "anchor for Unregister" in out)

        mingw = os.environ.get("MINGW")
        cxx = Path(mingw) / "arm64ec-w64-mingw32-clang++" if mingw else shutil.which("arm64ec-w64-mingw32-clang++")
        if not cxx or not Path(cxx).exists():
            print("note: no arm64ec llvm-mingw ($MINGW); compile step skipped")
        else:
            u_, g = dxmt / "src/util", dxmt / "src/dxgi"
            r = subprocess.run([str(cxx), "-std=c++20", "-O2", "-c", "-o", str(t / "a.o"), str(cpp),
                                "-I%s" % g, "-I%s" % (dxmt / "src/dxmt"), "-I%s" % u_, "-I%s" % (dxmt / "src/winemetal"),
                                "-I%s" % (dxmt / "src/airconv"), "-I%s" % (dxmt / "include"), "-I%s" % (dxmt / "libs"),
                                "-I%s" % (root / "madeira-d3d12/src/pe"),
                                "-DNOMINMAX", "-D_WIN32_WINNT=0xa00", "-DDXMT_IOS=1", "-DDXMT_PAGE_SIZE=4096",
                                "-fblocks", "-Wno-microsoft-exception-spec", "-Werror=return-type"],
                               capture_output=True, text=True)
            check("patched dxgi_adapter.cpp compiles for arm64ec", r.returncode == 0 and (t / "a.o").exists())
            if r.returncode:
                print(r.stderr[-2000:])

# --- the helpers on the host: register, watcher, query note, unregister ---
spec = __import__("importlib.util").util.spec_from_file_location("patch_budget", root / "tools/patch-dxgi-budget-events.py")
patch_mod = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(patch_mod)
prelude = r'''
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <sstream>
#include <string>
#include <utility>
#include <vector>
#include <strings.h>
typedef unsigned DWORD; typedef void *HANDLE; typedef long HRESULT; typedef void *HMODULE; typedef const wchar_t *LPCWSTR;
typedef uint64_t obj_handle_t; typedef DWORD (*LPTHREAD_START_ROUTINE)(void *);
#define WINAPI
#define FALSE 0
#define S_OK 0L
#define E_INVALIDARG ((HRESULT)0x80070057L)
#define DUPLICATE_SAME_ACCESS 2
#define GET_MODULE_HANDLE_EX_FLAG_PIN 1
#define GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS 4
#define STACK_SIZE_PARAM_IS_A_RESERVATION 0x10000
#define _stricmp strcasecmp
struct Stop {};
static std::vector<std::string> log_lines;
static std::vector<HANDLE> set_events, closed;
static int threads, pins, dup_fail, ticks, stop_at;
static uint64_t g_budget = 2304ull << 20, g_usage = 2000ull << 20;
static void (*on_tick)(int);
static const char *env_value = "1";
static DWORD GetEnvironmentVariableA(const char *n, char *v, DWORD sz) {
  if (strcmp(n, "MADEIRA_DXGI_BUDGET_EVENTS") || !env_value) return 0;
  DWORD l = (DWORD)strlen(env_value); if (l >= sz) return l + 1; memcpy(v, env_value, l + 1); return l; }
static HANDLE GetCurrentProcess() { return (HANDLE)-1; }
static int DuplicateHandle(HANDLE, HANDLE src, HANDLE, HANDLE *dst, DWORD, int, DWORD) {
  if (dup_fail) return 0; *dst = (HANDLE)((uintptr_t)src + 0x1000); return 1; }
static int SetEvent(HANDLE h) { set_events.push_back(h); return 1; }
static int CloseHandle(HANDLE h) { closed.push_back(h); return 1; }
static DWORD GetLastError() { return 0; }
static int GetModuleHandleExW(DWORD f, LPCWSTR, HMODULE *m) { if (f & GET_MODULE_HANDLE_EX_FLAG_PIN) pins++; *m = (HMODULE)1; return 1; }
static HANDLE CreateThread(void *, size_t, LPTHREAD_START_ROUTINE, void *, DWORD, DWORD *) { threads++; return (HANDLE)0x77; }
static void Sleep(DWORD) { ++ticks; if (on_tick) on_tick(ticks); if (ticks >= stop_at) throw Stop(); }
namespace WMT { struct Device { obj_handle_t handle = 0; int retained = 0;
  uint64_t recommendedMaxWorkingSetSize() const { return g_budget; } uint64_t currentAllocatedSize() const { return g_usage; }
  void retain() { retained++; } }; }
namespace dxmt {
namespace str { template <typename... A> std::string format(const A &...a) { std::ostringstream o; (o << ... << a); return o.str(); } }
struct Logger { static void info(const std::string &m) { log_lines.push_back(m); } static void warn(const std::string &m) { log_lines.push_back(m); } };
'''
runtime = r'''
} // namespace dxmt
using namespace dxmt;
static int bad;
#define EXPECT(c, what) do { if (!(c)) { printf("FAIL %s\n", what); bad = 1; } } while (0)
static size_t sets_since; static int queried_at = -1, unreg_at = -1, change_at = -1, small_at = -1; static DWORD cookie1;
static void tick(int t) {
  if (t == queried_at) madeira_budget_queried(g_budget, g_usage);
  if (t == change_at) g_budget -= 64ull << 20;
  if (t == small_at) g_budget -= 16ull << 20;
  if (t == unreg_at) madeira_budget_unregister(cookie1);
}
static void run(int n) { ticks = 0; stop_at = n; try { madeira_budget_watch(nullptr); } catch (Stop &) {} }
int main() {
  DWORD c1 = 0, c2 = 0;
  EXPECT(madeira_budget_events_switch(), "switch reads 1");
  EXPECT(madeira_budget_register(0xd1, nullptr, &c1) == E_INVALIDARG, "NULL event refused");
  EXPECT(madeira_budget_register(0xd1, (HANDLE)0x40, &c1) == S_OK && c1 > 0x4d420000, "registered with a cookie");
  EXPECT(set_events.size() == 1 && set_events[0] == (HANDLE)0x1040, "the duplicate is signalled at once");
  EXPECT(threads == 1 && pins == 1, "one watcher, DLL pinned");
  dup_fail = 1;
  EXPECT(madeira_budget_register(0xd1, (HANDLE)0x50, &c2) == S_OK && c2 == c1 + 1, "second registration");
  dup_fail = 0;
  EXPECT(set_events.size() == 2 && set_events[1] == (HANDLE)0x50, "without a duplicate, the caller's handle");
  EXPECT(threads == 1, "still one watcher");
  cookie1 = c1; on_tick = tick;
  /* nobody queried after the registrations: sent again at tick 4 (2 s), then 4 ticks later */
  sets_since = set_events.size(); run(9);
  EXPECT(set_events.size() == sets_since + 4, "unanswered signal repeated every 2 s to both events");
  /* a query answers it: no more repeats while the budget stays */
  queried_at = 10; sets_since = set_events.size(); ticks = 9; stop_at = 20;
  try { for (;;) { madeira_budget_watch(nullptr); } } catch (Stop &) {}
  EXPECT(set_events.size() == sets_since, "answered and unchanged: quiet");
  /* a 16 MB move is not enough; a 64 MB one signals both events */
  queried_at = -1; small_at = 2; change_at = -1; sets_since = set_events.size(); run(3);
  EXPECT(set_events.size() == sets_since, "16 MB: no signal");
  change_at = 1; small_at = -1; sets_since = set_events.size(); run(2);
  EXPECT(set_events.size() == sets_since + 2, "64 MB less: both events signalled");
  EXPECT(!log_lines.empty() && log_lines.back().find("[budget-event] budget 2224 MB (was 2304 MB)") == 0, "the signal is logged");
  /* the game asks: the note names the signals, once */
  size_t lines = log_lines.size();
  madeira_budget_queried(g_budget, g_usage);
  EXPECT(log_lines.size() == lines + 1 && log_lines.back().find("[budget-event] the game asked after 1 signal(s)") == 0, "query noted");
  madeira_budget_queried(g_budget, g_usage);
  EXPECT(log_lines.size() == lines + 1, "a query without a new signal is not noted");
  /* retries stop after 30 in a row without a query */
  change_at = -1; g_budget -= 64ull << 20; run(2);
  sets_since = set_events.size(); run(4 * 40);
  EXPECT(set_events.size() == sets_since + 2 * 30, "at most 30 repeats in a row");
  /* unregister closes our duplicate only; the watcher then signals the other one */
  closed.clear();
  EXPECT(madeira_budget_unregister(c1) && closed.size() == 1 && closed[0] == (HANDLE)0x1040, "unregister closes the duplicate");
  EXPECT(!madeira_budget_unregister(c1), "a cookie unregisters once");
  EXPECT(madeira_budget_unregister(c2) && closed.size() == 1, "the caller's own handle is not closed");
  sets_since = set_events.size(); g_budget -= 64ull << 20; run(2);
  EXPECT(set_events.size() == sets_since, "no events: nothing signalled");
  if (!bad) printf("ok\n");
  return bad;
}
'''
cxx = shutil.which("clang++") or shutil.which("g++")
if not cxx:
    print("note: no host C++ compiler; runtime checks skipped")
else:
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "w.cpp").write_text(prelude + patch_mod.HELPERS + runtime)
        exe = t / "w"
        r = subprocess.run([cxx, "-std=c++20", "-Wall", "-Wno-unused-function", "-o", str(exe), str(t / "w.cpp")],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-3000:])
        out = subprocess.run([str(exe)], capture_output=True, text=True).stdout if exe.exists() else ""
        check("helpers on the host: register, repeat until queried, 32 MB moves, 30-repeat cap, unregister",
              r.returncode == 0 and out.strip() == "ok")
        if out.strip() != "ok":
            print(out[:3000])

# --- the build script ---
sh = (root / "tools/build-dxgi-dll.sh").read_text()
check("build script patches the adapter copy after the UMD version patch",
      'cp "$G/dxgi_adapter.cpp" "$OUT/src/dxgi_adapter.cpp"' in sh
      and sh.index('tools/patch-dxgi-umd-version.py" "$OUT/src/dxgi_adapter.cpp"')
      < sh.index('tools/patch-dxgi-budget-events.py" "$OUT/src/dxgi_adapter.cpp"'))
check("build script: a patch that does not apply only warns; dxgi-src.dll is still built for every game",
      '::warning::tools/patch-dxgi-budget-events.py did not apply' in sh
      and re.search(r'patch-dxgi-budget-events\.py" "\$OUT/src/dxgi_adapter\.cpp" \|\| \{\n    BUDGET_EVENTS=""', sh) is not None)
check("build script refuses a result that lacks the patch it applied",
      'if [ -n "$BUDGET_EVENTS" ]; then\n    LC_ALL=C grep -aq "\\[budget-event\\] registered cookie=" "$OUT/dxgi.dll" || fail' in sh)

# --- the catalog ---
cat = (root / "app/Madeira/ConfigCatalog.generated.swift").read_text()
check("catalog lists env.MADEIRA_DXGI_BUDGET_EVENTS, off by default",
      re.search(r'key: "env.MADEIRA_DXGI_BUDGET_EVENTS".*kind: \.bool, defaultValue: "0"', cat) is not None)

print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
