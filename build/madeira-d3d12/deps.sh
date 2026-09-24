#!/bin/bash
# Resolve the Metal Shader Converter dependency for the madeira-d3d12 track.
#
# The converter is a LOCALLY SUPPLIED development dependency. Its installer
# terms have not been reviewed for redistribution, so nothing here copies it
# into the repository or into a build product; we reference an extraction of the
# pinned package and verify that package's hash before trusting it.
#
# Set MADEIRA_MSC_ROOT to an existing extraction to skip re-extracting. Sourcing
# this script exports:
#   MSC_ROOT      extraction root
#   MSC_PAYLOAD   .../MetalShaderConverter.pkg/Payload
#   MSC_INCLUDE   public headers
#   MSC_LIB_MACOS macOS universal dylib   (fast native iteration loop)
#   MSC_LIB_IOS   iOS arm64 dylib         (what actually ships in the app)
set -eu

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MSC_PKG="$REPO_ROOT/research/GPTK/Metal Shader Converter 4.0 beta 2.pkg"

# Pinned in MADEIRA_NATIVE_D3D12_EXECUTION_DESIGN.md section 2. A different
# package is a different compiler and invalidates every cached shader.
MSC_PKG_SHA256="1acc33c87ea663933df89721a998d066106685473020bcbe007cee7a16155734"

if [[ ! -f "$MSC_PKG" ]]; then
    echo "deps: missing converter package:" >&2
    echo "      $MSC_PKG" >&2
    echo "      Supply it locally; it is deliberately not vendored." >&2
    exit 1
fi

have="$(shasum -a 256 "$MSC_PKG" | cut -d' ' -f1)"
if [[ "$have" != "$MSC_PKG_SHA256" ]]; then
    echo "deps: converter package hash mismatch" >&2
    echo "      expected $MSC_PKG_SHA256" >&2
    echo "      found    $have" >&2
    echo "      Shader cache keys include the compiler package; refusing to continue." >&2
    exit 1
fi

MSC_ROOT="${MADEIRA_MSC_ROOT:-}"
if [[ -z "$MSC_ROOT" || ! -d "$MSC_ROOT/MetalShaderConverter.pkg/Payload" ]]; then
    # Reuse a previous extraction when one is intact, otherwise make a new one.
    for cand in /private/tmp/madeira-msc-*/expanded /private/tmp/madeira-dx12-design.*/expanded; do
        # ml880: a half-deleted /tmp extraction still has the Payload directory
        # but not every header; require one of the headers the compiler failed on.
        [[ -f "$cand/MetalShaderConverter.pkg/Payload/usr/local/include/metal_irconverter/ir_comparison_function.h" && \
           -f "$cand/MetalShaderConverter.pkg/Payload/usr/local/lib_iOS/libmetalirconverter.dylib" ]] && { MSC_ROOT="$cand"; break; }
    done
fi
if [[ -z "$MSC_ROOT" || ! -d "$MSC_ROOT/MetalShaderConverter.pkg/Payload" ]]; then
    d="$(mktemp -d /private/tmp/madeira-msc-XXXXXX)"
    echo "deps: expanding converter package into $d/expanded" >&2
    pkgutil --expand-full "$MSC_PKG" "$d/expanded" >/dev/null
    MSC_ROOT="$d/expanded"
fi

MSC_PAYLOAD="$MSC_ROOT/MetalShaderConverter.pkg/Payload"
MSC_INCLUDE="$MSC_PAYLOAD/usr/local/include"
MSC_LIB_MACOS="$MSC_PAYLOAD/usr/local/lib/libmetalirconverter.dylib"
MSC_LIB_IOS="$MSC_PAYLOAD/usr/local/lib_iOS/libmetalirconverter.dylib"

for f in "$MSC_INCLUDE/metal_irconverter/metal_irconverter.h" \
         "$MSC_INCLUDE/metal_irconverter_runtime/metal_irconverter_runtime.h" \
         "$MSC_LIB_MACOS" "$MSC_LIB_IOS"; do
    [[ -e "$f" ]] || { echo "deps: extraction incomplete, missing $f" >&2; exit 1; }
done

export MSC_ROOT MSC_PAYLOAD MSC_INCLUDE MSC_LIB_MACOS MSC_LIB_IOS
