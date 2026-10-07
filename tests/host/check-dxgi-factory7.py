#!/usr/bin/env python3
"""IDXGIFactory7 for DXMT's factory (tools/patch-dxgi-factory7.py) and the
opt-in source build of dxgi.dll around it; no Wine runs.

Applies the patch to a copy of dxmt/src/dxgi/dxgi_factory.cpp (or
$DXMT_SRC/src/dxgi/dxgi_factory.cpp) and checks: the factory derives from
IDXGIFactory7 and answers it in QueryInterface, RegisterAdaptersChangedEvent /
UnregisterAdaptersChangedEvent exist, EnumAdapterByLuid looks the LUID up, the
"[dxgi-src]" line is logged from CreateDXGIFactory2; a second run changes
nothing; a source whose anchors moved is refused with exit 1 and left as it
was. Then the wiring: tools/build-dxgi-dll.sh patches a copy, compares its
exports with the committed dxgi.dll and ships dxgi-src.dll (never dxgi.dll);
the workflow builds it before any DXMT patch step and before nvapi64 and the
IPA, without failing the run; WineProcessBridge.m links it in only for
env.MADEIRA_DXGI_SRC = 1; the script names stay out of the i386 farm's
tools/patch-dxmt-*.py cache key. With an arm64ec llvm-mingw ($MINGW, or
arm64ec-w64-mingw32-clang++ on PATH) it also compiles the patched file.
"""
from pathlib import Path
import fnmatch, os, re, shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
dxmt = Path(os.environ.get("DXMT_SRC", root / "dxmt"))
factory = dxmt / "src/dxgi/dxgi_factory.cpp"
ok = True


def check(what, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + what)
    ok &= bool(cond)


def run(path):
    r = subprocess.run([sys.executable, str(root / "tools/patch-dxgi-factory7.py"), str(path)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


if not factory.exists():
    print("note: %s not checked out (set DXMT_SRC); patch checks skipped" % factory)
else:
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        cpp = t / "dxgi_factory.cpp"
        shutil.copy(factory, cpp)
        original = cpp.read_text()
        upstream = "MTLDXGIObject<IDXGIFactory7>" in original and "GetAdapterLuid(device)" in original
        rc, out = run(cpp)
        check("patch applies", rc == 0 and "IDXGIFactory7" in out)
        src = cpp.read_text()
        once = src
        rc, out = run(cpp)
        check("second run: idempotent, file unchanged", rc == 0 and cpp.read_text() == once
              and ("already patched" in out or (upstream and "nothing to do" in out and once == original)))

        check("factory derives from IDXGIFactory7", "class MTLDXGIFactory : public MTLDXGIObject<IDXGIFactory7> {" in src
              and "MTLDXGIObject<IDXGIFactory6>" not in src)
        qi = src[src.index("QueryInterface(REFIID riid"):]
        qi = qi[:qi.index("return E_NOINTERFACE;")]
        check("QueryInterface answers IDXGIFactory7 with this", "riid == __uuidof(IDXGIFactory7)" in qi
              and qi.index("riid == __uuidof(IDXGIFactory7)") < qi.index("*ppvObject = ref(this);"))
        check("RegisterAdaptersChangedEvent: S_OK and a nonzero cookie",
              re.search(r"RegisterAdaptersChangedEvent\(HANDLE hEvent,\s+DWORD \*pdwCookie\) override", src) is not None
              and "*pdwCookie = cookie;" in src
              and ("if (cookie == 0)" in src or re.search(r"do\s*\{[^}]*\+\+next_cookie;\s*\}\s*while \(cookie == 0\)", src)))
        check("UnregisterAdaptersChangedEvent present", "UnregisterAdaptersChangedEvent(DWORD dwCookie) override" in src)
        # The two IDXGIFactory7 methods are the last virtual functions declared, after
        # IDXGIFactory6's, so they take the vtable slots the interface defines.
        cls = src[src.index("class MTLDXGIFactory"):src.index("private:\n  UINT flags_;")]
        check("Factory7 methods declared after EnumAdapterByGpuPreference",
              cls.index("EnumAdapterByGpuPreference") < cls.index("RegisterAdaptersChangedEvent")
              < cls.index("UnregisterAdaptersChangedEvent"))
        lu = src[src.index("EnumAdapterByLuid(LUID luid"):]
        lu = lu[:lu.index("\n  }\n")]
        check("EnumAdapterByLuid matches GetAdapterLuid and creates the adapter",
              "GetAdapterLuid(device)" in lu and "CreateAdapter(device, this" in lu and "QueryInterface(iid, adapter)" in lu
              and "not implemented" not in lu)
        cf = src[src.index('extern "C" HRESULT __stdcall CreateDXGIFactory2'):]
        if upstream:
            check("upstream Factory7 implementation is left byte-for-byte unchanged", src == original)
        else:
            check("CreateDXGIFactory2 logs the [dxgi-src] line once",
                  "madeira_dxgi_src_note(riid);" in cf[:300] and "[dxgi-src] madeira-bcd dxgi.dll from DXMT source" in src
                  and "noted.exchange(true)" in src)

        bad = t / "moved.cpp"
        bad.write_text(once.replace("madeira-bcd: IDXGIFactory7", "x")
                       .replace("MTLDXGIObject<IDXGIFactory7>", "MTLDXGIObject<IDXGIFactory5>"))
        before = bad.read_text()
        rc, out = run(bad)
        check("moved anchors: exit 1, file untouched, anchor named", rc == 1 and bad.read_text() == before and "anchor for" in out)

        mingw = os.environ.get("MINGW")
        cxx = Path(mingw) / "arm64ec-w64-mingw32-clang++" if mingw else shutil.which("arm64ec-w64-mingw32-clang++")
        if not cxx or not Path(cxx).exists():
            print("note: no arm64ec llvm-mingw ($MINGW); compile step skipped")
        else:
            u, g = dxmt / "src/util", dxmt / "src/dxgi"
            r = subprocess.run([str(cxx), "-std=c++20", "-O2", "-c", "-o", str(t / "f.o"), str(cpp),
                                "-I%s" % g, "-I%s" % (dxmt / "src/dxmt"), "-I%s" % u, "-I%s" % (dxmt / "src/winemetal"),
                                "-I%s" % (dxmt / "src/airconv"), "-I%s" % (dxmt / "include"), "-I%s" % (dxmt / "libs"),
                                "-DNOMINMAX", "-D_WIN32_WINNT=0xa00", "-DDXMT_IOS=1", "-DDXMT_PAGE_SIZE=4096",
                                "-DMADEIRA_DXGI_SRC_REV=\"test\"", "-fblocks", "-Wno-microsoft-exception-spec"],
                               capture_output=True, text=True)
            check("patched dxgi_factory.cpp compiles for arm64ec", r.returncode == 0 and (t / "f.o").exists())
            if r.returncode:
                print(r.stderr[-2000:])

# --- the build script ---
sh = (root / "tools/build-dxgi-dll.sh").read_text()
check("build script patches a copy, never the submodule",
      'cp "$G/dxgi_factory.cpp" "$OUT/src/dxgi_factory.cpp"' in sh
      and 'tools/patch-dxgi-factory7.py" "$OUT/src/dxgi_factory.cpp"' in sh)
check("build script compares its exports with the committed dxgi.dll",
      'exports "$SHIP/dxgi.dll" > "$OUT/exports.upstream"' in sh and 'diff "$OUT/exports.upstream" "$OUT/exports.built"' in sh)
check("build script checks the unpatched build against upstream's binary",
      '"$NM" --defined-only "$SHIP/dxgi.dll"' in sh and 'link_dll "$OUT/obj/dxgi_factory_plain.o"' in sh)
check("build script ships dxgi-src.dll and never overwrites dxgi.dll",
      'DEST="$SHIP/dxgi-src.dll"' in sh and not re.search(r'(cp|mv)[^\n]*"\$SHIP/dxgi\.dll"', sh))
check("build script failures are warnings that leave the bundle alone",
      "::warning::dxgi-src.dll NOT built" in sh and sh.index("fail()") < sh.index('mv -f "$DEST.tmp" "$DEST"'))

# --- the workflow ---
wf = (root / ".github/workflows/build-ipa.yml").read_text()
steps = re.findall(r"\n      - name: (.+)", wf)
idx = {n: i for i, n in enumerate(steps)}
b = next((i for i, n in enumerate(steps) if n.startswith("Build dxgi-src.dll")), None)
package = next((i for i, n in enumerate(steps) if n.startswith("Package ") and "unsigned IPA" in n), None)
check("workflow has the dxgi-src.dll step", b is not None)
check("workflow has an unsigned IPA packaging step", package is not None)
if b is not None:
    first_dxmt_patch = min(i for i, n in enumerate(steps) if n.startswith("Patch DXMT") or n.startswith("Patch winemetal")
                           or n.startswith("Patch airconv"))
    check("it runs after llvm-mingw and before every DXMT patch step",
          idx["Install ninja/meson and fetch llvm-mingw"] < b < first_dxmt_patch)
    check("it runs before nvapi64 derives its import lib and before the IPA is packaged",
          package is not None and b < idx["Build DXMT nvapi64.dll"] and b < idx["Verify committed PE DLLs"] < package)
    block = wf[wf.index("- name: " + steps[b]):]
    block = block[:block.index("\n      - name:", 10)]
    check("its failure does not fail the run", "continue-on-error: true" in block and "bash tools/build-dxgi-dll.sh" in block)
check("i386 farm cache key still uses tools/patch-dxmt-*.py, and the new scripts are not in it",
      "'tools/patch-dxmt-*.py'" in wf
      and not fnmatch.fnmatch("tools/patch-dxgi-factory7.py", "tools/patch-dxmt-*.py")
      and not fnmatch.fnmatch("tools/build-dxgi-dll.sh", "tools/patch-dxmt-*.py"))

# --- the app ---
m = (root / "app/Madeira/WineProcessBridge.m").read_text()
blk = m[m.index('getenv("MADEIRA_DXGI_SRC")') - 200:]
blk = blk[:blk.index("upstream's dxgi.dll stays")]
check("WineProcessBridge links dxgi-src.dll as dxgi.dll only for MADEIRA_DXGI_SRC=1",
      "dxgiSrc && dxgiSrc[0] == '1'" in blk and '@"dxgi-src.dll"' in blk
      and 'stringByAppendingPathComponent:@"dxgi.dll"' in blk and "drive_c/windows/sysx64" in blk
      and "if (use_arm64ec) [dirs addObject:sys32Dir];" in blk)
check("the switch block comes after the farms are relinked from the bundle",
      m.index('{ "sysx64",  "arm64ec-windows" }') < m.index('getenv("MADEIRA_DXGI_SRC")'))
gi = (root / ".gitignore").read_text()
check(".gitignore keeps the built DLL out of git", "app/Madeira/arm64ec-windows/dxgi-src.dll" in gi and "build/dxmt-dxgi/" in gi)
cat = (root / "app/Madeira/ConfigCatalog.generated.swift").read_text()
check("catalog lists env.MADEIRA_DXGI_SRC, off by default",
      re.search(r'key: "env.MADEIRA_DXGI_SRC".*kind: \.bool, defaultValue: "0"', cat) is not None)

print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
