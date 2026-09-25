# Wine fork: LGPL branch provenance (DRAFT 2026-09-16, for review)

**Problem.** The fork on `wine` HEAD (branch used by the superproject)
applied LGPL-2.1 section 3 to convert the whole copy to GPL-3.0-or-later
(commit 0224441925d). That conversion is irreversible for that copy and
makes upstream Wine authors' code GPL-only there, so no exception for
Apple's proprietary converter can attach to it.

**Solution prepared.** Branch `madeira-lgpl` in the `wine` repository,
built as: upstream tag `wine-11.4` + the 51 Madeira commits below
cherry-picked in order with `-x` (skipping only the conversion commit) +
one commit adding LGPL `LICENSE-MADEIRA.md` and `CONTRIBUTING.md`.
All 51 applied without conflict. ADOPTED 2026-09-16: the wine submodule now points at `madeira-lgpl`
(a1e4a040484). A diff against the retired GPL branch differs only in
licence-notice lines (22629 lines swapped, equal counts, no code change).

**Authorship.** Every one of the 51 commits carries the author
`Will Faust <willtechnoduck@gmail.com>`. Git authorship is evidence of
origin, not proof of ownership. OWNERSHIP CONFIRMATION: pending an explicit
statement by the author that no patch below was adapted from third-party
code except as noted (requested 2026-09-16):
- `dlls/wineios.drv/coreaudio.c`, `coremidi.c`, `coreaudio.h`, `unixlib.h`
  derive from upstream Wine's `winecoreaudio.drv` (Copyright CodeWeavers,
  Huw Davies) and keep those notices. Their licence is upstream's LGPL.
- Notice-line touches outside the conversion commit: 8 lines across the
  series (headers copied when files were derived); none change a licence.

**Baseline:** wine-11.4 (upstream). **Commits (oldest first):**

- e544ca5eda5 2026-04-28 .gitignore: exclude in-tree build dirs (build-macos, build-arm64ec)
- 119bbbec067 2026-04-28 arm64ec_process_init: re-run update_hybrid_metadata after globals are set
- e81b8060e75 2026-04-28 ntdll/loader: apply arm64ec_redirect_ptr to imports at load time
- 3d1a183912d 2026-04-29 EC5c: arm64ec_process_init early-stash + iOS skip start.exe fallback
- 97a93087440 2026-04-29 loader: process_attach xtajit64 dependency tree before arm64ec_process_init
- b06e11567d5 2026-04-29 loader: redirect ARM64EC EntryPoint via arm64ec_redirect_ptr
- 49bcfb48173 2026-04-29 arm64ec: split process_init into dispatchers + ProcessInit; redirect ffwd thunks
- 07d3e009ded 2026-04-29 loader: trace load_arm64ec_module + arm64ec_process_init progression
- c6fc2f3915a 2026-04-29 env: skip start.exe fallback for AMD64 main on ARM64EC iOS
- 1da757ad07f 2026-04-30 arm64ec: re-patch all ARM64EC modules' hybrid metadata after dispatcher globals are set
- 89992f5f76f 2026-05-07 ARM64EC import binding: 3 iOS-port fixes
- f7412bdecfe 2026-05-08 ARM64EC iOS: translate redirect_ptr results to JIT-pool alias
- 5ec17759cdf 2026-05-08 ntdll: bounds-check RtlIsEcCode against non-canonical pointers
- e7048eee391 2026-05-08 ntdll: log thread-TLS allocation once for iOS bringup diagnostics
- 06e9dcc7822 2026-05-09 ntdll: pre-allocate main EXE TLS slot before load_arm64ec_module
- 519b098579e 2026-05-11 ntdll: add unix_ios_push_jit_aliases bridge for iOS FEX alias awareness
- d19b1d24a4a 2026-07-08 iOS build: catch-up commit — 2 months of accumulated fork work (S0 TLS → rpcss → task-21)
- 0c8f1426105 2026-07-08 iOS: WIP CoreAudio/CoreMIDI wineios.drv (experimental, not yet wired into build)
- 6c7286a6134 2026-07-09 Mythic: 🎮 FEX-2607 — EC-ntdll fixes: xtajit64 ctor-init + NLS casemap + is_ec_code per-thread peb
- d3e7c616517 2026-07-10 Mythic: ntdll ARM64EC — NULL-guard the KiUserExceptionDispatcher fast path
- c58cf755c11 2026-07-10 Mythic iOS: Steam S3 — kernelbase no-Win8-lie + arm64ec redirect Destination-RVA validation
- a2de48bbac5 2026-07-10 Mythic iOS: task #32 — stop_thread via Mach context capture (POSIX signal suspend is dead on iOS)
- 5004daea1fe 2026-07-11 iOS: EC exception dispatch across three stacks + [rtcs] guest-Rip conversion trace
- 71b042a5b63 2026-07-28 iOS: guard bulk non-exec protect notifications on all three paths
- 46a77d60990 2026-07-28 iOS: rpcrt4 diagnostics for the ccontext == 0x10 crash
- 82bee454550 2026-07-29 iOS: delay-load ARM64EC redirect; document why arm64x_check_call keeps the x18 read
- 7b20a40df07 2026-07-30 iOS: end-of-stack is not an invalid stack — make guest faults survivable
- ecb295e87c6 2026-07-31 ios: delay-load POOL-STALE closed — dual pool-side slot write + gated eager delay-import resolution
- 78aab076315 2026-07-31 ios: SEH unwind livelock breaker
- c154286bf61 2026-07-31 iOS-Mythic: wininet urlcache loop kills, [dll-missing] probe, user32 GetPointerDevice
- 1bebdf8aa96 2026-08-02 iOS: Crashpad-VEH context guard + EC exception hardening (ml418-ml432)
- 73fc9ef2e26 2026-08-04 iOS-Mythic: #79 TCP state numbering, NSI bypass, minidump gate, alert-probe TEB guard
- 712d8e6f191 2026-08-04 iOS-Mythic ml487/ml488 (#78): checksum the bytes NtReadFile hands back for steamui *.js
- 70d2028f1d7 2026-08-04 iOS-Mythic ml493 (#61, #79): glyph-rasterisation and connect-outcome probes
- e07db484d3a 2026-08-05 iOS-Mythic ml506-ml508: blit censuses — where Chromium's pixels do NOT come from
- f2fa8d3b883 2026-08-08 Mythic: free_async_queue UAF fix + wineserver fd-ownership/kill tracing
- 58b9db22108 2026-08-11 wine: ARM64EC SEH unwind fixes + JIT alias three-view probe
- a8ac939b45f 2026-08-12 wine: hand FEX the Mono backpatcher bridge
- 78497aaf273 2026-08-14 ntdll: critical-section and failed-open diagnostics for iOS
- a0ccfbe4651 2026-08-16 server: add ios_thread_wait_links() for wait-chain inspection
- 25b00c4700e 2026-08-16 ntdll/arm64ec: fix x64 setjmp/longjmp by unwinding a PE pc, not a JIT-pool pc
- 7f458f5b2e7 2026-08-16 loader: [tls-life] probe for TLS slot assignment and callback invocation
- ae1335b3d09 2026-08-17 ntdll: export ios_teb_tsd_offset so ARM64EC modules stop hardcoding a TSD slot
- 9dac3664912 2026-08-21 ntdll: register images at the loader boundary so child processes can execute x86-64
- 4cda511efe1 2026-08-21 Report thread context from the syscall frame, and stop capped probes hiding dependencies
- 022e61874dc 2026-08-22 Suspend threads for real instead of only snapshotting their registers
- daeb4ee6bb6 2026-08-25 Add opt-in native-call tracing for a video decoder and its stream
- fa8655ead45 2026-08-25 Extend native-call tracing to the audio backend and report what the hook sees
- ca6f153cf9e 2026-08-25 Rename the project from Mythic to Madeira
- 7817e220384 2026-08-28 Print the arena band selector's deferred log from dispatcher init
- abf22e09603 2026-09-16 ntdll ARM64EC: root-frame unwinding, TLS window, arena hand-over and loader traces

## After adoption

Later changes are committed on `madeira-lgpl` directly. Third-party
contributions keep their authors' copyright under LGPL-2.1-or-later and are
signed off under the DCO (the fork's `CONTRIBUTING.md`):

- feb96ad2be4 2026-09-25 xinput: read Madeira host controller snapshots
  through win32u. Author: 125hz. willfaust/wine pull request #1, merged as
  815cf1f92e2.
