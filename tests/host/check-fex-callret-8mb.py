#!/usr/bin/env python3
"""Check tools/patch-fex-ios-callret-8mb.py against the pinned FEX tree.

Works on a copy: the overlay applies, a second run is a no-op, the JIT window
it relies on is still the [2 MiB, 6 MiB) one, no SIZE/4 default is left, a
drifted anchor is refused, and build-xtajit64.sh restores every file it edits.
"""
from pathlib import Path
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
tool = root / 'tools/patch-fex-ios-callret-8mb.py'
spec = importlib.util.spec_from_file_location('overlay', tool)
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)

files = set(overlay.EDITS) | {rel for rel, _, _ in overlay.WINDOW_CHECKS}


def copy_tree(dst):
    for rel in files:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / 'FEX' / rel, dst / rel)


def run(dst):
    return subprocess.run([sys.executable, str(tool), str(dst)], capture_output=True, text=True)


with tempfile.TemporaryDirectory() as tmp:
    fex = Path(tmp) / 'FEX'
    copy_tree(fex)
    r = run(fex)
    assert r.returncode == 0, r.stderr
    first = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
    r = run(fex)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count('already patched') == len(overlay.EDITS), r.stdout
    assert first == {rel: (fex / rel).read_text() for rel in overlay.EDITS}

    h = first['FEXCore/include/FEXCore/Debug/InternalThreadState.h']
    size = int(re.search(r'CALLRET_STACK_SIZE \{(0x[0-9a-f]+)\}', h).group(1), 16)
    default = int(re.search(r'CALLRET_DEFAULT_OFFSET \{(0x[0-9a-f]+)\}', h).group(1), 16)
    live = int(re.search(r'CALLRET_LIVE_OFFSET \{(0x[0-9a-f]+)\}', h).group(1), 16)
    live_size = int(re.search(r'CALLRET_LIVE_SIZE \{(0x[0-9a-f]+)\}', h).group(1), 16)
    assert size == 8 << 20 and default == 4 << 20 and live == 2 << 20 and live_size == 4 << 20
    assert live + live_size <= size and live <= default < live + live_size
    d = first['FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp']
    assert 'lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 23);' in d
    assert 1 << 23 == size
    for rel in ('FEXCore/Source/Interface/Core/Core.cpp', 'Source/Windows/Common/CallRetStack.h'):
        assert 'CALLRET_STACK_SIZE / 4' not in first[rel], rel

with tempfile.TemporaryDirectory() as tmp:
    fex = Path(tmp) / 'FEX'
    copy_tree(fex)
    p = fex / 'FEXCore/Source/Interface/Core/JIT/BranchOps.cpp'
    p.write_text(p.read_text().replace('TMP1, TMP1, 22);', 'TMP1, TMP1, 23);'))
    assert run(fex).returncode != 0, 'a moved JIT window was accepted'

with tempfile.TemporaryDirectory() as tmp:
    fex = Path(tmp) / 'FEX'
    copy_tree(fex)
    p = fex / 'FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp'
    p.write_text(p.read_text().replace('lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 24);', 'changed-anchor'))
    assert run(fex).returncode != 0, 'a drifted dispatcher anchor was accepted'

script = (root / 'tools/build-xtajit64.sh').read_text()
apply_at = script.index('patch-fex-ios-callret-8mb.py" "$R/FEX"')
restore = next(line for line in script.splitlines() if line.startswith('git -C FEX checkout -- "$CPUF"'))
assert apply_at < script.index(restore)
for rel in overlay.EDITS:
    assert rel in restore, 'not restored after the build: ' + rel
print('PASS')
