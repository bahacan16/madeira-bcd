# madeira-bcd: what this fork adds to willfaust/Madeira

Rebased on upstream `5a82d39` (2026-09-24) on 2026-09-24; tracks upstream main by merge. Upstream carries the
runtime (Wine, FEX, DXMT, the native D3D12 runtime); this fork carries a CI
build and the app-side pieces below. Everything else is upstream's.

## CI build (`.github/workflows/build-ipa.yml`)

Builds an unsigned IPA on a GitHub macOS runner from a clean checkout: Wine's
unix side, wineserver, win32u, FEX's iOS archives, LLVM 15 for iOS, DXMT's unix
half and the app. The PE-side DLLs (ntdll, xtajit64, DXMT, the D3D12 runtime)
are upstream's tracked builds. The Microsoft VC++ runtime is fetched from
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

## Runtime

- `NtFlushInstructionCache` (`build/ntdll-unix/virtual_ios.c`) invalidates the
  icache under `WINE_IOS` regardless of `HAVE___CLEAR_CACHE`. CI's generated
  config.h does not define it, and without the flush every x64 guest died in a
  FEX JIT block tail (fault PCs all 64-byte aligned).

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
- Game Mode (`GCSupportsGameMode`, games category) in Info.plist.
- Settings > Experimental > Storage-backed memory writes `swap-mb = 3072` to
  `Documents/madeira.cfg`, which turns on upstream's file-backed guest data tier
  (virtual_ios.c ml1077). Measured on an iPhone 17 Pro Max / iOS 27.0 with this
  fork's earlier implementation: 256 MB of dirtied file-backed memory moved
  phys_footprint by 0 MB against +256 MB anonymous.
- Bundle identifier `com.willfaust.mythicemu`, so an installed copy keeps its
  container (Wine prefix, library, covers) across updates.
