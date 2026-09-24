#!/bin/bash
# Configure (first time) and build the ARM64EC FEX module (libarm64ecfex.dll,
# shipped as xtajit64.dll). Options mirror the development build's CMakeCache.
set -eu
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PATH="$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin:$PATH"
B="$R/FEX/build-arm64ec"
if [ ! -f "$B/CMakeCache.txt" ]; then
    cmake -S "$R/FEX" -B "$B" -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_TOOLCHAIN_FILE="$R/FEX/Data/CMake/toolchain_mingw.cmake" \
        -DENABLE_FEX_ALLOCATOR=ON -DENABLE_JEMALLOC_GLIBC_ALLOC=ON -DENABLE_OFFLINE_RUNTIME=ON \
        -DBUILD_FEXCONFIG=ON -DENABLE_CLANG_THUNKS=ON -DENABLE_CCACHE=ON \
        -DBUILD_TESTING=OFF -DBUILD_THUNKS=OFF -DENABLE_ASSERTIONS=OFF
fi
cmake --build "$B" --target arm64ecfex
cp "$B/Bin/libarm64ecfex.dll" "$R/app/Madeira/arm64ec-windows/xtajit64.dll" && ls -l "$R/app/Madeira/arm64ec-windows/xtajit64.dll"
