# madeira-bcd: what this fork adds to willfaust/Madeira

Rebased on upstream `5a82d39` (2026-09-24) on 2026-09-24; tracks upstream main by merge, plus upstream PRs #28 (WoW64, D3D9) and #29 (controller layouts) by 125hz. Upstream carries the
runtime (Wine, FEX, DXMT, the native D3D12 runtime); this fork carries a CI
build and the app-side pieces below. Everything else is upstream's.

## CI build (`.github/workflows/build-ipa.yml`)

Builds an unsigned IPA on a GitHub macOS runner from a clean checkout: Wine's
unix side, wineserver, win32u, FEX's iOS archives, LLVM 15 for iOS, DXMT's unix
half and the app. The PE-side DLLs (ntdll, DXMT) are upstream's tracked
builds; `madeira_d3d12.dll`, `xtajit64.dll` and `faultrep.dll` are built from
source (below). The Microsoft VC++ runtime is fetched from
Microsoft at build time and never committed. Archived as Debug, per upstream's
docs/BUILDING.md.

**Metal Shader Converter.** The D3D12 runtime's unix half compiles against
Apple's converter headers, which upstream does not track. Apple's installer
(`Metal_Shader_Converter_4.0_beta_2.pkg`, from developer.apple.com/download) is
kept as an asset of a DRAFT release tagged `msc-private` -- drafts are visible
only to people with write access, so the package is never published. CI
expands it, stages the Apache-2.0 public headers in
`build/madeira-d3d12/msc-include/` (ignored) and copies the package's iOS
library over `app/Madeira/d3d12/libmetalirconverter.dylib`, so the headers and
the library always come from one build. First run (build 122): converter
4.0.1, iOS library sha256 `073f903b...` -- identical to upstream's tracked one.
Without the asset `build/madeira-d3d12/madeira_ir_stub.c` answers every
conversion with `MADEIRA_IR_NO_DYLIB`: D3D12 pipelines fail by name, D3D11 is
unaffected. The job has `contents: write` only because listing drafts needs it.

**Upstream fixes applied in CI.** Upstream builds from long-lived trees; a clean
checkout of its pins does not build. The workflow works around each, and every
step says so when it becomes a no-op:
- wine `dlls/ntdll/arm64ec_x64_export_iat.c` is included but was never
  committed: an empty placeholder for makedep (PE-side only, never compiled).
- wine `dlls/ntdll/unix/sync.c` includes `build/madeira_cfg.h` before
  `config.h` on purpose; makedep's ordering check is made a warning.
- `server_ios.c` reads `rusage_info_v6.ri_page_wait_time_mach`, absent from the
  runner's SDK: the `pgw=` figure of the `[xp]` line reads 0.
- FEX (`tools/patch-fex-ios-probes.py`): two diagnostic probes compiled outside
  their guards, and `rpm_cas_snapshot_take` (rpmalloc, not built with
  `ENABLE_FEX_ALLOCATOR=OFF`) gets a weak fallback; `IOS_RPM_GUARD` gets a
  no-op definition in the system-allocator branch.

## WoW64 and D3D9 (125hz, upstream PR #28, not yet merged upstream)

Merged from `willfaust/Madeira` pull request #28 (125hz): 32-bit programs run
in a 4 GB guest window at a per-process base (`docs/WOW64.md`), with Wine's
i386 set in `app/Madeira/i386-windows/`, FEX's WoW64 module
(`aarch64-windows/xtajit.dll`) and DXMT's D3D9 frontend. The binaries are
125hz's prebuilt farm (`docs/BINARIES-WOW64.md` lists every file with its
SHA-256), including new 64-bit `ntdll`, `nsi`, DXMT DLLs and `xtajit64.dll`.
That `xtajit64.dll` was built from 125hz's FEX change, which the FEX
submodule does not pin; its ARM64EC interface is upstream's plus one
diagnostic export, so `tools/build-xtajit64.sh` keeps building the AVX
variant from the pinned source and checks it against upstream's pre-merge
module (fetched by commit). The two conflicts with this fork were the same
TEB retarget fix (125hz's version kept) and the `[xp]` `pgw` placeholder.

## Runtime

- TEB retarget pass (`build/ntdll-unix/virtual_ios.c`, pass 0 of the x18
  patcher): its literal-pool guard indexed the per-word BITMAP as a byte per
  word, reading past the allocation for code beyond the first eighth of
  `.text`. Upstream's FEX module keeps its hand-written TEB reads near the
  start and never tripped it; a rebuild that links them later lost all six
  retargets and FEX read the TEB from the wrong TSD slot (NULL).

- `xtajit64-avx.dll`: FEX's ARM64EC module rebuilt by
  `tools/build-xtajit64.sh` with `tools/patch-fex-ios-avx.py`, shipped beside
  upstream's untouched `xtajit64.dll` and linked in as
  `system32\xtajit64.dll` only for a game with AVX on (WineProcessBridge.m): the iOS path of
  `FetchHostFeatures` never sets `SupportsAVX` and skips the HostFeatures
  override, so titles compiled for AVX (Ghost of Tsushima) die on their first
  VEX instruction (c000001d). `MADEIRA_FEX_AVX=1` turns on FEX's 128-bit AVX
  emulation for that launch; the game settings' "AVX / AVX2" switch sets it.
  Off by default, so titles that check CPUID keep their SSE paths. The script
  first rebuilds the unpatched source and ships nothing unless it matches the
  committed DLL (sections and exports); the recipe (`MINGW_TRIPLE`,
  `FEX_IOS_HOST_BUILD`, `-DFEX_IOS_HOST=1` for C, C++ AND the assembler, LTO off;
  builds 138-148 missed the assembler flag, got the stock `ExitToX64` and
  crashed every x64 DLL entry point with a misaligned sp) is not in upstream's
  `build/fex-arm64ec/build.sh`.
- `faultrep.dll` (`build/faultrep`, `tools/build-faultrep-dll.sh`): upstream's
  Wine set has none, and games that import it fail in the loader with
  STATUS_DLL_NOT_FOUND. Wine's exports plus `WerReportHang`, all succeeding
  without doing anything.
- Extra Wine DLLs (`tools/build-wine-extra-dlls.sh`): the VC++ 2002-2012
  runtimes (msvcr70-110, msvcp60-120, vcomp*), D3DX9 24-42, D3DX10, D3DX11,
  d3dcompiler_33-46, d3d10, d3d10_1, avifil32, msvfw32, dinput, XAudio2 /
  X3DAudio / XAPOFX, built from the wine submodule for arm64ec in CI the way
  upstream configures `wine/build-arm64ec`, stripped and padded like the
  shipped builtins, never replacing one upstream ships. Crysis's
  `Crysis64.exe` needs msvcr80. The msvcr* builds carry
  `tools/patch-wine-msvcrt-datasync.py`: an ARM64EC DLL runs from its JIT-pool
  copy, so its live globals are the copy's and an importer's data imports
  (bound to the PE mapping) read a stale snapshot -- Crysis64's CRT startup
  read a NULL `_acmdln`. At the end of process attach the DLL copies its
  writable sections over the PE mapping's -- with plain stores after a
  VirtualQuery, never VirtualProtect: on a pool-copied image the protect path
  syncs the PE side into the running copy and would wipe the DLL's state. A local build reproduces upstream's
  `msvcr120.dll` section for section. About +11 MB compressed.
- `vulkan-1.dll` (and d3d10/avifil32 if the build above failed)
  (`tools/build-stub-dlls.py`):
  stand-ins generated from Wine's `.spec` export lists, for games that import
  them statically (Crysis Remastered). vulkan-1 reports
  VK_ERROR_INCOMPATIBLE_DRIVER and NULL from the *ProcAddr entry points (Wine's
  forwards to winevulkan, which needs a host driver), so games fall back to
  D3D; d3d10 keeps Wine's forwards to the shipped d3dcompiler_43 and answers
  E_NOTIMPL otherwise; avifil32 returns AVIERR_UNSUPPORTED.

- `NtFlushInstructionCache` (`build/ntdll-unix/virtual_ios.c`) invalidates the
  icache under `WINE_IOS` regardless of `HAVE___CLEAR_CACHE`. CI's generated
  config.h does not define it, and without the flush every x64 guest died in a
  FEX JIT block tail (fault PCs all 64-byte aligned).

- User folders (`WineProcessBridge.m`, after upstream's ml719): Wine's profile
  is named after the unix user (`mobile`), not the template's `madeira`, so
  `C:\users\mobile\Documents` and friends can be missing; Ghost of Tsushima
  stops with "Unable to create the game's save folder". Documents, Desktop,
  Downloads, Music, Pictures, Videos, Saved Games and AppData\{Local,
  LocalLow,Roaming} are made real directories for both names at launch.

- `device_Release` (`research/madeira-d3d12/src/pe/madeira_d3d12.c`) released
  the device's GPU-timeline `MTLSharedEvent` before the heap reclaim that
  reads it; a device created and dropped at once (Ghost of Tsushima's adapter
  probe) crashed in `objc_msgSend` and the game reported "No installed
  graphics card". The event is now released last.

## App

- `HomeView.swift`: a library-first home screen (games with covers and
  per-game settings, the Windows desktop, test programs, settings). The
  original panel is still there under Settings > Developer tools.
- `GameControllerManager.swift`: physical controllers mapped to keyboard and
  mouse (ported from SaimSuhailQu/Madeira 72ca339). Since upstream #22 hands
  pads to games as real XInput controllers (`GamepadInput.swift`), this
  mapping is opt-in while XInput is on (Controllers sheet, "Also send keyboard
  and mouse"), so a game does not get every press twice; detection and the
  live input tester work either way.
- `SessionUI.swift`, modelled on upstream's unreleased new UI (Madeira
  Discord), without its Steam sign-in and downloads: a launch screen with the
  game's cover until its first frame (or 8s after a window appears, for
  programs that never present), the game alone on screen, and a Session panel
  -- touch controls on/off and opacity, edit controls, keyboard, FPS limit,
  display fit (4:3 or stretch), performance overlay, ECO, pointer mode and
  sensitivities, live log. Landscape opens it from the touch-controls bar
  (the game surface is a window-level view, so it lives on that window);
  portrait from the bar under the game. A game started from the library no
  longer shows the developer view, which stays under Settings > Developer
  tools.
- Library (`GameLibrary.swift`, `HomeView.swift`): scans 12 levels deep,
  includes `C:\users` (without AppData/Temp), skips redistributable folders,
  splits a folder that only holds other games into one card each, shows each
  card's exe folder and exe count, and a Games / Every .exe switch. 64-bit exes
  are preferred as a title's default; 32-bit ones launch through the WoW64
  series below (the settings sheet marks them experimental).
- `FixedBaseImage` (`GameLibrary.swift`): a 64-bit exe linked /FIXED below
  4 GB (Crysis `Bin64\Crysis64.exe`, base 0x37000000) cannot be placed on iOS
  and Wine refuses to move it (c0000018). Before launch the library rebuilds
  its relocation table from the aligned in-image pointers in its data
  sections, adds it as a `.mreloc` section, clears RELOCS_STRIPPED and keeps
  the original as `<exe>.madeira-orig`. Validated against the real `.reloc` of
  41 x64 binaries; images with a writable executable section are skipped.
- Session logs (`LogStore.startSessionLog`): every launch also names its log
  `Documents/logs/<exe>-<yyyy-MM-dd_HH-mm-ss>.txt` -- a hard link to
  madeira-log.txt, so every writer's lines land in both and the next
  launch's rotation leaves the finished run under its own name. The newest
  40 are kept.
- Game Mode (`GCSupportsGameMode`, games category) in Info.plist.
- Settings > Experimental > Storage-backed memory writes `swap-mb = 3072` to
  `Documents/madeira.cfg`, which turns on upstream's file-backed guest data tier
  (virtual_ios.c ml1077). Measured on an iPhone 17 Pro Max / iOS 27.0 with this
  fork's earlier implementation: 256 MB of dirtied file-backed memory moved
  phys_footprint by 0 MB against +256 MB anonymous.
- Bundle identifier `com.willfaust.mythicemu`, so an installed copy keeps its
  container (Wine prefix, library, covers) across updates.
