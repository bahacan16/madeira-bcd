# Building Madeira from a clean checkout (reproducibility record, 2026-09-16)

This is the "scripts to control compilation and installation" record the
LGPL relink obligation depends on (docs/LICENSING.md). Each step says
whether it has been re-executed from a clean checkout. A fresh recursive
clone of the repository at commit 8a8cabe was tested on 2026-09-16 (with
the submodule URLs redirected to the local forks, since nothing is pushed):
the app target does NOT build from the clone alone, because the inputs
marked "not in the repository" below are absent. Two further findings:
`app/Madeira/x86_64-vcruntime` is required by the project but ignored, and
the submodule commits (FEX, its nested rpmalloc fork, wine branch
`madeira-lgpl`, dxmt) exist only locally: the forks named in `.gitmodules`
(all under github.com/willfaust) do not yet carry them, so a recipient's
recursive clone fails at the first submodule until every fork is pushed. This document is the
remediation; steps marked UNVERIFIED have not yet been re-run from scratch.

## Inputs that are not in the repository

| Input | Why absent | How to obtain | Verified from clean |
|---|---|---|---|
| `toolchains/llvm-mingw-20260421-ucrt-macos-universal/` | 122 MB third-party toolchain | `llvm-mingw-20260421-ucrt-macos-universal.tar.xz` from https://github.com/mstorsjo/llvm-mingw/releases/tag/20260421, SHA-256 `bd85a3975723815cef28dbbd2ca2cb0c926f6b348a12a0453f39f7af273cb3f7`, extracted under `toolchains/` | tarball hash recorded; download UNVERIFIED |
| `toolchains/llvm-project/` + `toolchains/llvm-ios-build/` + `toolchains/llvm-host-build/` | LLVM built for iOS (hours) | upstream llvm-project at commit `8dfdcc7b7` ("[libc++] Fix memory leaks when throwing inside std::vector constructor"); configure `llvm-ios-build` with `-DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_SYSROOT=iphoneos -DCMAKE_BUILD_TYPE=Release -DLLVM_HOST_TRIPLE=arm64-apple-ios17.0 -DLLVM_DEFAULT_TARGET_TRIPLE=arm64-apple-ios17.0 -DLLVM_TARGET_ARCH=host -DLLVM_TARGETS_TO_BUILD= -DLLVM_ENABLE_PROJECTS= -DLLVM_BUILD_TOOLS=Off -DLLVM_INCLUDE_TESTS=Off -DLLVM_ENABLE_ZLIB=Off` (values read back from the existing CMakeCache); a host build for tablegen lives in `llvm-host-build` | recipe reconstructed; UNVERIFIED |
| `research/GPTK/Metal Shader Converter 4.0 beta 2.pkg` | Apple installer, 30 MB, licence-bound | Apple developer downloads; SHA-256 `1acc33c87ea663933df89721a998d066106685473020bcbe007cee7a16155734` (pinned in `build/madeira-d3d12/deps.sh`). Only needed to REBUILD the converter fetch; the library itself is tracked | n/a |
| `app/Madeira/x86_64-vcruntime/` | Microsoft Visual C++ 2015-2022 x64 runtime DLLs (concrt140, msvcp140*, vcamp140, vccorlib140, vcruntime140*), redistributable under Microsoft's terms, not under this repository's licence | extract from Microsoft's `vc_redist.x64.exe` (or copy from `C:\Windows\System32` of a licensed Windows install) into that folder | UNVERIFIED |
| StikDebug (JIT) and a free Apple ID | runtime requirements | see README | n/a |

## Native build chains (all in the repository)

Run in this order after the inputs above are in place. Outputs are
git-ignored and consumed by the app project.

1. `build/gnutls-ios/build.sh`: GMP 6.3.0, Nettle 3.10.1, GnuTLS 3.8.9 from
   the tracked tarballs in `build/gnutls-ios/src` (SHA256SUMS there) ->
   `app/Madeira/lib{gmp,nettle,hogweed,gnutls}.a` (these four outputs are
   also tracked). Verified: built on the development machine; not re-run
   from a clean checkout.
2. FEX (submodule, branch ios-port-2607):
   - `FEX/build-ios`: `build/fex-ios/build.sh` (same options as the development CMakeCache) -> `FEX/build-ios/FEXCore/Source/lib{FEXCore,FEXCore_Base,JemallocLibs}.a` and the `External/{cephes,fmt,SoftFloat-3e,xxhash}` archives. UNVERIFIED from clean.
   - `FEX/build-arm64ec`: `build/fex-arm64ec/build.sh` (configures with `FEX/Data/CMake/toolchain_mingw.cmake` and the recorded options on first run, builds target `arm64ecfex`, copies `Bin/libarm64ecfex.dll` to `app/Madeira/arm64ec-windows/xtajit64.dll`). The build step was verified this session; the first-run configure in the script is reconstructed from CMakeCache and UNVERIFIED.
3. Wine (submodule, branch madeira-lgpl):
   - unix side: `build/ntdll-unix/build.sh`, `build/wineserver/build.sh`,
     `build/win32u-unix/build.sh` -> `app/Madeira/lib{ntdll_unix,wineserver,win32u_unix}.a`. Verified on the development machine.
   - PE side: `build/wine-pe/build-ntdll.sh` (configures `wine/build-arm64ec` with `--enable-archs=arm64ec --without-x --disable-tests` on first run, builds `dlls/ntdll`, strips, pads to SizeOfImage + 0x50000, copies to the app). Other PE modules: `make -C dlls/<name>` in that tree and copy the DLL, as the script's header says. The strip/pad step was verified this session; the configure step is UNVERIFIED from clean.
4. DXMT (submodule, branch ios-port):
   - unix side: `build/dxmt-ios/build.sh` (needs `toolchains/llvm-ios-build`) -> `app/Madeira/libdxmt_combined.a` (ignored; the app links it). Verified this session.
   - PE side: `meson setup research/dxmt/build-arm64ec research/dxmt -Dbuildtype=release -Dwine_build_path=../../wine/build-arm64ec --cross-file=research/dxmt/build-arm64ec-win.txt` then `ninja -C research/dxmt/build-arm64ec src/winemetal/winemetal.dll` (and d3d11.dll) -> copied to `app/Madeira/arm64ec-windows/`. Verified this session (winemetal.dll).
5. Native D3D12 runtime: `build/madeira-d3d12/build-pe.sh` -> `d3d12.dll`, `madeira_d3d12.dll` and the test executables in `app/Madeira/arm64ec-windows/` (tracked). Verified this session. `build/madeira-d3d12/fetch-converter.sh` re-verifies the converter library; `build/stage-licenses.sh` refreshes the bundled licence copies (the Xcode build fails if they are stale).
6. App: `xcodebuild -project app/Madeira.xcodeproj -scheme Madeira -destination 'generic/platform=iOS' -allowProvisioningUpdates build` (Debug is the configuration that runs the games; Release builds have crashed the guest), then zip `Payload/Madeira.app` into an IPA and sideload. Verified this session on the development machine.

## Status of the LGPL relink question

A recipient of a built package can obtain the complete corresponding
source of every LGPL library (Wine fork, GnuTLS, Nettle, GMP) from the
repository, and the application source and build scripts above. Whether
they can actually relink depends on assembling the "not in the repository"
inputs and re-executing the UNVERIFIED steps; that end-to-end clean-machine
rebuild, signing and installation has NOT been performed. Until it is,
docs/LICENSING.md keeps the relink capability marked unverified. The
alternative the LGPL offers, shipping the application's object files, is
not currently done.
