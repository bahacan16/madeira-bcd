# madeira-bcd: what this fork adds to willfaust/Madeira

Rebased on upstream `5a82d39` (2026-09-24) on 2026-09-24. Upstream carries the
runtime (Wine, FEX, DXMT, the native D3D12 runtime); this fork carries a CI
build and the app-side pieces below. Everything else is upstream's.

## CI build (`.github/workflows/build-ipa.yml`)

Builds an unsigned IPA on a GitHub macOS runner from a clean checkout: Wine's
unix side, wineserver, win32u, FEX's iOS archives, LLVM 15 for iOS, DXMT's unix
half and the app. The PE-side DLLs (ntdll, xtajit64, DXMT, the D3D12 runtime)
are upstream's tracked builds. The Microsoft VC++ runtime is fetched from
Microsoft at build time and never committed. Archived as Debug, per upstream's
docs/BUILDING.md.

**Metal Shader Converter headers.** The D3D12 runtime's unix half compiles
against Apple's converter headers, which upstream does not track. With them
under `build/madeira-d3d12/msc-include/` CI builds the real conversion service;
without them `build/madeira-d3d12/madeira_ir_stub.c` answers every conversion
with `MADEIRA_IR_NO_DYLIB`, so D3D12 pipelines fail by name and D3D11 is
unaffected.

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
  mouse (ported from SaimSuhailQu/Madeira 72ca339).
- Game Mode (`GCSupportsGameMode`, games category) in Info.plist.
- Settings > Experimental > Storage-backed memory writes `swap-mb = 3072` to
  `Documents/madeira.cfg`, which turns on upstream's file-backed guest data tier
  (virtual_ios.c ml1077). Measured on an iPhone 17 Pro Max / iOS 27.0 with this
  fork's earlier implementation: 256 MB of dirtied file-backed memory moved
  phys_footprint by 0 MB against +256 MB anonymous.
- Bundle identifier `com.willfaust.mythicemu`, so an installed copy keeps its
  container (Wine prefix, library, covers) across updates.
