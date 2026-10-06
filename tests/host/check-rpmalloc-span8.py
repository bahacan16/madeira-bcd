#!/usr/bin/env python3
"""Exercise real pinned rpmalloc; contrast the old and new arena geometry."""
from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
source = root / "FEX/External/rpmalloc/rpmalloc/rpmalloc.c"
assert source.is_file(), "initialize FEX/External/rpmalloc at its pinned revision first"
spec = importlib.util.spec_from_file_location("span8", root / "tools/patch-fex-ios-rpmalloc-span8.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = source.read_text()
patched = module.patch(original)
assert module.patch(patched) == patched
span4 = module.patch(original, 4)
assert module.patch(span4, 4) == span4
for corrupt in (
    original.replace("#define LARGE_PAGE_SIZE_SHIFT 24", "#define LARGE_PAGE_SIZE_SHIFT 25"),
    patched.replace("#define LARGE_SIZE_CLASS_COUNT 19", "#define LARGE_SIZE_CLASS_COUNT 20"),
    patched.replace("#define SPAN_SIZE (8 * 1024 * 1024)", "#define SPAN_SIZE (4 * 1024 * 1024)"),
):
    try:
        module.patch(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("changed or partial overlay must fail before writing")
for corrupt in (
    span4.replace('LARGE_SIZE_CLASS_COUNT 15', 'LARGE_SIZE_CLASS_COUNT 19'),
    span4.replace('LARGE_PAGE_SIZE_SHIFT 22', 'LARGE_PAGE_SIZE_SHIFT 23'),
    span4.replace('(7 * 512 * 1024)', '(4 * 1024 * 1024)'),
    patched,
):
    try:
        module.patch(corrupt, 4)
    except ValueError:
        pass
    else:
        raise AssertionError('partial or different span4 overlay accepted')
try:
    module.patch(original, 2)
except ValueError:
    pass
else:
    raise AssertionError('unsupported span size accepted')

build = (root / "tools/build-xtajit64.sh").read_text()
apply = build.index('python3 "$R/tools/patch-fex-ios-rpmalloc-span8.py"')
rebuild = build.index("\nbuild\n", apply)
restore = build.index("git -C FEX/External/rpmalloc checkout -- rpmalloc/rpmalloc.c", rebuild)
assert build.index("=== unpatched rebuild") < apply < rebuild < restore < build.index('cp "$B/Bin/libarm64ecfex.dll"')
assert '--span-mb 4' in build[apply:rebuild]
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "if b'rpmalloc-span4 madeira-bcd' not in module:" in workflow
assert 'python3 tests/host/check-rpmalloc-span8.py' in workflow

with tempfile.TemporaryDirectory(prefix="rpmalloc-span8-") as directory:
    temp = Path(directory)
    for name in ("rpmalloc.h", "malloc.c"):
        (temp / name).write_bytes(source.with_name(name).read_bytes())
    # Apple's compiler-rt may not support LeakSanitizer. ASan/UBSan and the
    # allocator's own zero-live-mappings assertion still run on every host.
    leaks = '0' if sys.platform == 'darwin' else '1'
    env = dict(os.environ, ASAN_OPTIONS=f"detect_leaks={leaks}", UBSAN_OPTIONS="halt_on_error=1")
    cc = os.environ.get("CC", "cc")
    for name, text in (("original", original), ("span8", patched), ("span4", span4)):
        unit = temp / "rpmalloc.c"
        unit.write_text(text)
        # The ordinary platform's spans, large pages and class ceiling remain
        # identical. Preprocessing also validates both sides of the new guards.
        macros = subprocess.check_output([cc, "-E", "-dM", "-I", str(temp), str(unit)], text=True)
        for definition in (
            "#define SPAN_SIZE (256 * 1024 * 1024)",
            "#define LARGE_PAGE_SIZE_SHIFT 26",
            "#define LARGE_SIZE_CLASS_COUNT 20",
            "#define LARGE_BLOCK_SIZE_LIMIT (8 * 1024 * 1024)",
        ):
            assert definition in macros, definition
        binary = temp / name
        subprocess.run([
            cc, "-std=gnu11", "-O1", "-g", "-DFEX_IOS_HOST",
            "-DRPMALLOC_FIRST_CLASS_HEAPS=1", "-DENABLE_DECOMMIT=1", "-DENABLE_OVERRIDE=0",
            "-DENABLE_ASSERTS=1", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-Wno-unused-function", "-I", str(temp),
            str(root / "tests/host/rpmalloc-span8-fixture.c"), "-pthread", "-o", str(binary),
        ], check=True)
        mode = str({'original': 0, 'span8': 1, 'span4': 2}[name])
        subprocess.run([str(binary), "12", mode, "geometry"], env=env, check=True)
        subprocess.run([str(binary), "12", mode, "pressure"], env=env, check=True)
        if name != "original":
            subprocess.run([str(binary), "12", mode, "late_pressure"], env=env, check=True)
            for gib in ("4", "8", "16"):
                subprocess.run([str(binary), gib, mode, "geometry"], env=env, check=True)
        # Exercise macOS's fresh-mapping/alignment path and Apple Silicon's
        # 16 KiB allocation-page size on Linux too, including the old baseline.
        portable = temp / f'{name}-portable-16k'
        subprocess.run([
            cc, '-std=gnu11', '-O1', '-g', '-DFEX_IOS_HOST', '-DRPMALLOC_TEST_PORTABLE_MAP',
            '-DRPMALLOC_TEST_PAGE_SIZE=16384',
            '-DRPMALLOC_FIRST_CLASS_HEAPS=1', '-DENABLE_DECOMMIT=1', '-DENABLE_OVERRIDE=0',
            '-DENABLE_ASSERTS=1', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
            '-Wno-unused-function', '-I', str(temp),
            str(root / 'tests/host/rpmalloc-span8-fixture.c'), '-pthread', '-o', str(portable),
        ], check=True)
        subprocess.run([str(portable), '12', mode, 'geometry'], env=env, check=True)
    assert source.read_text() == original, "host tests must not edit the pinned submodule"

print("PASS: strict/idempotent overlay, actual allocator boundaries and arena pressure, non-iOS geometry preserved")
