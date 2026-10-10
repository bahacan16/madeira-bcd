#!/bin/bash
# Build madeira_d3d12.dll (arm64ec) from madeira-d3d12 and ship it in
# place of upstream's tracked binary, as both madeira_d3d12.dll and d3d12.dll;
# then the x64 D3D12 cube test the home screen starts.
#
# build/madeira-d3d12/build-pe.sh links against dxmt/build-arm64ec's
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
SRC="$R/madeira-d3d12/src/pe"
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

# madeira-bcd: the shader cache's converter identity (madeira_d3d12.c,
# MAD_SC_CONVERTER_ID) -- a hash of everything that shapes a conversion: the
# service and the IR ABI, its build script, DXMT's DXBC compiler and parser as
# patched for this build, LLVM's configuration, and Apple's converter library
# and headers. A build that changes none of them keeps the device's shader cache
# instead of converting every shader again. Runs after dxmt-ios and the MSC
# staging steps, so it sees exactly what this build ships.
scid_inputs() {
    local f x
    for f in madeira-d3d12/src/unix madeira-d3d12/src/madeira_ir_abi.h \
             build/dxmt-ios/build.sh dxmt/src/airconv dxmt/libs \
             toolchains/llvm-ios-build/include/llvm/Config/llvm-config.h \
             app/Madeira/d3d12/libmetalirconverter.dylib ${MADEIRA_MSC_INCLUDE:+"$MADEIRA_MSC_INCLUDE"}; do
        [ -e "$f" ] || { echo "absent ${f#"$R"/}"; continue; }
        find "$f" -type f | LC_ALL=C sort | while read -r x; do
            echo "$(shasum -a 256 < "$x" | cut -c1-64) ${x#"$R"/}"
        done
    done
}
SCID=$(cd "$R" && scid_inputs | shasum -a 256 | cut -c1-16)
echo "  shader cache converter identity $SCID"
# The IPA build prints it in its notices.
echo "$SCID" > "$OUT/scid.txt"
# MAD_BUILD_ID (set by the workflow) names this build in the device log.
BUILDDEF=()
[ -n "${MAD_BUILD_ID:-}" ] && BUILDDEF=(-DMAD_BUILD_ID="\"$MAD_BUILD_ID\"")

# madeira-bcd: helper kernels (mad_kernels.metal) as an embedded metallib. A
# build without xcrun (or a failed compile) leaves them out; the runtime then
# skips the draws that need them, as before.
echo "=== helper kernels (metallib) ==="
KDEF=""
# madeira-bcd clear-rects: should its clear functions not compile, the file is
# compiled again without them, so the other kernels never depend on them. The
# typed-view-align texture-buffer probe goes first the same way.
mad_kernels_metal() {
    xcrun -sdk iphoneos metal -std=metal3.0 "$@" -o "$OUT/mad_kernels.metallib" "$SRC/mad_kernels.metal" 2> "$OUT/mad_kernels.err"
}
if command -v xcrun > /dev/null 2>&1 &&
   { mad_kernels_metal ||
     { echo "::warning::helper kernels: the first compile failed -- trying without the texture-buffer probe"
       head -20 "$OUT/mad_kernels.err"
       mad_kernels_metal -DMAD_NO_TEXBUF_PROBE; } ||
     { echo "::warning::helper kernels: trying without the clear-rects functions"
       head -20 "$OUT/mad_kernels.err"
       mad_kernels_metal -DMAD_NO_CLEAR_RECTS; } ||
     { echo "::warning::helper kernels: trying without the probe and the clear-rects functions"
       head -20 "$OUT/mad_kernels.err"
       mad_kernels_metal -DMAD_NO_TEXBUF_PROBE -DMAD_NO_CLEAR_RECTS; }; }; then
    (cd "$OUT" && xxd -i mad_kernels.metallib > mad_kernels_metallib.h)
    KDEF="-DMAD_HAVE_KERNELS"
    echo "  $(wc -c < "$OUT/mad_kernels.metallib" | tr -d ' ') bytes"
else
    echo "::warning::helper kernels did not build -- indirect tessellation draws stay skipped"
    cat "$OUT/mad_kernels.err" 2> /dev/null | head -20 || true
fi

echo "=== madeira_d3d12.dll (arm64ec) ==="
"$MINGW/arm64ec-w64-mingw32-clang" -shared -O2 -Wall -DMAD_SC_CONVERTER_ID="\"$SCID\"" $KDEF ${BUILDDEF[@]+"${BUILDDEF[@]}"} \
    -o "$OUT/madeira_d3d12.dll" "$SRC/madeira_d3d12.c" "$SRC/d3d12.def" \
    -I"$SRC" -I"$OUT" -I"$R/madeira-d3d12/src" -I"$R/dxmt/src/winemetal" \
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

python3 "$R/tests/host/check-x64-graphics-entry.py" --d3d12 "$OUT/madeira_d3d12.dll"

cp "$OUT/madeira_d3d12.dll" "$SHIP/madeira_d3d12.dll"
cp "$OUT/madeira_d3d12.dll" "$SHIP/d3d12.dll"
echo "::notice::madeira_d3d12.dll and d3d12.dll rebuilt from madeira-d3d12 and shipped"

echo "=== d3d12-cube-x64.exe (x86_64 guest, visible) ==="
TESTS="$R/madeira-d3d12/tests/windows"
if "$MINGW/x86_64-w64-mingw32-clang" -O2 -Wall -mwindows \
       -o "$OUT/d3d12-cube-x64.exe" "$TESTS/cube_window.c" -I"$TESTS" -luuid -lole32 2> "$OUT/cube.err"; then
    cp "$OUT/d3d12-cube-x64.exe" "$SHIP/d3d12-cube-x64.exe"
    echo "::notice::d3d12-cube-x64.exe rebuilt ($(wc -c < "$OUT/d3d12-cube-x64.exe" | tr -d ' ') bytes) and shipped"
else
    grep -m 20 "error:" "$OUT/cube.err"
    echo "::warning::d3d12-cube-x64.exe did not build -- keeping the tracked test"
fi
