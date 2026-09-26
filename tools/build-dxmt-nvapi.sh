#!/bin/bash
# Build DXMT's nvapi64.dll (research/dxmt/src/nvapi) for arm64ec and ship it.
#
# Upstream's DXMT build leaves it out (meson install_tag 'nvext'), so a game
# that asks NVAPI for the driver finds nothing. With DXMT_ENABLE_NVEXT=1 (the
# library's per-game "Report an NVIDIA GPU" switch) DXGI names NVIDIA as the
# vendor and this DLL answers NvAPI_Initialize, NvAPI_SYS_GetDriverAndBranchVersion
# and friends. Ghost of Tsushima otherwise stops at "Failed to get GPU Driver
# Info" / "No installed graphics card has been detected".
#
# Linked against import libraries derived from the shipped winemetal.dll and
# dxgi.dll (the same trick as build-madeira-d3d12-dll.sh), with DXMT's util
# sources compiled in. Run from the repository root.
set -eu
R="$(pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
D="$R/research/dxmt"
U="$D/src/util"
SHIP="$R/app/Madeira/arm64ec-windows"
OUT="$R/build/dxmt-nvapi"
mkdir -p "$OUT"

[ -f "$D/external/nvapi/nvapi.h" ] || { echo "::error::research/dxmt/external/nvapi is not checked out"; exit 1; }

for dll in winemetal dxgi; do
    { echo "LIBRARY $dll.dll"; echo "EXPORTS"
      "$MINGW/llvm-readobj" --coff-exports "$SHIP/$dll.dll" | awk '$1 == "Name:" { print $2 }' | sort -u; } > "$OUT/$dll.def"
    "$MINGW/llvm-dlltool" -m arm64ec -d "$OUT/$dll.def" -l "$OUT/lib$dll.a"
done

"$MINGW/arm64ec-w64-mingw32-clang++" -std=c++20 -O2 -shared -o "$OUT/nvapi64.dll" \
    "$D/src/nvapi/nvapi.cpp" "$D/src/nvapi/nvapi64.def" \
    "$U/util_env.cpp" "$U/util_string.cpp" "$U/util_futex.cpp" "$U/thread.cpp" \
    "$U/com/com_guid.cpp" "$U/com/com_private_data.cpp" "$U/config/config.cpp" "$U/log/log.cpp" \
    "$U/wsi_monitor_win32.cpp" "$U/wsi_platform_win32.cpp" \
    -I"$D/include" -I"$D/libs" -I"$U" -I"$D/src/winemetal" -I"$D/external/nvapi" -I"$D/src/nvapi" \
    -DNOMINMAX -D_WIN32_WINNT=0xa00 -DDXMT_IOS=1 -DDXMT_PAGE_SIZE=4096 -fblocks \
    -Wno-microsoft-exception-spec \
    -L"$OUT" -lwinemetal -ldxgi -lntdll -static -Wl,--file-alignment=4096 \
    2> "$OUT/nvapi.err" || { grep -m 20 "error" "$OUT/nvapi.err"; exit 1; }
"$MINGW/llvm-strip" --strip-debug "$OUT/nvapi64.dll"

"$MINGW/llvm-readobj" --coff-exports "$OUT/nvapi64.dll" | grep -q "Name: nvapi_QueryInterface" \
    || { echo "::error::nvapi64.dll lacks nvapi_QueryInterface"; exit 1; }
cp "$OUT/nvapi64.dll" "$SHIP/nvapi64.dll"
echo "::notice::nvapi64.dll built from research/dxmt/src/nvapi ($(wc -c < "$OUT/nvapi64.dll" | tr -d ' ') bytes) and shipped"
