#!/bin/bash
# Build faultrep.dll (arm64ec) from build/faultrep and ship it next to the
# other arm64ec Windows DLLs. Upstream's Wine DLL set has no faultrep.dll, and
# games that import it (Ghost of Tsushima) die in the loader with
# STATUS_DLL_NOT_FOUND. Run from the repository root.
set -eu
R="$(pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
SRC="$R/build/faultrep"
OUT="$R/build/faultrep/out"
SHIP="$R/app/Madeira/arm64ec-windows"
mkdir -p "$OUT"

"$MINGW/arm64ec-w64-mingw32-clang" -shared -O2 -Wall \
    -o "$OUT/faultrep.dll" "$SRC/faultrep.c" "$SRC/faultrep.def"

want=$(awk 'f && NF { print $1 } /^EXPORTS/ { f = 1 }' "$SRC/faultrep.def" | sort)
have=$("$MINGW/llvm-readobj" --coff-exports "$OUT/faultrep.dll" | awk '$1 == "Name:" { print $2 }' | sort)
if [ "$want" != "$have" ]; then
    echo "::error::faultrep.dll exports do not match faultrep.def"
    diff <(echo "$want") <(echo "$have") || true
    exit 1
fi

cp "$OUT/faultrep.dll" "$SHIP/faultrep.dll"
echo "::notice::faultrep.dll built ($(wc -c < "$OUT/faultrep.dll" | tr -d ' ') bytes, $(echo "$have" | wc -l | tr -d ' ') exports) and shipped"
