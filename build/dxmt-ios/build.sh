#!/bin/bash
# Build DXMT winemetal unix side + airconv + dxbc_parser as iOS-aarch64
# static library, for linking into Madeira.app.
#
# Produces: libdxmt_unix.a
set -eu

BUILD_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$BUILD_DIR/../.." && pwd)"
DXMT_SRC="$REPO_ROOT/research/dxmt/src"
DXMT_ROOT="$REPO_ROOT/research/dxmt"
LLVM_SRC="$REPO_ROOT/toolchains/llvm-project/llvm"
LLVM_BUILD="$REPO_ROOT/toolchains/llvm-ios-build"
SDK=$(xcrun --sdk iphoneos --show-sdk-path)
OBJ_DIR="$BUILD_DIR/obj"
OUT_LIB="$BUILD_DIR/libdxmt_unix.a"

mkdir -p "$OBJ_DIR"

COMMON_FLAGS="-arch arm64 -isysroot $SDK -miphoneos-version-min=18.0 -fblocks -O2"
INCLUDES="-I$DXMT_ROOT/include -I$DXMT_ROOT/libs -I$DXMT_SRC/winemetal -I$DXMT_SRC/airconv"
INCLUDES_DIRECTX="-I$DXMT_ROOT/include/native/directx -I$DXMT_ROOT/include/native/windows"
INCLUDES_SHADERS="-I$BUILD_DIR/shader-headers"
LLVM_INCLUDES="-I$LLVM_BUILD/include -I$LLVM_SRC/include"
AIRCONV_DEFS="-D_FILE_OFFSET_BITS=64 -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
CXX_FLAGS="-std=c++20 -fno-exceptions -fno-rtti"

SUCCEEDED=0
FAILED=0
FAILED_FILES=""

compile_objc() {
    local src=$1 name=$2
    # MADEIRA_ONLY=<name>: recompile one object only. madeira_ir_unix carries a __DATE__
    # stamp in the shader-cache key, so a full rebuild costs a full shader recompile on device.
    if [ -n "${MADEIRA_ONLY:-}" ] && [ "$name" != "$MADEIRA_ONLY" ]; then return 0; fi
    printf "  %-40s " "$name"
    if xcrun -sdk iphoneos clang $COMMON_FLAGS -x objective-c $INCLUDES \
        -c "$src" -o "$OBJ_DIR/$name.o" 2>"$OBJ_DIR/$name.err"; then
        echo "OK"; SUCCEEDED=$((SUCCEEDED+1))
    else
        echo "FAILED"; FAILED=$((FAILED+1)); FAILED_FILES="$FAILED_FILES $name"
    fi
}

compile_cxx() {
    local src=$1 name=$2 extra="${3:-}"
    # MADEIRA_ONLY=<name>: recompile one object only. madeira_ir_unix carries a __DATE__
    # stamp in the shader-cache key, so a full rebuild costs a full shader recompile on device.
    if [ -n "${MADEIRA_ONLY:-}" ] && [ "$name" != "$MADEIRA_ONLY" ]; then return 0; fi
    printf "  %-40s " "$name"
    if xcrun -sdk iphoneos clang++ $COMMON_FLAGS $CXX_FLAGS $INCLUDES $INCLUDES_DIRECTX $INCLUDES_SHADERS $LLVM_INCLUDES $AIRCONV_DEFS $extra \
        -c "$src" -o "$OBJ_DIR/$name.o" 2>"$OBJ_DIR/$name.err"; then
        echo "OK"; SUCCEEDED=$((SUCCEEDED+1))
    else
        echo "FAILED"; FAILED=$((FAILED+1)); FAILED_FILES="$FAILED_FILES $name"
    fi
}

# ---- madeira-d3d12 M1 canary (optional) -------------------------------------
# Compiled into this library so the app can run the shader-converter gate
# in-process. Guarded: the converter package is a locally supplied dependency
# and the DXMT build must not start failing when it is absent.
compile_objcxx_arc() {
    local src=$1 name=$2 extra="${3:-}"
    # MADEIRA_ONLY=<name>: recompile one object only. madeira_ir_unix carries a __DATE__
    # stamp in the shader-cache key, so a full rebuild costs a full shader recompile on device.
    if [ -n "${MADEIRA_ONLY:-}" ] && [ "$name" != "$MADEIRA_ONLY" ]; then return 0; fi
    printf "  %-40s " "$name"
    if xcrun -sdk iphoneos clang++ $COMMON_FLAGS -std=c++20 -fobjc-arc -x objective-c++ $extra \
        -c "$src" -o "$OBJ_DIR/$name.o" 2>"$OBJ_DIR/$name.err"; then
        echo "OK"; SUCCEEDED=$((SUCCEEDED+1))
    else
        echo "FAILED"; FAILED=$((FAILED+1)); FAILED_FILES="$FAILED_FILES $name"
    fi
}
# madeira-bcd: deps.sh is `set -eu` and `exit 1`s when the package is absent;
# sourced directly, that exit ends THIS script. Probe it in a subshell first.
# MADEIRA_MSC_INCLUDE points at the converter's public headers alone (CI has
# no installer package); the converter library itself is loaded at runtime
# with dlopen, so the headers are all this compile needs.
HAVE_MSC=0
if [[ -n "${MADEIRA_MSC_INCLUDE:-}" && -f "$MADEIRA_MSC_INCLUDE/metal_irconverter/metal_irconverter.h" ]]; then
    MSC_INCLUDE="$MADEIRA_MSC_INCLUDE"; HAVE_MSC=1
elif [[ -f "$BUILD_DIR/../madeira-d3d12/deps.sh" ]] && \
     ( source "$BUILD_DIR/../madeira-d3d12/deps.sh" ) >/dev/null 2>&1; then
    source "$BUILD_DIR/../madeira-d3d12/deps.sh"; HAVE_MSC=1
fi
if [[ "$HAVE_MSC" == 1 ]]; then
    echo "=== madeira-d3d12 canary (Objective-C++, Metal Shader Converter) ==="
    compile_objcxx_arc "$REPO_ROOT/research/madeira-d3d12/tests/native/msc_canary.mm" \
                       msc_canary "-DIR_PRIVATE_IMPLEMENTATION -I$MSC_INCLUDE"
    # The runtime conversion service reached from the D3D12 runtime through
    # winemetal's unix call. Deliberately NOT defining IR_PRIVATE_IMPLEMENTATION
    # here: the converter's runtime header emits its bind points and helper
    # bodies only where that macro is set, and defining it in a second
    # translation unit gives duplicate symbols. The canary owns the one copy.
    # ml1008: also needs airconv_public.h -- shader-model-5.x DXBC goes to the
    # in-tree AIR compiler, which is linked into this same archive, so the shim
    # includes the compiler's real header rather than restating its structs.
    compile_objcxx_arc "$REPO_ROOT/research/madeira-d3d12/src/unix/madeira_ir_unix.mm" \
                       madeira_ir_unix "-I$MSC_INCLUDE -I$REPO_ROOT/research/madeira-d3d12/src $INCLUDES $INCLUDES_DIRECTX"
    # ml1011: the input-layout resolver, plain C++ because DXBCParser's signature
    # reader includes a Windows shim whose BOOL clashes with Objective-C's.
    compile_cxx "$REPO_ROOT/research/madeira-d3d12/src/unix/madeira_sm5_ia.cpp" \
                madeira_sm5_ia "-I$REPO_ROOT/research/madeira-d3d12/src"
    # ml1149: AMD AGS 64-bit atomics -> native SM6.6 atomics, a DXIL rewrite on
    # the LLVM 15 that airconv already links (bitcode reader + writer).
    compile_cxx "$REPO_ROOT/research/madeira-d3d12/src/unix/madeira_ags.cpp" madeira_ags
else
    echo "=== madeira-d3d12 canary SKIPPED (converter headers not resolvable) ==="
    echo "=== madeira-d3d12 conversion STUB: D3D12 pipelines will fail, D3D11 unaffected ==="
    compile_objc "$REPO_ROOT/build/madeira-d3d12/madeira_ir_stub.c" madeira_ir_stub
fi

echo "=== winemetal unix (Objective-C) ==="
compile_objc "$DXMT_SRC/winemetal/unix/winemetal_unix.c" winemetal_unix
compile_objc "$DXMT_SRC/winemetal/unix/cache.c"          cache

echo "=== airconv (C++ 20, needs LLVM headers) ==="
for cpp in airconv_context.cpp air_type.cpp air_signature.cpp air_operations.cpp \
           dxbc_converter.cpp dxbc_converter_gs.cpp dxbc_converter_ts.cpp \
           dxbc_converter_basicblock.cpp dxbc_converter_cfg.cpp \
           dxbc_instructions.cpp dxbc_signature.cpp metallib_writer.cpp; do
    name=$(basename "$cpp" .cpp)
    compile_cxx "$DXMT_SRC/airconv/$cpp" "$name"
done
compile_cxx "$DXMT_SRC/airconv/nt/air_builder.cpp" air_builder
compile_cxx "$DXMT_SRC/airconv/nt/dxbc_converter_base.cpp" dxbc_converter_base
compile_cxx "$DXMT_SRC/airconv/transforms/lower_16bit_texread.cpp" lower_16bit_texread

echo "=== DXBCParser (uses exceptions — override) ==="
for cpp in BlobContainer.cpp DXBCUtils.cpp ShaderBinary.cpp; do
    name=dxbc_$(basename "$cpp" .cpp)
    # ShaderBinary uses `throw`, so we can't use -fno-exceptions from CXX_FLAGS.
    printf "  %-40s " "$name"
    if xcrun -sdk iphoneos clang++ $COMMON_FLAGS -std=c++20 -fno-rtti \
            $INCLUDES $INCLUDES_DIRECTX $AIRCONV_DEFS \
            -c "$DXMT_ROOT/libs/DXBCParser/$cpp" -o "$OBJ_DIR/$name.o" 2>"$OBJ_DIR/$name.err"; then
        echo "OK"; SUCCEEDED=$((SUCCEEDED+1))
    else
        echo "FAILED"; FAILED=$((FAILED+1)); FAILED_FILES="$FAILED_FILES $name"
    fi
done

echo ""
echo "Results: $SUCCEEDED succeeded, $FAILED failed"
if [ -n "$FAILED_FILES" ]; then
    echo "Failed:$FAILED_FILES"
    echo "See .err files in $OBJ_DIR/"
    exit 1
fi

echo ""
echo "=== Archiving libdxmt_unix.a ==="
xcrun -sdk iphoneos ar rcs "$OUT_LIB" "$OBJ_DIR"/*.o
echo "Built: $OUT_LIB ($(wc -c < "$OUT_LIB" | tr -d ' ') bytes)"

# The app links libdxmt_combined.a (this unix side merged with the LLVM archives
# airconv needs), NOT libdxmt_unix.a. Refreshing only the latter is how a change
# here reaches nothing: the app would keep linking the previous objects and the
# build would look clean. Replace our members in place and re-index.
COMBINED="$BUILD_DIR/libdxmt_combined.a"
if [ -f "$COMBINED" ]; then
    echo "=== Refreshing libdxmt_combined.a ==="
    xcrun -sdk iphoneos ar r "$COMBINED" "$OBJ_DIR"/*.o
    xcrun -sdk iphoneos ranlib "$COMBINED"
    echo "Refreshed: $COMBINED ($(wc -c < "$COMBINED" | tr -d ' ') bytes)"
    APP_COPY="$REPO_ROOT/app/Madeira/libdxmt_combined.a"
    if [ -f "$APP_COPY" ]; then cp "$COMBINED" "$APP_COPY"; echo "Staged: $APP_COPY"; fi
else
    echo "NOTE: $COMBINED absent; the app links that file, so build it before deploying."
fi
