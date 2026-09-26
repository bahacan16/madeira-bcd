#!/bin/bash
# Build the ARM64EC FEX module (libarm64ecfex.dll) from the FEX submodule with
# tools/patch-fex-ios-avx.py applied and ship it as xtajit64-avx.dll, next to
# upstream's untouched xtajit64.dll. WineProcessBridge.m links it in as
# system32\xtajit64.dll only for a game whose settings turn AVX on
# (MADEIRA_FEX_AVX=1); every other game runs upstream's module.
#
# build/fex-arm64ec/build.sh does not record everything the committed DLL was
# built with; the options below reproduce it. FEX_IOS_HOST must reach the
# ASSEMBLER too: Module.S holds the iOS transition code (TEB from TPIDR, not
# x18; the FFS bypass). Builds 138-148 missed CMAKE_ASM_FLAGS, got the stock
# ExitToX64 (`str x30,[sp,#-8]!`, sp off 16-byte alignment) and every x64 DLL
# entry point crashed -- with section sizes identical, so sizes prove nothing.
# Before shipping, the unpatched source is built and compared with the
# committed DLL by function list and by the instructions of the Module.S
# transition code; any drift keeps the AVX build out. Run from the repo root.
set -eu
R="$(pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
export PATH="$MINGW:$PATH"
B="${FEX_ARM64EC_BUILD:-$R/FEX/build-arm64ec}"
SHIP="$R/app/Madeira/arm64ec-windows/xtajit64.dll"
AVX="$R/app/Madeira/arm64ec-windows/xtajit64-avx.dll"
CPUF="Source/Windows/Common/CPUFeatures.cpp"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

# The iOS static-library steps patch FEX sources in place; this module is
# built from the pristine submodule, as the committed one was.
git -C FEX diff --name-only | while read -r f; do git -C FEX checkout -- "$f"; done

# TUNE_CPU: FEX's default "native" probes /proc/cpuinfo through a script that
# needs pkg_resources; on a Linux x86 host it settles on cortex-a78, which is
# what reproduces the committed module, and on the macOS runner it aborts the
# configure. Name it outright.
# Always (re)configure: a cached tree from before the ASM flag would keep it.
{
    cmake -S "$R/FEX" -B "$B" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_TOOLCHAIN_FILE="$R/FEX/Data/CMake/toolchain_mingw.cmake" \
        -DMINGW_TRIPLE=arm64ec-w64-mingw32 -DTUNE_CPU=cortex-a78 \
        -DFEX_IOS_HOST_BUILD=ON \
        -DCMAKE_C_FLAGS=-DFEX_IOS_HOST=1 -DCMAKE_CXX_FLAGS=-DFEX_IOS_HOST=1 \
        -DCMAKE_ASM_FLAGS=-DFEX_IOS_HOST=1 \
        -DENABLE_LTO=OFF -DENABLE_FEX_ALLOCATOR=ON -DENABLE_JEMALLOC_GLIBC_ALLOC=ON \
        -DBUILD_FEXCONFIG=OFF -DENABLE_CCACHE=OFF -DBUILD_TESTING=OFF -DBUILD_THUNKS=OFF \
        -DENABLE_ASSERTIONS=OFF > "$B.cfg.log" 2>&1 \
        || { tail -30 "$B.cfg.log"; exit 1; }
}

build() {
    cmake --build "$B" --target arm64ecfex -j"$JOBS" > "$B.build.log" 2>&1 \
        || { grep -m 20 "error" "$B.build.log"; exit 1; }
}

# Section sizes, export names, every function name, and the Module.S
# transition code (DispatchJump .. the end of CheckCall) instruction by
# instruction with addresses blanked -- a different link order moves every
# function, so raw bytes cannot be compared.
fingerprint() {
    "$MINGW/llvm-objdump" -d --no-show-raw-insn "$1" | grep -E '^[0-9a-f]+ <.+>:$' | sed 's/^[0-9a-f]* //' | sort
    "$MINGW/llvm-objdump" -d --no-show-raw-insn "$1" \
        | awk '/<DispatchJump>:/{f=1} f && /^ *[0-9a-f]+:/{l=$0; sub(/^ *[0-9a-f]+:[ \t]*/,"",l); print l} /<CheckCall>:/{c=1} c && /\tret$/{exit}' \
        | sed -E 's/0x[0-9a-f]+//g; s/<[^>]*>//g'
    sectionsizes "$1"
}
sectionsizes() {
    python3 - "$1" <<'PY'
import struct, sys
d = open(sys.argv[1], 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
n = struct.unpack_from('<H', d, pe + 6)[0]
o = pe + 24 + struct.unpack_from('<H', d, pe + 20)[0]
for _ in range(n):
    name = d[o:o + 8].rstrip(b'\0').decode()
    if name in ('.text', '.rdata', '.pdata', '.hexpthk', '.a64xrm', '.tls'):
        print(name, hex(struct.unpack_from('<I', d, o + 8)[0]))
    o += 40
PY
    "$MINGW/llvm-readobj" --coff-exports "$1" | awk '$1 == "Name:" { print $2 }'
}

echo "=== unpatched rebuild, compared with the committed xtajit64.dll ==="
build
if ! diff <(fingerprint "$SHIP") <(fingerprint "$B/Bin/libarm64ecfex.dll"); then
    echo "::warning::the rebuilt FEX module does not match the committed xtajit64.dll -- not shipping an AVX build"
    exit 1
fi
echo "  matches the committed module"

echo "=== with the AVX opt-in ==="
python3 "$R/tools/patch-fex-ios-avx.py" "$R/FEX/$CPUF"
build
git -C FEX checkout -- "$CPUF"
cp "$B/Bin/libarm64ecfex.dll" "$AVX"
echo "::notice::xtajit64-avx.dll built from FEX $(git -C FEX rev-parse --short HEAD) with the MADEIRA_FEX_AVX opt-in and shipped (xtajit64.dll stays upstream's)"
