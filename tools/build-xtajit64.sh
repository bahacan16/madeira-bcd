#!/bin/bash
# Rebuild the ARM64EC FEX module (libarm64ecfex.dll, shipped as xtajit64.dll)
# from the FEX submodule with tools/patch-fex-ios-avx.py applied, so a game can
# opt in to AVX with MADEIRA_FEX_AVX=1. Without the variable the module behaves
# exactly like the committed one.
#
# build/fex-arm64ec/build.sh does not record everything the committed DLL was
# built with; the options below reproduce it (same exports and imports, and
# the same .text/.rdata/.pdata sizes to the byte). Before shipping anything,
# the unpatched source is built and compared against the committed DLL: if
# the sizes differ, the recipe or the source has drifted and the committed DLL
# is kept. Run from the repository root.
set -eu
R="$(pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
export PATH="$MINGW:$PATH"
B="${FEX_ARM64EC_BUILD:-$R/FEX/build-arm64ec}"
SHIP="$R/app/Madeira/arm64ec-windows/xtajit64.dll"
CPUF="Source/Windows/Common/CPUFeatures.cpp"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

# The iOS static-library steps patch FEX sources in place; this module is
# built from the pristine submodule, as the committed one was.
git -C FEX diff --name-only | while read -r f; do git -C FEX checkout -- "$f"; done

if [ ! -f "$B/CMakeCache.txt" ]; then
    cmake -S "$R/FEX" -B "$B" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_TOOLCHAIN_FILE="$R/FEX/Data/CMake/toolchain_mingw.cmake" \
        -DMINGW_TRIPLE=arm64ec-w64-mingw32 \
        -DFEX_IOS_HOST_BUILD=ON \
        -DCMAKE_C_FLAGS=-DFEX_IOS_HOST=1 -DCMAKE_CXX_FLAGS=-DFEX_IOS_HOST=1 \
        -DENABLE_LTO=OFF -DENABLE_FEX_ALLOCATOR=ON -DENABLE_JEMALLOC_GLIBC_ALLOC=ON \
        -DBUILD_FEXCONFIG=OFF -DENABLE_CCACHE=OFF -DBUILD_TESTING=OFF -DBUILD_THUNKS=OFF \
        -DENABLE_ASSERTIONS=OFF > "$B.cfg.log" 2>&1 \
        || { tail -30 "$B.cfg.log"; exit 1; }
fi

build() {
    cmake --build "$B" --target arm64ecfex -j"$JOBS" > "$B.build.log" 2>&1 \
        || { grep -m 20 "error" "$B.build.log"; exit 1; }
}

# Section sizes and export names: equal for the same source and recipe, even
# though a different link order moves every function.
fingerprint() {
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
    echo "::warning::the rebuilt FEX module does not match the committed xtajit64.dll -- keeping the committed one (no AVX opt-in this build)"
    exit 1
fi
echo "  matches the committed module"

echo "=== with the AVX opt-in ==="
python3 "$R/tools/patch-fex-ios-avx.py" "$R/FEX/$CPUF"
build
git -C FEX checkout -- "$CPUF"
cp "$B/Bin/libarm64ecfex.dll" "$SHIP"
echo "::notice::xtajit64.dll rebuilt from FEX $(git -C FEX rev-parse --short HEAD) with the MADEIRA_FEX_AVX opt-in and shipped"
