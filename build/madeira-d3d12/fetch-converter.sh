#!/bin/bash
# Stage Apple's Metal Shader Converter iOS dynamic library into the app bundle
# source folder, from a FRESH extraction of the hash-verified installer
# package, and verify the extracted library against its pinned hash. Nothing
# from an earlier /tmp extraction or an environment-selected directory is
# trusted. The library is deliberately not in git (Apple-proprietary; ships
# only inside built packages under Apple's agreement, see
# app/Madeira/d3d12/NOTICE.txt). Also stages the licence texts the bundle
# must carry.
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
MSC_PKG="$REPO_ROOT/research/GPTK/Metal Shader Converter 4.0 beta 2.pkg"
MSC_PKG_SHA256="1acc33c87ea663933df89721a998d066106685473020bcbe007cee7a16155734"
MSC_IOS_DYLIB_SHA256="073f903be98e973ff38f4d79f2c48d61ef938754a77b1caedda79c9f05a068c2"
[[ -f "$MSC_PKG" ]] || { echo "fetch-converter: missing $MSC_PKG (supply Apple's installer package)" >&2; exit 1; }
have="$(shasum -a 256 "$MSC_PKG" | cut -d' ' -f1)"
[[ "$have" == "$MSC_PKG_SHA256" ]] || { echo "fetch-converter: package hash mismatch ($have)" >&2; exit 1; }
tmp="$(mktemp -d /private/tmp/madeira-msc-fetch-XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
pkgutil --expand-full "$MSC_PKG" "$tmp/expanded" >/dev/null
SRC="$tmp/expanded/MetalShaderConverter.pkg/Payload/usr/local/lib_iOS/libmetalirconverter.dylib"
[[ -f "$SRC" ]] || { echo "fetch-converter: iOS dylib not in the package payload" >&2; exit 1; }
got="$(shasum -a 256 "$SRC" | cut -d' ' -f1)"
[[ "$got" == "$MSC_IOS_DYLIB_SHA256" ]] || { echo "fetch-converter: extracted iOS dylib hash mismatch ($got)" >&2; exit 1; }
DEST_DIR="$REPO_ROOT/app/Madeira/d3d12"
cp "$SRC" "$DEST_DIR/libmetalirconverter.dylib"
bash "$REPO_ROOT/build/stage-licenses.sh"
echo "staged $(wc -c < "$DEST_DIR/libmetalirconverter.dylib" | tr -d ' ') bytes (sha256 verified) -> $DEST_DIR"
