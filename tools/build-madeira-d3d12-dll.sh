#!/bin/bash
# Build madeira_d3d12.dll (arm64ec) from research/madeira-d3d12 and ship it in
# place of upstream's tracked binary, as both madeira_d3d12.dll and d3d12.dll.
#
# build/madeira-d3d12/build-pe.sh links against research/dxmt/build-arm64ec's
# libwinemetal.a, which only a full meson build of DXMT's PE half produces. The
# only thing taken from it is winemetal.dll's import table, so derive the import
# library from the winemetal.dll that ships next to it instead: same exports,
# same DLL the runtime will actually load.
#
# Refuses to replace the shipped DLL unless the new one exports every name the
# tracked one does. Run from the repository root.
set -eu
R="$(pwd)"
MINGW="$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin"
SRC="$R/research/madeira-d3d12/src/pe"
OUT="$R/build/madeira-d3d12/out-pe"
SHIP="$R/app/Madeira/arm64ec-windows"
mkdir -p "$OUT"

exports() { "$MINGW/llvm-readobj" --coff-exports "$1" | awk '$1 == "Name:" { print $2 }' | sort -u; }

echo "=== import library from the shipped winemetal.dll ==="
{ echo "LIBRARY winemetal.dll"; echo "EXPORTS"; exports "$SHIP/winemetal.dll"; } > "$OUT/winemetal.def"
echo "  $(($(wc -l < "$OUT/winemetal.def") - 2)) exports"
"$MINGW/llvm-dlltool" -m arm64ec -d "$OUT/winemetal.def" -l "$OUT/libwinemetal.a"

echo "=== vtable stubs ==="
python3 "$SRC/gen_vtables.py" \
    "$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/generic-w64-mingw32/include/d3d12.h" \
    "$SRC/madeira_d3d12_stubs.h" > /dev/null

echo "=== madeira_d3d12.dll (arm64ec) ==="
"$MINGW/arm64ec-w64-mingw32-clang" -shared -O2 -Wall \
    -o "$OUT/madeira_d3d12.dll" "$SRC/madeira_d3d12.c" "$SRC/d3d12.def" \
    -I"$SRC" -I"$R/research/madeira-d3d12/src" -I"$R/research/dxmt/src/winemetal" \
    -L"$OUT" -lwinemetal -luuid -lole32 2> "$OUT/madeira_d3d12.err" \
    || { grep -m 20 "error:" "$OUT/madeira_d3d12.err"; exit 1; }
echo "  built $(wc -c < "$OUT/madeira_d3d12.dll" | tr -d ' ') bytes (tracked: $(wc -c < "$SHIP/madeira_d3d12.dll" | tr -d ' '))"

exports "$SHIP/madeira_d3d12.dll" > "$OUT/tracked.exports"
exports "$OUT/madeira_d3d12.dll" > "$OUT/built.exports"
MISSING=$(comm -23 "$OUT/tracked.exports" "$OUT/built.exports")
if [ -n "$MISSING" ]; then
    echo "::error::the rebuilt madeira_d3d12.dll lacks exports the tracked one has -- keeping the tracked DLL:"
    echo "$MISSING" | head -20
    exit 1
fi
echo "  all $(wc -l < "$OUT/tracked.exports" | tr -d ' ') tracked exports present"

cp "$OUT/madeira_d3d12.dll" "$SHIP/madeira_d3d12.dll"
cp "$OUT/madeira_d3d12.dll" "$SHIP/d3d12.dll"
echo "::notice::madeira_d3d12.dll and d3d12.dll rebuilt from research/madeira-d3d12 and shipped"
