#!/usr/bin/env python3
"""Video-memory budget-change events for DXMT's DXGI adapter (opt-in at run
time: env.MADEIRA_DXGI_BUDGET_EVENTS = 1).

The budget DXGI reports (QueryVideoMemoryInfo.Budget, from winemetal's
recommendedMaxWorkingSetSize) already shrinks as the app nears its memory
limit (winemetal_unix.c ml1075: from vram-trim-mb below the limit, 1:1 with
the excess). A game only sees that if it asks again. Red Dead Redemption 2
asks when its budget-change event fires: it calls
RegisterVideoMemoryBudgetChangeNotificationEvent, which DXMT answers with
DXGI_ERROR_UNSUPPORTED (a registration whose event never fires hung RDR2
before its window appeared, rdr77). In the owner's run of 2026-10-09 11:02 the
log has one budget query for the whole session; the footprint went from 6349
to 8184 MB in the first minute of play and iOS killed the app.

With the switch, the registration is real:
  - the caller's event is duplicated (so a handle the game closes or reuses
    is never signalled) and signalled at once, as Windows does, so a thread
    waiting for the first notification goes on and reads the budget;
  - a watcher thread (one per process, started by the first registration)
    reads the budget every 500 ms and signals every registered event when it
    has moved by 32 MB or more since the last signal, and again when no
    QueryVideoMemoryInfo followed a signal within 2 s (at most 30 times in a
    row), so a game that reset its event before waiting does not hang;
  - one registry for the process: a cookie is valid on any adapter object;
  - Unregister closes the duplicate.
Without the switch nothing changes (DXGI_ERROR_UNSUPPORTED, or the
madeira-dxgi-budget.txt registration as before).

Log: "[budget-event] registered ...", one line per signal (64, then every
50th) and "[budget-event] the game asked after N signal(s) ..." for the
QueryVideoMemoryInfo that follows a signal (64, then every 50th).

Applied by tools/build-dxgi-dll.sh to a COPY of dxgi_adapter.cpp; the dxmt
submodule is not modified. Idempotent; exits 1 with a message if an anchor is
missing. tests/host/check-dxgi-budget-events.py checks it.
Usage: patch-dxgi-budget-events.py <copy of dxmt/src/dxgi/dxgi_adapter.cpp>
"""
import sys

MARKER = "madeira-bcd: video-memory budget events"

HELPERS = r'''
/* ''' + MARKER + r''' (tools/patch-dxgi-budget-events.py),
 * opt-in with env.MADEIRA_DXGI_BUDGET_EVENTS = 1. The budget below already
 * shrinks near the app's memory limit (winemetal ml1075); these events make a
 * game that waits for them (Red Dead Redemption 2) ask for it again. */
struct MadeiraBudgetEvent {
  DWORD cookie;
  HANDLE handle; /* our duplicate of the caller's event, or the caller's own */
  bool own;
};

struct MadeiraBudgetEvents {
  std::mutex lock;
  std::vector<MadeiraBudgetEvent> events;
  DWORD next_cookie = 0x4d420000; /* apart from the per-adapter cookies */
  obj_handle_t device = 0;        /* retained once, never released */
  uint64_t signalled = 0;         /* the budget the events last announced */
  unsigned signals = 0;
  bool watching = false;
  std::atomic<int> unanswered{0}; /* signals not yet followed by a query */
};

static MadeiraBudgetEvents *madeira_budget() {
  /* Never freed: the watcher thread outlives every adapter object. */
  static MadeiraBudgetEvents *state = new MadeiraBudgetEvents;
  return state;
}

static bool madeira_budget_events_switch() {
  static const bool on = [] {
    char v[8] = {};
    DWORD n = GetEnvironmentVariableA("MADEIRA_DXGI_BUDGET_EVENTS", v, sizeof(v));
    if (n == 0 || n >= sizeof(v))
      return false;
    return !strcmp(v, "1") || !_stricmp(v, "on") || !_stricmp(v, "true") ||
           !_stricmp(v, "yes");
  }();
  return on;
}

static DWORD WINAPI madeira_budget_watch(void *) {
  MadeiraBudgetEvents *s = madeira_budget();
  unsigned quiet = 0, retries = 0;
  for (;;) {
    Sleep(500);
    WMT::Device device;
    device.handle = s->device;
    uint64_t budget = device.recommendedMaxWorkingSetSize();
    std::lock_guard<std::mutex> guard(s->lock);
    if (s->events.empty() || !budget)
      continue;
    uint64_t moved = budget > s->signalled ? budget - s->signalled : s->signalled - budget;
    /* A signal nobody answered with a query within 2 s is sent again (at most
     * 30 times in a row): a game that reset its event before waiting on it
     * would otherwise wait forever, as RDR2 did (rdr77). */
    bool again = false;
    if (s->unanswered.load() == 0)
      quiet = retries = 0;
    else if (++quiet >= 4 && retries < 30)
      again = true;
    if (moved < (32ull << 20) && !again)
      continue;
    quiet = 0;
    retries = moved < (32ull << 20) ? retries + 1 : 0;
    for (auto &e : s->events)
      SetEvent(e.handle);
    unsigned n = ++s->signals;
    s->unanswered.fetch_add(1);
    if (n <= 64 || n % 50 == 0)
      Logger::info(str::format("[budget-event] budget ", budget >> 20, " MB (was ",
                               s->signalled >> 20, " MB), usage ",
                               device.currentAllocatedSize() >> 20, " MB: signalled ",
                               s->events.size(), " event(s), signal #", n,
                               moved < (32ull << 20) ? " (again: no query since the last one)" : ""));
    s->signalled = budget;
  }
  return 0;
}

static HRESULT madeira_budget_register(obj_handle_t device_handle, HANDLE event,
                                       DWORD *cookie) {
  MadeiraBudgetEvents *s = madeira_budget();
  if (!event)
    return E_INVALIDARG;
  HANDLE dup = nullptr;
  if (!DuplicateHandle(GetCurrentProcess(), event, GetCurrentProcess(), &dup, 0,
                       FALSE, DUPLICATE_SAME_ACCESS))
    dup = nullptr;
  WMT::Device device;
  device.handle = device_handle;
  uint64_t budget = device.recommendedMaxWorkingSetSize();
  bool start = false;
  {
    std::lock_guard<std::mutex> guard(s->lock);
    if (!s->device) {
      device.retain();
      s->device = device_handle;
    }
    DWORD assigned = ++s->next_cookie;
    s->events.push_back({assigned, dup ? dup : event, dup != nullptr});
    *cookie = assigned;
    if (!s->signalled)
      s->signalled = budget;
    start = !s->watching;
    s->watching = true;
    /* Windows signals a new registration: the caller may wait for it. */
    SetEvent(dup ? dup : event);
    s->unanswered.fetch_add(1);
    Logger::info(str::format("[budget-event] registered cookie=", assigned,
                             " (MADEIRA_DXGI_BUDGET_EVENTS=1", dup ? "" : ", caller's handle: duplicate failed",
                             "): signalled now; budget ", budget >> 20, " MB, usage ",
                             device.currentAllocatedSize() >> 20,
                             " MB; signalled again when the budget moves by 32 MB"));
  }
  if (start) {
    HMODULE self = nullptr;
    /* The watcher runs this DLL's code for good: keep it loaded. */
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                       (LPCWSTR)(void *)&madeira_budget_watch, &self);
    HANDLE thread = CreateThread(nullptr, 256 * 1024, madeira_budget_watch, nullptr,
                                 STACK_SIZE_PARAM_IS_A_RESERVATION, nullptr);
    if (thread)
      CloseHandle(thread);
    else
      Logger::warn(str::format("[budget-event] the watcher thread did not start (error ",
                               GetLastError(), "): only the first signal is sent"));
  }
  return S_OK;
}

static bool madeira_budget_unregister(DWORD cookie) {
  if (!madeira_budget_events_switch())
    return false;
  MadeiraBudgetEvents *s = madeira_budget();
  std::lock_guard<std::mutex> guard(s->lock);
  for (auto it = s->events.begin(); it != s->events.end(); ++it) {
    if (it->cookie != cookie)
      continue;
    if (it->own)
      CloseHandle(it->handle);
    s->events.erase(it);
    Logger::info(str::format("[budget-event] unregistered cookie=", cookie, ", ",
                             s->events.size(), " left"));
    return true;
  }
  return false;
}

static void madeira_budget_queried(uint64_t budget, uint64_t usage) {
  if (!madeira_budget_events_switch())
    return;
  int pending = madeira_budget()->unanswered.exchange(0);
  if (!pending)
    return;
  static std::atomic<int> said{0};
  int n = said.fetch_add(1);
  if (n < 64 || n % 50 == 0)
    Logger::info(str::format("[budget-event] the game asked after ", pending,
                             " signal(s): budget ", budget >> 20, " MB, usage ",
                             usage >> 20, " MB"));
}
'''

EDITS = [
    ("includes",
     "#include <mutex>\n",
     "#include <mutex>\n#include <atomic> /* madeira-bcd: budget events */\n"),
    ("helpers after the CreateOutput declaration",
     "Com<IDXGIOutput> CreateOutput(IMTLDXGIAdapter *pAadapter, HMONITOR monitor, DxgiOptions &options);\n",
     "Com<IDXGIOutput> CreateOutput(IMTLDXGIAdapter *pAadapter, HMONITOR monitor, DxgiOptions &options);\n"
     + HELPERS),
    ("QueryVideoMemoryInfo",
     "    pVideoMemoryInfo->Budget = device_.recommendedMaxWorkingSetSize();\n"
     "    pVideoMemoryInfo->CurrentUsage = device_.currentAllocatedSize();\n",
     "    pVideoMemoryInfo->Budget = device_.recommendedMaxWorkingSetSize();\n"
     "    pVideoMemoryInfo->CurrentUsage = device_.currentAllocatedSize();\n"
     "    madeira_budget_queried(pVideoMemoryInfo->Budget, pVideoMemoryInfo->CurrentUsage);\n"),
    ("Register",
     "    if (!budget_notify_enabled()) {\n"
     "      Logger::warn(\"DXGI: RegisterVideoMemoryBudgetChangeNotificationEvent \"\n",
     "    if (madeira_budget_events_switch())\n"
     "      return madeira_budget_register(device_.handle, event, cookie);\n"
     "\n"
     "    if (!budget_notify_enabled()) {\n"
     "      Logger::warn(\"DXGI: RegisterVideoMemoryBudgetChangeNotificationEvent \"\n"),
    ("Unregister",
     "  UnregisterVideoMemoryBudgetChangeNotification(DWORD cookie) override {\n"
     "    std::lock_guard<std::mutex> lock(budget_mutex_);\n",
     "  UnregisterVideoMemoryBudgetChangeNotification(DWORD cookie) override {\n"
     "    if (madeira_budget_unregister(cookie))\n"
     "      return;\n"
     "    std::lock_guard<std::mutex> lock(budget_mutex_);\n"),
]


def patch(src):
    if MARKER in src:
        return src, "already patched"
    for name, old, new in EDITS:
        if src.count(old) != 1:
            raise ValueError("anchor for %s found %d times (expected 1); dxgi_adapter.cpp "
                             "changed upstream, update the patch" % (name, src.count(old)))
        src = src.replace(old, new)
    return src, "budget-change events added (MADEIRA_DXGI_BUDGET_EVENTS=1)"


def main(path):
    src = open(path).read()
    try:
        out, what = patch(src)
    except ValueError as e:
        sys.stderr.write("patch-dxgi-budget-events: %s\n" % e)
        return 1
    if out != src:
        open(path, "w").write(out)
    print("patch-dxgi-budget-events:", what, "in", path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__.strip().splitlines()[-1] + "\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
