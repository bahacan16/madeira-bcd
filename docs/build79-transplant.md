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

### The emulator itself is not the difference, and this was measured too

On 2026-09-22 the user pulled `arm64ec-windows/xtajit64.dll` out of the same
working IPA. It is a different binary from either of ours, but the comparison
says the lineage is shared and the code is very nearly the same:

| | Build 79 | ours (built in CI) | ours (committed) |
|---|---|---|---|
| size | 5,386,240 | 5,348,352 | 5,410,816 |
| `.text` virtual size | **0x1ffcf6** | **0x1ffcf6** | 0x205cf6 |
| x18 references | 121 | 120 | 115 |
| SectionAlignment | 0x1000 | 0x4000 | 0x1000 |
| version string | `Madeira-pinned-053c385` | 053c385 submodule | `FEX-2607-55-gac555dd` |
| build id | `rev=ml755 compiled Sep  9 2026` | — | `rev=ml755 compiled Aug 27 2026` |
| clang | 22.1.4 (`3599050`) | same | same |

The `.text` sizes agreeing to the byte, out of two megabytes, is not a
coincidence a different source tree produces. Same pinned FEX, same Madeira
revision ml755, same llvm-mingw. So swapping this binary in the way `ntdll.dll`
was swapped would buy very little -- and the committed one, which is a
different lineage, would be a step backwards. We keep building ours.

What the comparison did surface is that our build carries two changes of our
own that Build 79 does not:

* **the per-block suspend check, patched out.** Added 2026-09-14 and labelled
  "EXPERIMENT (revert = delete this block)", because the `brk #0xCAFE` it
  plants was firing and FEX's repack afterwards produced PC=0. Build 79 emits
  the check and runs Fallout and Stray, and transplant 11 has since fixed the
  repack's cause. Removed 2026-09-22.
* **`--section-alignment=0x4000`.** Build 79's binary is 0x1000-aligned, which
  means `.data` lands off iOS's 16KB grid and `ios_jit_data_align_delta`
  shifts the whole pool copy -- and it works anyway. Removed 2026-09-22. It was
  added to avoid that shift; 003731d then measured that the shift was a side
  effect and not the cause of anything. What settled it was the first
  fingerprint run: the flag puts `.text` at RVA 0x4000 instead of 0x1000, which
  changes every PC-relative constant in two megabytes of code, and **7711 of
  8189 chunks differed** from a binary built from the same source. A layout
  preference is not worth that.

`tools/xtajit-fingerprint.py` holds Build 79's binary as per-chunk hashes
(`tools/ref/xtajit64-build79.fp`, 256-byte chunks over `.text`) and compares
our build against it on every run. The runner cannot send us its artifact --
its egress reaches GitHub and nothing else -- so the step prints the differing
chunks' bytes into the build log, where they can be disassembled against the
reference off-runner. It never fails the build: a build that legitimately
differs should say so and carry on.

### The two halves of DXMT in our IPA are from different revisions

On 2026-09-22 the user put the whole working Build 79 IPA in a release, which
made the comparison exhaustive rather than file by file. Version 0.34.7,
build 79, `com.willfaust.mythicemu` -- the IPA the runtime patches came from.

**259 of the 266 Windows DLLs are byte-identical to ours.** Every one of
`aarch64-windows` and `arm64ec-windows` matches except seven files:

| file | ours | Build 79 |
|---|---|---|
| `aarch64/d3d10core.dll` | 524,288 | 1,937,408 |
| `aarch64/d3d11.dll` | 8,654,848 | 34,414,592 |
| `aarch64/dxgi.dll` | 1,835,008 | 5,894,144 |
| `aarch64/winemetal.dll` | 135,168 | 204,800 |
| `arm64ec/d3d11.dll` | 5,398,528 | 35,291,136 |
| `arm64ec/dxgi.dll` | 1,695,744 | 6,127,616 |
| `arm64ec/xtajit64.dll` | 5,410,816 | 5,386,240 |

`nls/` is identical; `prefix-template.tar.gz` differs by one added file,
`system32/xaudio2_7.dll`. There is no pile of missing DLLs. Six of the seven
are DXMT.

Most of that size gap is DWARF -- Build 79's DXMT is unstripped -- but the
code differs too, and in a direction that names the problem. Their
`arm64ec/d3d11.dll` `.text` is **61,440 bytes smaller** than ours, and their
`winemetal.dll` carries airconv's reflection symbols (`MTL_SM50_SHADER_ARGUMENT_*`,
`MTL_GEOMETRY_SHADER_PASS_THROUGH`, `ArgumentTableQwords`) which ours does not.
Code moved out of d3d11 and into winemetal between the two builds.

Now put that next to what we link. DXMT is two halves: the unix half is
compiled from `research/dxmt` and linked into the Madeira binary, and the PE
half ships as committed binaries that nothing here builds -- the PE build was
dropped as "redundant" in commit 4184816. The submodule is pinned at
`b4b89f0`, which is **the tip of `willfaust/dxmt` ios-port**; there is nothing
newer upstream, so Build 79's DLLs and our unix half come from the same
source. But `b4b89f0` landed 2026-08-29 and our committed DLLs are dated
2026-08-27, and those reflection symbols are in the submodule's
`airconv_public.h`.

So our IPA pairs a unix half from `b4b89f0` with a PE half from before it.
That is not a version anyone tested; it is a version nobody built. And the
guest now dies with `d3d11.dll` loaded and on the terminal stack.

`tools/import-verified-runtime.py` takes the PE half out of the verified IPA
until we build it here. The IPA and every file are checked against
`tools/ref/build79-runtime.manifest`, so a replaced release asset fails the
build rather than quietly changing what ships. Building the PE half from the
pinned submodule, the way `xtajit64.dll` is built, remains the right end
state -- and now there is a measurement saying it matters.

### What is *not* different, which was worth checking

Their host binary carries diagnostics our `build/` overlay has no trace of --
`[shadow] ml760`, `[hot-lock] ml444`, `[lock-orphan] ml447`, `[alert-storm]
ml439`, `[acc-take] ml480`. That reads like 200 revisions of host-side work we
are missing, and it is not: they live in the pinned submodules we already
build. `[lock-orphan]` and `[hot-lock]` are in `wine/dlls/ntdll/unix/sync.c`
at `7817e22`; `[shadow]` is `research/dxmt/src/winemetal/unix/winemetal_unix.c`
at `b4b89f0`. Our own sources carry revision markers up to ml786 against their
binary's ml762. The host side is not behind.

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
