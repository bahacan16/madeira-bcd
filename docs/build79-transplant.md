# Build 79 runtime-fix transplant

Madeira Build 79 runs Fallout and Stray on the same device this fork dies on.
Its GPLv3 corresponding-source package pins the *same* upstream revisions we
do — DXMT `b4b89f0`, FEX `053c385`, Wine `7817e22`, Madeira `97e2ce26` — so the
difference between a build that runs x64 games and one that does not is not the
base tree. It is `release-source/runtime-patches/`, thirteen numbered fixes the
developer wrote on top.

Our `build/` overlay was byte-identical to that pinned base everywhere except
`virtual_ios.c` and `signal_arm64_ios.c`, and in both the difference was our own
work (the SP-align trampoline rewrite and the diagnostic probes), not missing
theirs. So this is a transplant, not a migration: their fixes onto our tree.

## Method

Every patch is applied **verbatim** — the developer's text, not a reimplementation.
Direct `patch(1)` is unavailable in this environment, so each change is made with
an editor and then *proved* equivalent: a scratch script independently rebuilds
what the unified diff would produce from the pristine pinned file, and the result
is compared byte for byte. Where a file carries none of our own changes the whole
file matches; where it does, the patched region matches and the line delta is
accounted for.

Patches that land in a submodule (`FEX`, `wine`) cannot be committed — both point
at `willfaust/*`, which we cannot push to. Those ride in as idempotent CI scripts
under `tools/`, applied during the build, each failing the build by name if the
pinned tree ever moves. This is the same mechanism the repository already used
for `tools/patch-fex-link-log.py`.

## Status

| # | Fix | Target | State |
|---|---|---|---|
| 01 | ntdll static TLS isolation | wine `dlls/ntdll/loader.c` | **binary swapped in** — see below |
| 02 | CPUID per-CPU metadata index | FEX `CPUID.cpp`/`.h` | applied · `tools/patch-fex-cpuid-index.py` |
| 03 | module TLS | wine `dlls/ntdll/loader.c` | **binary swapped in** — see below |
| 04 | CPUID assembly check | — | not a patch; it is 02's binary verification harness |
| 05 | FEX runtime TEB | FEX `Module.S` | applied · `tools/patch-fex-teb-macro.py` |
| 05b | SRW orphan guard | wine `sync.c` | same hunk as 13 |
| 06 | native TSD bitmap | `build/ntdll-unix/virtual_ios.c` | applied in tree |
| 07 | guest exit | — | no patch file in the package |
| 08 | fd-cache exit | `build/ntdll-unix/server_ios.c` | applied in tree |
| 09 | async cancellation | wine `server/async.c` | applied · `tools/patch-wine-async-cancel.py` |
| 10 | APC signalling | `build/wineserver/mach_ios.c` | applied in tree |
| 11 | APC context | `build/ntdll-unix/signal_arm64_ios.c` | applied in tree |
| 12 | decommit edge pages | `build/ntdll-unix/virtual_ios.c` | applied in tree |
| 13 | SRW orphan | wine `sync.c` | applied · `tools/patch-wine-srw-orphan.py` |

### 05 and 06 belong together

06 explains 05's symptom. `data_map` is a bitmap, one bit per word, and
`virtual_ios.c` reads it correctly in both x18 passes (3794, 3921) — but the
TEB-retarget pass between them indexed it as a byte array (3888), so it called
most words literal-pool data and skipped them. That is why `found N static TSD
read(s)` is absent from 15 of 17 device logs: the pass ran and matched nothing,
leaving `Module.S` reading TSD slot `0x898` when the device publishes `0x8d0`.
05 removes the dependency on that pass; 06 removes the bug in it.

### 01 and 03 are the difference, and this was measured

On 2026-09-22 the user captured two logs from the same device, same game,
minutes apart: one from the developer's working Build 79 IPA, one from ours.
The working one ran to `[PRESENT_GAP] #1024` without a single fault. Ours died.
The tag sequences diverge on exactly one thing:

    working build:   err:module:alloc_module_tls_slot   (three times)
    ours:            absent

`alloc_module_tls_slot` does not exist in the unpatched tree. Patch 03 adds it:

    03-module-tls-fix/module-tls.patch:38:
      +static NTSTATUS alloc_module_tls_slot( LDR_DATA_TABLE_ENTRY *mod )

Comparing the binaries confirmed it. The `aarch64-windows` ntdll.dll is
**byte-identical** to ours, so the difference is entirely in the arm64ec copy,
which is the one whose loader sets up the guest's TLS:

| | working | ours |
|---|---|---|
| size | 4,259,840 | 1,572,864 |
| `alloc_module_tls_slot` | 3 | 0 |
| `alloc_tls_slot` | 2 | 1 |
| exports | 1469 | 1469 (identical set) |
| `.text` / `.data` / `.pdata` | 468K / 50K / 17K | same |

The size gap is six DWARF sections, not code: the working binary is unstripped.
Code sections match to the kilobyte, and `.rdata` differs by 1K -- consistent
with the same wine build plus these two patches, not a different lineage.

This matters because without them ntdll's own TLS index stays zero, which
Madeira allocates to the main executable, so wine's exception and unwind code
reads the executable's TLS block instead of its own. Our 2026-09-22 crash was a
`br x11` to 0x159c00000 -- the ARM64EC CPU area, a data structure -- from inside
a JIT block, with the correct value (`EnterEC=0x1587fc138`) printed on the same
log line.

`tools/check-ntdll-tls.py` tests the shipped binary for the marker so this can
never silently regress, and the workflow runs it on every build.

### How 01 and 03 got in

Not as patches. The user pulled `arm64ec-windows/ntdll.dll` out of the
developer's working Build 79 IPA — which they have, and which is GPLv3 with its
corresponding source published and held here — and committed it in place of
ours. That is exactly what the developer's own `build_r6.py` does: start from a
verified IPA and replace components.

This is a stopgap, and it should be said plainly. We now ship a 4 MB binary we
did not build, so we cannot change anything in wine's PE-side loader without
going back to the same source. Building `ntdll.dll` from the pinned wine tree
with llvm-mingw in CI remains the right end state; it would let 01 and 03 be
applied as patches like the other eleven, and would unblock every future
PE-side fix. The measurement above is what makes it worth doing rather than
speculative.

### Why they cannot be applied as patches here

Both patch wine's **PE-side** `dlls/ntdll/loader.c`, and this repository ships
`ntdll.dll` as a committed binary for both `aarch64-windows` and
`arm64ec-windows`. The workflow's wine cross-configure only runs `make include`;
no PE DLL is built from source. Applying 01/03 therefore requires building
`ntdll.dll` with llvm-mingw in CI — which does not exist yet.

They also apply in sequence: 03's hunks begin exactly where 01's end, so 01 must
land first.

Worth building. It unblocks these two and every future wine PE-side fix, and 01
is about ARM64EC static TLS isolation — the loader's initial-ntdll path never
registers ntdll's own 96-byte TLS directory, so its TLS index stays zero, which
Madeira allocates to the main executable. Wine's exception and unwind code then
reads the executable's TLS block instead of its own. That is adjacent to the
transition machinery we have been chasing.

### One thing to resolve with the developer

09's patch adds an inline comment reading *"Proposed isolated replacement, not
installed"*, while the same directory's README opens *"reviewed and installed"*.
Because the patch itself adds that line, Build 79's own `async.c` carries it too,
so it reads as drafting residue rather than a statement about the shipped build.
Kept verbatim rather than tidied — fidelity is the point — but it will mislead
the next reader.

## Not transplanted

`build79-exact-embedded-source/ResolutionSource/` is app-side work this fork has
none of: `TouchControls`, `ControllerInput`, `AudioClock`, `ResolutionInterpose`,
`MadeiraIPadUI`. Whatever makes Stray and Fallout *playable* rather than merely
launching is likely in there. It is a separate piece of work from the runtime
fixes and should wait until the runtime is known good.
