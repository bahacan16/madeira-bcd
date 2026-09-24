# RDR2 bring-up — handoff

**Date:** 2026-09-16
**Current build:** `Madeira-ml942-revert.ipa` (== ml940 behaviour; ml941 reverted)
**Status:** blocked in an anti-tamper DLL's `DllMain`, on a genuine ARM64EC
compatibility gap. One fix attempt failed and was reverted; the replacement
design has an open question I want a second opinion on before spending runs.

Everything below is from the vphone VM (192.168.64.9), virtual desktop,
`explorer /desktop=shell,960x540`.

---

## 1. Where execution actually is

```
explorer (0024)
 └─ RDR2.exe            (007c)  loads 47 modules, spawns the launcher, exits(0)
     └─ Launcher.exe    (0084)  runs, re-launches the game
         └─ RDR2.exe    (008c)  <-- DIES HERE
                                loader_init: "EMP.dll" failed to initialize
                                status c000001d (STATUS_ILLEGAL_INSTRUCTION)
```

Note the asymmetry, it matters: **instance 1 completes `loader_init` fully,
including `EMP.dll`'s `DllMain`.** Only instance 2 fails — it is launched with
cmdline `RDR2.exe` by the launcher, and `EMP.dll` takes its "this is the real
launch" branch, which does the heavy unpacking work. So the failure is not "the
anti-tamper DLL cannot initialise", it is "one branch of its init hits a gap".

---

## 2. The current wall (I am confident in this diagnosis)

`EMP.dll` installs **x64 inline hooks on ARM64EC exports** and the patched code
is then executed natively as ARM64.

Evidence chain, all from one run:

```
[exec-req] addr=0x12FC724C0 size=6 new_prot=40    <- make 6 bytes RWX
<write>                                            <- plant an x64 branch
[exec-req] addr=0x12FC724C0 size=6 new_prot=20    <- restore to RX
ILL at pc=0x12fc724c0
[rip-leak] guest RIP ... = PE 0xe6a706400 (module base 0xe6a6e0000 rva 0x26400)
```

`0xe6a6e0000` is **kernelbase.dll** — the `C:\windows\sysx64` copy, which is the
ARM64EC DLL farm (`SizeOfImage 0x2f0000`; the plain-ARM64 system32 copy is
`0x170000`). Sizes 5 and 6 bytes, protect→RWX→write→restore, is a textbook
inline hook.

I resolved the two patched RVAs against the shipped DLL. They are **exactly the
fast-forward-sequence jmp targets** of:

| patched rva | export |
|---|---|
| `0x26400` | `LoadLibraryA` |
| `0x1e2c4` | `GetFileAttributesW` |

So `EMP.dll` resolves the export (gets the x64 FFS — correct), then **follows the
`e9 rel32`** to find the "real" implementation, which is what hook engines do on
x64 Windows to see through forwarders and API sets. On ARM64EC that jmp lands on
ARM64 code, so it writes an x64 branch over ARM64 instructions, and the next
native caller executes them as ARM64 → SIGILL.

### Ruled out by reading the code, not by assumption

- **IAT binding** is already gated correctly — `import_dll` only redirects when
  the *importing* module is ARM64EC, with a comment saying exactly why. EMP's
  IAT is fine.
- **Delay-load** binding is gated the same way.
- **`GetProcAddress`** returns the raw export-table address (the FFS) — no
  redirect. `find_ordinal_export` has no EC handling at all.

So nothing upstream is wrong. The gap is that the FFS `jmp` has nothing
patchable behind it.

---

## 3. The failed fix (ml941) and precisely why it failed

**Design:** repoint each FFS jmp at a per-thunk landing pad in the module's own
`.hexpthk` slack:

```
FFS:  mov rax,rsp; mov [rax+20h],rbx; push rbp; pop rbp; jmp PAD
PAD:  90 x19          <- single-byte NOPs: any hook resume offset <=19 is a
      e9 <rel32>         valid boundary, then jmp to the real ARM64 body
```

Tool: `build/tools/ec-ffs-pad.py` (kept — the mechanics are sound and verified).
Pads go **inside the image** deliberately: both the PE mapping and its JIT-pool
copy execute the same bytes, so only an image-relative jmp is correct in both.

**Result: regression.** Instance 1 died at `unix_calls=76`, before finishing
`loader_init`; `Launcher.exe` never spawned. Fault address `0xe9909090` is
literally `90 90 90 e9` — the sled executed as an ARM64 instruction —
at kernelbase rva `0x8c970`, inside the pad region.

**Why my reasoning was wrong, stated plainly.** I checked that Wine's
`arm64ec_redirect_ptr` consults `RedirectionMetadata` (FFS rva → body rva)
*before* byte-decoding an FFS, verified **1329/1329** coverage for kernelbase,
and encoded that as the tool's hard precondition. That invariant is real and it
held. It just was not the binding one: **something else in the stack decodes a
fast-forward sequence without consulting that metadata**, and with the pad
inserted it took the pad to be the ARM64 target and branched to it natively.

⚠️ **Which decoder is not yet established.** FEX's ARM64EC dispatch is my
leading suspect — it has its own FFS recognition — but I have NOT verified that,
and I am flagging it as a suspicion rather than a finding precisely because the
last thing I asserted without checking is what broke this run. `arm64x_check_call`
and the `[x86-ptr]`/`[iat-sync]` pointer machinery in `virtual_ios.c` are equally
plausible. What IS established: the pad was reached and executed natively, at
kernelbase rva `0x8c970`, so at least one such path exists.

I verified one decoder and generalised to "all decoders". That is the mistake.

---

## 4. The open question I want a second opinion on

v2 would make the pad a **full duplicate FFS** instead of a NOP sled:

```
PAD:  mov rax,rsp; mov [rax+20h],rbx; push rbp; pop rbp; jmp <real body>
```

Then any decoder that lands on the pad recognises it and follows one more hop.

The problem is that two readers want different bytes **at the same offset**:

| reader | reads | wants at pad+0 |
|---|---|---|
| hook engine | writes 5–6 bytes at pad+0 | single-byte NOPs, so any resume offset works |
| FFS decoders | read pad+0 | the FFS signature, to follow to the body |

You cannot satisfy both at offset 0. v2 picks the decoders and therefore depends
on the hook engine **relocating whole instructions** rather than a fixed byte
count: it must copy `mov rax,rsp` (3) + `mov [rax+20h],rbx` (4) = 7 bytes and
resume at pad+7 (`push rbp; pop rbp; jmp body`). If it instead resumes at pad+5
or pad+6 it lands mid-instruction and we are back to garbage.

**What I cannot determine offline:** how EMP's engine computes its resume
offset. The protect sizes (5 and 6) tell me the patch width, not the relocation
logic.

Mitigating argument: real ARM64EC Windows depends on exactly this property — the
FFS exists so x64 hooks can patch it — so an engine that gets it wrong would
break on real Windows on ARM too. That is an argument, not evidence.

### Questions for Astra

1. Is there a third option that avoids the offset collision entirely? e.g.
   teaching every FFS decoder to skip a pad, rather than making the pad look
   like an FFS. That inverts the problem: one change per decoder, but no
   dependence on the hook engine's behaviour.
2. Is the set of FFS decoders enumerable with confidence? I know of Wine's
   `arm64ec_redirect_ptr` (`wine/dlls/ntdll/signal_arm64ec.c:473`) and FEX's
   dispatch. Are there others — the `[x86-ptr]`/`[iat-sync]` pointer machinery
   in `virtual_ios.c`, `arm64x_check_call`, the EC code bitmap consumers?
3. Is intercepting the hook *write* a better layer? We already trap guest writes
   to EC code pages (the W^X / UNALIGNED-BACKPATCH machinery). Detecting "an x64
   branch is being planted on an ARM64EC body that has an FFS" and redirecting
   the patch to the FFS is possible in principle, but the hook's own trampoline
   would still resume at body+N, so I think it does not close.

**Next action I had planned before writing this:** enumerate every FFS decoder
in the stack offline and confirm each follows a chain, *before* building v2.
Not doing that is what cost the ml941 run.

---

## 5. What is solid (do not re-litigate)

Four walls cleared this session, all general accuracy bugs, none game-specific.

### ml937 — a self-terminating process killed the whole app
`abort_process()` called `_exit()`. On iOS every Windows process is a
pseudo-process inside one Mach task and `_exit` is **not** shimmed the way
`exit()` is, so any guest calling `TerminateProcess(GetCurrentProcess(), code)`
erased the task — exit code 0, no crash report, log just stops.
Fix: route through `process_exit_wrapper` (`build/ntdll-unix/thread_ios.c`).
Device-verified: fired twice, app survived both.

### ml936/ml938 — an image whose preferred base is below 4GB
`EMP.dll` wants `ImageBase 0x13000000`, `RELOCS_STRIPPED=1`, `DYNAMIC_BASE=0`,
and its `.reloc` is **20 DIR64 fixups for a 0x20f000 image**, all inside its own
unpacker section, while `.rdata` alone holds 729 aligned 64-bit pointers into
the image. So it is neither relocatable nor placeable.

**iOS mandates a 4GB `__PAGEZERO`.** Measured four ways:
- app with 128MB or 16KB pagezero → SIGKILL at exec, no crash report;
- standalone binary, same → SIGKILL;
- standalone with a 16KB pagezero and **every segment forced above 4GB**
  (`-segaddr __TEXT/__DATA_CONST/__LINKEDIT 0x104000000+`) → **also SIGKILL**, so
  it is the pagezero size, not segment placement;
- in a live 4GB process, `mach_vm_allocate(ANYWHERE, hint=0x4000)` returns
  `0x104638000`; FIXED at `0x13000000 … 0x104000000` all `KERN_INVALID_ADDRESS`;
  first success `0x110000000`.

No process on this OS can have a lower floor, so a helper task does not help
either. ⛔ Do not retry this ladder.

**Fix: emulate the mapping instead of creating it.** ml938 registers a
"sub-floor window" for any image whose preferred base is below the floor, and
the fault handler translates the faulting address into the image's real mapping,
emulates the instruction, and steps over. Same memory, so the two views cannot
diverge, and the image is not modified. Required writing the load/atomic
emulation my own notes recorded as missing
(`reference_str_emulator_state.md`: *"All loads … LDAR / LDAXR / LDAPR — no
emulator path"*). Device-verified; fault volume is small, not a storm.

### ml940 — the signal path cannot resume into JIT code
ml938 initially hooked `segv_handler`. The emulation was correct but every
resume corrupted FEX: `[CALLRET_OOB] UNDERFLOW callret_sp=<pc+4>`, then a
rip-leak, then `c000001d`.

Cause: iOS sigreturn always zeroes x18, so a signal resuming at a JIT-pool PC
must detour through the per-thread trampoline
(`ldr x18,[pc,#-8]; br x17`) — which carries the resume address in **x17** and
never restores it. **x17 is FEX's callret stack pointer.** This is general: any
signal-resumed fault at a pool PC corrupts FEX state.

Fix: move the service into the Mach exception handler, which resumes via
`thread_set_state` (restores x18 directly, no trampoline). That is also why the
pre-existing unaligned-atomic emulator lives there and not in
`ios_emulate_store`. Device-verified: `CALLRET_OOB` 2 → 0.

---

## 6. Reproduction

```sh
# VM (see vphone-cli-setup/VM-ACCESS.md; IP changes per boot)
sshpass -p alpine ssh -p 22222 -o StrictHostKeyChecking=no root@192.168.64.9

# pull the log (no scp on the guest; guest PATH is minimal)
sshpass -p alpine ssh -p 22222 root@192.168.64.9 \
  'export PATH=/var/jb/usr/bin:/var/jb/usr/sbin:/var/jb/bin:/var/jb/sbin:$PATH;
   base64 /var/mobile/Containers/Data/Application/5BC39EAE-.../Documents/madeira-log.txt' \
  | base64 -d > run.txt
```

Grep markers worth knowing:

| marker | meaning |
|---|---|
| `[subfloor] ml938 window #N` | a sub-floor image was registered |
| `[subfloor] ml939 serviced N ... via=mach\|segv` | which handler serviced it |
| `[subfloor] ml939 via=... UNHANDLED encoding 0x…` | an instruction to add |
| `[CALLRET_OOB]` | FEX callret corruption (should be 0) |
| `[rip-leak]` | guest RIP holding a pool address |
| `[exec-req] addr=… new_prot=40` | a guest hot-patch in progress |
| `mixed-arch fallback:` | an AMD64 consumer got the sysx64 (EC) DLL |

Build chain: `build/ntdll-unix/build.sh`, `build/wineserver/build.sh`, then
`xcodebuild -project app/Madeira.xcodeproj -scheme Madeira -configuration Debug`.
Verify every artifact **by content** (`grep -ac <marker>`), never by exit status.
⚠️ The prefix's `C:\windows\sysx64\*.dll` are **symlinks into the app bundle**,
so shipping a modified EC DLL only needs an app rebuild.

⚠️ `ec-ffs-pad.py` edits a **git-tracked binary in place**. If `kernelbase.dll`
is ever rebuilt from `wine/build-arm64ec` the transform is silently lost. It must
become a build step before it is trusted.

---

## 7. Uncommitted work

Nothing is committed. Working tree carries ml936–ml940 plus the parked UE5 work:

```
build/ntdll-unix/thread_ios.c        ml937
build/ntdll-unix/signal_arm64_ios.c  ml938 ml939  (registry, load/atomic emulation, Mach hook)
build/ntdll-unix/virtual_ios.c       ml938        (window registration)
build/wineserver/mapping_ios.c       ml936        (sub-floor relocation)
build/tools/ec-ffs-pad.py            ml941        (untracked; transform reverted, tool kept)
```

---

## 8. OUTCOME — Astra's primary-IAT fix works (ml943, 2026-09-16)

Astra's diagnosis was correct and superseded §2–§4 above. I verified all four
load-bearing claims independently before applying anything:

| Claim | Verified how |
|---|---|
| Fault rva is `0x264c0` = **LoadLibraryExW**, not `0x26400` | `0x12fc724c0 − 0x12fc4c000`; FFS-target map |
| `0x94000029` @ rva `0x2641c` is a real `BL` → `0x264c0` | decoded: opcode `0x25`, target matches |
| kernel32 exports these as `ff 25` stubs into its primary IAT | slots `0x50f20` / `0x50f10` / `0x50850` |
| `arm64x_check_call` returns the FFS dest unreclassified | read the asm: `add x11,#14; add x11,w9,sxtw; ret` |

⚠️ **Both pad designs are dead**, by that last row. §3–§4's v2 proposal is
withdrawn — do not build it.

⚠️ **My §2 attribution was wrong.** The overwritten function is LoadLibraryExW;
LoadLibraryA merely calls it. I had dismissed the `BL` at `0x2641c` as a
coincidental byte pattern while I still believed sysx64 was the x86-64 farm, and
never revisited that after correcting the farm identification. The route is the
**primary IAT of kernel32's public `FF 25` export stubs**, not FFS-jmp-following.

### Applied

Astra's patch, with two changes: the classifier renamed to
`wine/dlls/ntdll/arm64ec_x64_export_iat.c` /
`arm64ec_iat_slot_is_x64_export` (keep game names out of the Wine tree), and the
canary folded into the real run as a capped `ml943 [x64-iat]` probe in the
selector, so one launch verifies both the selection and the preserved target.

Build note: `make -C dlls/ntdll` in `wine/build-arm64ec` needs **bison ≥ 3**
(`/opt/homebrew/opt/bison/bin`); macOS's 2.3 cannot parse widl's `%code`.
🔑 **Correction to `reference_build_and_deploy_chains`:** strip does NOT yield a
file exactly `SizeOfImage`. Measured `0x120000` stripped vs `SizeOfImage
0x140000`. The pad target is what matters: `SizeOfImage + 0x50000` = 1,638,400,
byte-for-byte the previously shipped size. Section table identical except
`.rdata` +0xbc and `.pdata` +0x10 — one added function's worth.

### Result

| Prediction | Outcome |
|---|---|
| Hooks land on canonical FFS | ✅ rva `0x82bc0` and `0x81840` |
| Patch width becomes whole instructions | ✅ 7 bytes (was 5/6) |
| `EMP.dll` initialises | ✅ zero `failed to initialize` |

`loader_init` completed; instance 2 ran 14,119 log lines vs 6,774. Selector fired
124 times across 9 modules. **No sign of the ml237 variadic-RPC regression** that
the eager redirect originally fixed — explorer, services.exe and rpcss all came
up normally.

### Next wall: an unpopulated ARM64EC entry-thunk slot

```
call_site@(lr-16): aa1c03e1 d2800003 58014184 d63f0080
                   mov x1,x28   mov x3,#0   ldr x4,<lit>   blr x4
[thunk-slot] WEDGE pc=0x0 lr_exec=0x1378843d0 ... x4=0x0 x11=0x167bb0000
```

`x4 = 0`: the literal holding the call target is zero, so `blr x4` lands on 0 and
guest RIP becomes 0. Two identifications:

- `0x167bb0000` is **not** a function pointer — it is this thread's
  `ChpeV2CpuAreaInfo` (per the `[cpu-area]` line).
- `lr=0x1378843d0` is just past `EnterEC=0x137884138`, so the call is inside our
  **EC entry-thunk machinery**.

Fatal because **EMP.dll installs a VEH** (`EMP.dll+0x37c00`) that returns
`EXCEPTION_CONTINUE_EXECUTION`, so the fault recurs until the `[redeliv]` guard
terminates the pseudo-process at 2,000 identical redeliveries. The app survives
(ml937).

This is now entirely our own machinery, and `[thunk-slot] WEDGE` is a
pre-existing probe, so there should be prior context on this failure mode.

---

## 9. Open: a VEH continue resumes the guest at RIP 0 (ml944/ml945)

**Confidence: LOW on the mechanism. High on the reproduction.** Writing this up
rather than guessing again; §3's lesson repeated itself once already below.

### Reproduction (stable across ml943 and ml944 runs)

`EMP.dll` initialises, `loader_init` completes, then in instance 2:

1. **Root fault (the game's own).** `ldrb w2, [x25], #1` — a post-index byte
   walk, i.e. a string scan — faults reading `0x204d5221`, with
   `rax = 0x204d5241` = ASCII **`"ARM "`**. Guest RIP is valid code
   (`[guest-rip] ... code address is VALID`). So the guest walked a string and
   dereferenced ASCII as a pointer. Suggestive of something
   architecture-related that we report differently from what it expects;
   NOT investigated yet.
2. **EMP's VEH asks to recover.** Handler at `<EMP base>+0x37c00` returns
   `0xffffffff` (`EXCEPTION_CONTINUE_EXECUTION`) with
   `Rip = <EMP base>+0x37ce0` — a valid address. **60 such continues per run**,
   every one requesting that same Rip.
3. **The guest resumes at RIP 0.** `CompileBlock: REFUSING low/invalid RIP=0x0`
   fires **2001** times; FEX's dispatcher branches to the refusal's 0 return
   (`ldr x4,<lit>; blr x4`), the VEH re-enters, and the `[redeliv]` guard
   terminates the pseudo-process at 2000 identical redeliveries. App survives
   (ml937). Also seen once: `REFUSING ... RIP=0x170`.

### What is established

- The resume path, read end to end: `call_vectored_handlers` →
  `NtContinue` → `context_x64_to_arm` → `syscall_NtContinue` →
  unix `NtContinueEx` → `signal_set_full_context` →
  `NtSetContextThread(self)` → `is_ec_code(frame->pc)?` → bounce via
  `KiUserEmulationDispatcher` (`mov x0,sp; bl dispatch_emulation`) →
  `context_arm_to_x64` (Pc→Rip) → `pBeginSimulation()`.
- ⛔ **Context layout is NOT the problem.** I proposed a Pc/Sp aliasing mismatch;
  it is wrong. `ARM64EC_NT_CONTEXT.Pc` is at `0x0f8` annotated `(Rip)` and `Sp`
  at `0x098` annotated `(Rsp)`, so `context_x64_to_arm` reads the right fields.
- **The `KiUserEmulationDispatcher` bounce is never taken: 0 of 60 continues**
  (ml944 probe, and `[ec-bounce-pool]` also 0).
- `ios_is_arm64ec_cur()` should be TRUE here — it requires the current
  pseudo-process to be AMD64, and `[proc-ident] Machine=0x8664` confirms that.
- Therefore, **if** `signal_set_full_context` ran, `is_ec_code(frame->pc)` was
  TRUE at the decision — i.e. `frame->pc` was NOT the guest's x64 Rip.
- There is a sibling bug already fixed here: **ml420**, same `is_ec_code` branch,
  where a pool-address resume got bounced and the guest then executed FEX's own
  emitted code. Ours is the PE-address case and takes the other branch.

### What is NOT established

- Whether `signal_set_full_context` runs at all on this path.
- Whether `NtSetContextThread(self)` applies `Pc` — if the handler's
  `ContextFlags` lacks `CONTEXT_CONTROL`, `frame->pc` would keep its native EC
  value, which would explain `is_ec_code` being TRUE and no bounce.
- Where RIP 0 actually comes from.

### 🔑 Probe-design lesson (cost one run)

ml944 logged **inside** the bounce branch. The bounce was never taken, so the
probe was silent and the run produced nothing: **I gated the instrument on the
hypothesis it was meant to test.** ml945 moves it to the decision itself,
unconditional, logging `req pc/sp/flags`, `status`, `frame pc/sp`,
`is_ec_code(frame->pc)` and `ios_is_arm64ec_cur()`. A wrong hypothesis now still
yields data. Generalises the existing note in
`feedback_probe_and_diagnosis_discipline`: *probe the decision, not the branch.*

### Two amplifiers worth fixing on merit, independent of the cause

1. **`CompileBlock`'s refusal returns 0** and FEX's dispatcher branches to the
   returned value unconditionally, so a clean diagnosable refusal becomes a
   branch to 0. It should raise a guest exception instead. Deliberately NOT
   changed yet: it would alter the failure signature being measured.
2. **The `[thunk-slot] WEDGE` probe misleads here.** Its comment and its
   `exec-relative slot @…` arithmetic assume the `$iexit_thunk$` shape
   (`adrp x8; ldr x16,[x8,#0x480]; blr x16`). The actual faulting site is a
   literal-pool load in FEX's dispatcher (`ldr x4,<lit>; blr x4`, imm19=2572),
   so that computed line is derived from the wrong encoding. Do not trust it.

### Build state

`Madeira-ml945-setctx.ipa` carries ml936–ml943 plus the ml945 decision probe.
Only the unix ntdll changed for ml944/ml945, so no EC PE ntdll rebuild was
needed after ml943.

### §9 update (ml945 run): one refutation, one more sampling error

**Refuted: `NtSetContextThread(self)` DOES apply the requested Pc.** All 24
samples show `req pc == frame pc`, with `flags=0x00400007`
(CONTROL|INTEGER|FLOATING_POINT), so `CONTEXT_CONTROL` is present and honoured.
The §9 "prime suspect" is dead.

**Also refuted: my inference that `is_ec_code(frame->pc)` must have been TRUE.**
The conjunction `ios_is_arm64ec_cur() && !is_ec_code(frame->pc)` was false via
the *other* term: every sample reported `is_ec=0` **and `ec_cur=0`**. For those
threads `ec_cur=0` is correct — they are ARM64 pseudo-processes, and
`ios_is_arm64ec_cur()` requires `ios_cur_image_info()->Machine == AMD64`. So the
branch was skipped for a legitimate reason on the threads sampled.

**Still unknown:** the state at the VEH continue itself. The probe never sampled
it.

🔑 **Second probe-design error, different from the first.** ml945 was
unconditional (fixing ml944's gating) but used a **flat 24-shot cap**. The
budget was spent by log line **1547**; the resume of interest is at line
**5571**. Worse, 22 of the 24 were the same `req pc` — thread-start resumes on
a hot path. A flat cap samples whatever happens *first*, which for a rare event
behind a hot path is never the event. ml946 keys the probe on **distinct
`req pc`** (48 slots, one line per unseen target, plus tid), so a thousand
identical thread starts cost one slot and the VEH's continue target is
guaranteed one. ⛔ Do not "fix" this class by raising a flat cap.

Combined rule now worth generalising: **probe the decision, not the branch; and
key the budget on the value in question, not on call count.**

Minor correction: the VEH handler is confirmed `EMP.dll+0x37c00`. It looked
otherwise for one run only because EMP.dll is mapped twice at different bases
(once per RDR2 instance) and a `grep -o` picked the wrong instance's base.

### §9 update 2 (ml946 run): the RIP is delivered correctly — loss is downstream

⚠️ **First, a retraction that invalidated the previous two updates.** Probe-hit
counts per pulled log:

| log | lines | ml944 | ml945 | ml946 |
|---|---|---|---|---|
| rdr9  | 14154 | **0** | **0** | 0 |
| rdr10 | 13979 | **12** | 24 | 0 |
| rdr11 | 14559 | **12** | 0 | 8 |

**rdr9 contained none of the probes — it was a stale IPA, not the ml944 build.**
So "the KiUserEmulationDispatcher bounce is never taken, 0 of 60" was concluded
from a log that could not have shown it. The bounce was being taken all along;
ml944 fired 12× in rdr10 and I never looked, having already accepted the false
conclusion.

🔑 **Process guard, added because this cost two runs:** before interpreting the
ABSENCE of a probe, confirm the installed build on the device carries its
marker —
`ssh … 'grep -ac <marker> <bundle>/Madeira.debug.dylib'`. This was done in
earlier rounds and skipped here. Absence of a probe string is not absence of the
event; it is first of all evidence about which build ran. (Companion to
`feedback_probe_and_diagnosis_discipline`'s "absence of a probe STRING != absence
of the FAILURE".)

**CONFIDENT — measured 12/12:**

```
ml944 #1 requested pc=0xeac1d7ce0 | frame pc=0xeac1d7ce0
         handoff ctx=0x169eaec10 pc=0xeac1d7ce0 | is_ec=0
```

The VEH's requested Rip (`EMP.dll+0x37ce0`) arrives **intact** at the
`KiUserEmulationDispatcher` handoff. Every hop under our control is exonerated:
`context_x64_to_arm`, `NtSetContextThread(self)`, `NtGetContextThread`, and the
handoff context. **The loss is strictly downstream** —
`dispatch_emulation` → `context_arm_to_x64` → `pBeginSimulation()`.
Also confirmed: `ec_cur=1`, `is_ec(frame->pc)=0`, so the branch is correctly
chosen; §9's earlier reasoning about this branch is moot.

**CONFIDENT — second-order corruption.** SP across the 12 bounces:

```
#1  sp=0x169eaefa0   valid guest stack
#2  sp=0x169eae260   valid guest stack
#3  sp=0xeae6c0000   PE-band garbage
#4..#12 sp=0xeae6c0000  stuck
```

From the third continue the guest SP is bogus, and the handoff `CONTEXT` is then
placed at `(sp - sizeof(CONTEXT)) & ~15` = `0xeae6bfc70` — inside that bogus
region, i.e. we scribble a CONTEXT into unknown memory. Worth fixing on merit
(refuse the bounce when SP is not a plausible stack) regardless of the root
cause.

**NOT CONFIDENT — leading hypothesis (ml947 tests it).** `dispatch_emulation`
does `context_arm_to_x64( get_arm64ec_cpu_area()->ContextAmd64, arm_ctx )` then
`pBeginSimulation()`. If the TEB is wrong at that instant, the context is written
into the wrong CPU area and simulation resumes from a stale `Rip` — which would
be 0. This thread repeatedly shows `x18=0` and
`no TEB owns sp … -> BEST-EFFORT delivery on guest stack`, so it is plausible,
but unproven. ml947 logs, at the bounce: `teb`, `ChpeV2CpuAreaInfo` (TEB+0x1788),
`ContextAmd64` (CpuArea+0x18) and that context's current `Rip` (+0xf8) — the
field BeginSimulation resumes from — for cross-check against the `[cpu-area]`
line logged for the same tid. Unix-side only, so no EC PE ntdll rebuild.

If the CPU area and ContextAmd64 are correct at the bounce, the remaining
suspects are `context_arm_to_x64`'s Pc→Rip mapping and FEX's `BeginSimulation`,
both of which would need PE-side/FEX instrumentation.

Build: `Madeira-ml947-cpuarea.ipa` (ml936–ml943 + ml944/ml947 probes).

### §9 update 3 (ml947 run): Wine-side plumbing is exonerated by measurement

Log verified as the ml947 build by fingerprint before reading
(ml944=12, ml946=8, **ml947=12**).

🔑 **Guard upgraded again.** The previous pull looked like a fresh ml947 run —
the installed dylib carried the `ml947` marker and the log's mtime was 16s after
the install — but it was the still-live ml946 process appending footprint lines.
Checking the *installed binary* answers "what is on disk", not "what produced
this log". **Fingerprint the LOG**: grep it for the expected marker, and if
absent, grep the previous builds' markers to identify which one ran. Same
failure shape as the rdr9 retraction, one level up.

**CONFIDENT (measured):**

1. **The TEB and CPU area are correct at the bounce.** My §9-update-2 hypothesis
   is refuted. Probe vs tid 0x8c's own init line:
   ```
   [cpu-area]  tid=0x8c teb=0x6fff20000 ... area=0x162370000
   ml947 #1..#12     teb=0x6fff20000 cpuarea=0x162370000 ContextAmd64=0x162370050
   ```
2. **`context_arm_to_x64` DOES install the requested Rip.** `ContextAmd64->Rip`
   reads back `0x6f61d7ce0` (= `EMP.dll+0x37ce0`, the VEH's requested target) on
   bounces #2–#12. (#1 reads `0x0` on *entry*, i.e. before the first write.)
3. Combined with update 2 (`requested pc == frame pc == handoff pc`), **every hop
   on the Wine/unix side is exonerated by measurement**: `context_x64_to_arm`,
   `NtSetContextThread(self)`, `NtGetContextThread`, the handoff context, the
   TEB, the CPU area, and `context_arm_to_x64`.
4. **The guest is never observed executing at the requested target.**
   `0x6f61d7ce0` appears only as `pc=`/`its_Rip=` in probe output — never as a
   `GuestRIP` or `BlockEntry`. What FEX actually simulates:
   ```
   GuestRIP=0x48d28a41   x1   -> NoExec, region 0x0+0x0 (wild pointer)
   REFUSING RIP=0x0      x1
   REFUSING RIP=0x170    x2000   <-- the storm
   ```
   `0x48d28a41` little-endian is `41 8a d2 48` = `mov dl, r10b` + a REX prefix,
   i.e. **a dword lifted out of an x86 instruction stream**, not a plausible
   address. It does not occur in EMP.dll's on-disk bytes (it is a packed image,
   so runtime code is a candidate source).

**NOT CONFIDENT — two readings, not yet separated:**

- **(A) FEX's simulation entry does not source Rip from `ContextAmd64`.**
  Everything up to `pBeginSimulation()` is measured correct, and the guest is
  never seen at the requested RIP. Suspect `BeginSimulation`/`EnterEC` using a
  stale `State.rip` (`ThreadState(ED1)`) instead. Note `process_ios.c:946`
  already records that "EnterEC storing an x64 target in State.rip is its job".
- **(B) The resume works and EMP's own recovery code bails.** `rax=0x7ffe02e8`
  at the fault is KUSER_SHARED_DATA-shaped — a timing read, which is exactly
  what anti-tamper recovery code does. EMP's handler could run at `+0x37ce0`,
  fail a check, and jump to garbage. Then the root cause is the ORIGINAL fault
  (§9 step 1: the `ldrb w2,[x25],#1` string walk with `rax = "ARM "`) and this
  whole VEH loop is a symptom.

⚠️ **Why fact 4 does not settle it:** `BlockEntry` is logged only on the NoExec
error path, not for successful block entries. So the absence of
`BlockEntry=0x6f61d7ce0` is consistent with (A) but does not prove the guest
never executed there. Do not treat it as proof — that is the same
absence-of-evidence trap that produced the rdr9 retraction.

**Probe that would separate them:** log the guest RIP at FEX's simulation entry
(or log the first N block compilations after a bounce, successful ones included).
If a block at `0x6f61d7ce0` is ever entered, it is (B) and the investigation
moves to the `"ARM "` misparse. If not, it is (A) and the investigation moves
into FEX's `EnterEC`/`BeginSimulation`. ⚠️ FEX's AGENTS.md forbids AI-generated
FEX contribution code — diagnosis and reading are fine; a FEX-side change is the
user's to make.

**Unexplained:** the dominant symptom is `RIP=0x170` repeated 2000 times, which
neither reading accounts for yet.

**Amplifiers still worth fixing on merit, independent of A/B:**
- `CompileBlock`'s refusal returns 0 and FEX's dispatcher branches to the
  returned value (`ldr x4,<lit>; blr x4`), turning a clean refusal into a branch
  to 0. Should raise a guest exception.
- The bounce places its `CONTEXT` at `(sp - sizeof(CONTEXT)) & ~15` using an SP
  that is garbage from continue #3 onward (`0xeae6c0000`-class, PE band), i.e. it
  scribbles a CONTEXT into unknown memory. Should refuse the bounce when SP is
  not a plausible stack.

---

## 10. Astra's relocation review supersedes §9 (ml948, 2026-09-16)

Astra's offline review
(`Codex/2026-08-05/users-willfaust-documents-ios-pc-game/RDR2-RESUME-AND-RELOCATION-REVIEW-2026-09-16.md`)
is correct and overturns three of my conclusions. Recording the retractions
first so nobody works from the dead theories.

### ⛔ Retracted

1. **"FEX ignores the requested RIP" (§9 reading A) — WRONG.** `EMP+0x37ce0` is
   the complete stub `add rsp,0x198 / ret`. The logged guest RSP advances by
   exactly `0x1a0` (= 0x198 + 8) after each recovery, repeatedly, in rdr10–13.
   The resume executes. Do not change `BeginSimulation`, and note FEX's source
   does load the supplied context (`SyncThreadContext` /
   `LoadStateFromECContext` copy Rip and Rsp under `CONTEXT_CONTROL`).
2. **The `"ARM "` string-parser theory (§9 step 1) — WRONG, and it was my own
   probe.** The guest RIP first became the wild value `0x204d5241`; only *then*
   did `[guest-bytes] ml631` fault at `0x204d5241 - 32 = 0x204d5221`. The
   `ldrb w2,[x25],#1` is that probe's byte-formatting loop, not game code. Same
   recurrence in rdr13 at `0x48d28a41 - 32`. **A diagnostic manufactured a
   finding and cost a whole line of investigation.**
3. **`RIP=0x170` is explained, not a mystery.** The FEX emulator stack is
   reserved at `0x6f8680000+0x40000`, top exactly `0x6f86c0000`; a
   `NtMapViewOfSection` at that same address creates a
   `CROSS_PROCESS_WORK_LIST`. Once a native fault leaks that stack top in as the
   guest RSP, `add rsp,0x198` reads the free-list entry at `0x30 + 9*0x28 =
   0x198`, whose `next` field is the previous entry's offset `0x170`. Downstream
   symptom of a wrong stack, not an independent bug.

### 🔑 The real lead: a shared-data pointer biased by the module delta

`bad address - relocation delta == 0x7ffe02e8` in **all six** saved runs, with
six different load bases (rdr8, rdr9, rdr10, rdr11, rdr12, rdr13). `0x7FFE02E8`
is canonical KUSER_SHARED_DATA, which must not move with a DLL. The failing
guest pair is:

```asm
EMP+0x169698: mov rbp, qword ptr [r10]
EMP+0x16969b: mov eax, dword ptr [rbp]     <-- faults; host insn ldapr w8,[x29] (a LOAD)
```

**Mechanism (verified independently here).** All 20 DIR64 fixup targets hold
**zero** on disk — I dumped them. Astra's addition is what makes that matter:
those zeros are the *immediates of `movabs reg, 0`*, consumed by later
arithmetic (RVA 0x120ab0 is the immediate of `movabs rax,0` followed by
`add rbp,rax`; 0x120b17 feeds a subtraction). So applying DIR64 does not
"relocate pointers" — it **injects the relocation delta as a constant** into the
packer's own address arithmetic. On Windows `RELOCS_STRIPPED` guarantees the
delta is 0 and every one of these is a no-op. ml936 made it non-zero.

⚠️ This also kills the §5/ml938 argument that "some internal pointers resolve
high, others low, both safe". The bad address `0x7631802e8` is **outside** the
sub-floor window `[0x13000000,0x1320f000)`, so the fault-service alias neither
recognises nor repairs it. Two aliases onto the same bytes are not equivalent
for pointer arithmetic, module identity, external addresses, instruction fetch,
or API parameters.

⛔ Do **not** "fix" this by subtracting the module delta from high addresses, and
do not silently skip the fixups and declare it correct — control-flow
consequences are untested. Astra has not identified the exact instruction that
first constructs the biased pointer; that is the open question.

### Shipped: ml948 — the diagnostic that crashed itself

`wine/dlls/ntdll/signal_arm64ec.c` `[guest-bytes] ml631` dereferenced
`(grip - 32)` directly, clipping `lim` only when the alias probe returned an
`end`. When the probe found nothing mapped — precisely the wild-RIP case this
telemetry exists for — it read 64 bytes from unmapped memory inside exception
dispatch and faulted recursively. Now copied through `NtReadVirtualMemory`
(status + short-read checked, underflow handled); the alias probe's `end` is a
clip hint only, never the safety check. 🔑 An alias-table hit is not a lifetime
guarantee — do not go back to query-then-dereference. The same pattern should be
applied to the three-view hash reads in the same block (still
query-then-dereference, though at least gated on a positive verdict).

Built EC PE ntdll: stripped `0x120000`, padded to `SizeOfImage + 0x50000` =
1,638,400. `Madeira-ml948-safedump.ipa`, ml943's IAT fix and ml944/ml947 probes
intact, kernelbase still unpadded.

### Next experiment (one variable): map high, do NOT apply the fixups

Rationale: with the fixups skipped, `movabs reg,0` stays 0, so the packer's
self-adjust arithmetic is a no-op *exactly as on Windows*; the image's
`0x13000000`-based absolutes stay low and are serviced by ml938's window; and
shared-data pointers are never biased. Keep the `ImageBase` rewrite (so anything
reading the header computes a zero delta). This is the ml937 gate I built and
then reverted — reinstate it for RELOCS_STRIPPED sub-floor images only.
Astra is right that this needs testing rather than assertion; a run is that test.

### §10 update (ml948 run): the fixed diagnostic immediately produced the evidence

Log fingerprinted by the reformatted `ml631` line — new format
`RX from -32, 64 of 64 bytes` ×2, old format 0 → this is the ml948 build. **The
dump no longer faults** (the ml948 failure path never fired, both reads
returned 64/64), so §10's recursive-fault bug is closed.

It paid for itself in one run. Dump #2 is at `RIP=0xc1630969b`, i.e. EMP base
`0xc161a0000` + **`0x16969b`** — the exact instruction Astra identified — and
now shows the site live:

```
offset 29..31:  49 8b 2a         mov rbp, qword ptr [r10]     ; +0x169698
offset 32..35:  8b 44 25 00      mov eax, dword ptr [rbp]     ; +0x16969b  FAULTS
offset 36..:    45 0f bf c8      movsx r9d, r8w
                49 81 c2 04 ..   add r10, 4
```

**Seventh consecutive confirmation of the relation.** This run:
`delta = 0xc161a0000 - 0x13000000 = 0xc031a0000`;
`0xc031a0000 + 0x7ffe02e8 = 0xc831802e8` = the observed bad address (6 hits).

**New datum that settles the mechanism.** At the fault, `r10 = 0x169adf1a8` —
the guest **stack** band, not EMP's image (`0xc161a0000`). So the biased pointer
is *computed and stored onto the stack* by the packer's own arithmetic; it is
not a relocated constant sitting in the image. That is the difference between
"we relocated a pointer we shouldn't have" and "we injected a delta into code
that then biases an external address" — the latter, which is Astra's reading.

Unchanged: EMP init 0 failures, 2001 `REFUSING` (1× `0x0`, 2000× `0x170`), one
`[redeliv] terminating`. Removing the spurious nested fault did not change the
outcome, consistent with the storm being downstream.

### Shipped: ml949 — map high, do not apply the directory

`build/ntdll-unix/virtual_ios.c`: when an image declares
`IMAGE_FILE_RELOCS_STRIPPED`, keep the high mapping and the `ImageBase` rewrite
but skip `process_relocation_block` entirely. Logs one line naming the image,
base, preferred base and delta. ml936, ml938 and ml943 all unchanged.

Expected if the reading is right: the delta never enters the packer's
arithmetic, no external address is biased, the `0x7ffe02e8 + delta` fault
disappears, and the image's own preferred-base absolutes are serviced by the
ml938 window as before. ⚠️ Astra's caution stands — the control-flow
consequences of not applying those 20 entries are untested, so a *different*
failure here is a real possibility and would still be informative.

`Madeira-ml949-noreloc.ipa`; verified in the bundle: ml949 present, ml948 EC
ntdll, ml943 IAT fix, ml938 window, ml936 force-reloc all intact.

---

## 11. ml949/ml950: the relocation fix is CONFIRMED; the window's limit is now the wall

### ✅ Astra's diagnosis is confirmed on device (ml949)

Skipping the directory for `RELOCS_STRIPPED` sub-floor images eliminated the
entire failure class, in one run:

| | 7 prior runs | ml949/ml950 |
|---|---|---|
| `0x7ffe02e8 + delta` fault | 6 per run | **0** |
| `REFUSING low/invalid RIP` | 2001 | **0** |
| `[redeliv] terminating` | 1 | **0** |

ml948 also proved itself: `ml948 #4 ... window [-32,+64) UNREADABLE (status
8000000d) — dump skipped, handler intact`. The diagnostic that used to fault
recursively now declines cleanly.

### ✅ One more bug of mine, fixed (ml950)

`[subfloor] ... UNHANDLED encoding 0xac400c22` — `LDP q,q,[Xn],#imm`, the
128-bit SIMD pair load the shared-cache memmove uses. My SIMD-pair branch was
supposed to cover it but the mask was **unsatisfiable**:
`0x2C000000 & 0x3A000000 == 0x28000000`, so the branch had been dead code since
it was written. Mask corrected to `0x3E000000` (pins `[29:27]=101`, `V=1`,
`[25]=0`; disjoint from the GPR pair test `0x3E400000/0x28400000`, which needs
`V=0`). Verified arithmetically before building. `0 unhandled` in the ml950 run.

🔑 Also retagged the `serviced` line to carry `ml950`, because the fix itself
lives only in a comment and the build would otherwise have had **no log
fingerprint** — the trap that cost two runs in §9.

### 🔴 The wall: a fault-service alias cannot answer an executability query

With the directory no longer applied, the packer's control flow now transfers to
**preferred-base addresses**, and that is where it stops:

```
[guest-state] rip=0x131031ae            <- guest running inside the window
[iOS-xquery] MISS tracker=... addr=0x13001240
NoExec instruction in entry block: 13001240
[guest-rip] 0x13001240 -> Wine has NO VIEW of this address
```

FEX does not fault here. `QueryGuestExecutableRange`
(`FEX/Source/Windows/ARM64EC/Module.cpp:753`) asks
`InvalidationTracker->QueryExecutableRange(Address)`; the tracker is built from
**actually-mapped executable sections**, nothing is mapped at `0x13001240`, so
it returns `Size == 0` and FEX refuses to build the entry block. ml938 emulates
individual data accesses on fault — there is no fault to intercept, only a
question to answer.

**This is exactly the limitation Astra named** in §10: a coherent model "must
cover instruction fetch/control flow, data accesses, module queries and pointer
translation across APIs. The current fault-only image alias is not that model."
§10's own caveat — that the control-flow consequences of skipping the fixups were
untested — has landed, and this is the untested consequence.

### The one plausible extension, and the judgement call

`QueryGuestExecutableRange` already carries an iOS-specific fallback of exactly
the right shape: reverse-translate a JIT-alias address to its PE original, query
the tracker with that, and forward-translate the returned base. A sub-floor
equivalent is mechanically the same — translate guest→real through the ml938
window, query, map the base back — and would need the window's
translate/reverse exported to FEX alongside the existing
`IosJitTranslate`/`IosJitReverseTranslate`.

⚠️ Note for Astra: the project's recorded position
(`feedback_fex_claude_md_does_not_apply`) is that FEX's "no AI code" line does
**not** apply to this private fork, so a FEX-side change is permitted here —
you declined out of caution, reasonably, but it is not a blocker.

**Open judgement call, deliberately not decided unilaterally.** Even with the
query answered, FEX must still *read* the code bytes at `0x13001240`, one
faulting access at a time (now possible: the LDP q fix covers memmove), and will
cache blocks and do SMC/invalidation tracking against a phantom RIP range. That
is a fourth approximation layer on an image that fundamentally wants real
placement. The alternative reading is that each layer buys one wall and the
model should be fixed properly (or the title parked) rather than extended again.
I have not built this; it needs a call on which of those two it is.

Current build: `Madeira-ml950-simdpair.ipa` — ml936/938/943/948/949/950 all
verified present. Failure is now deterministic and identical run to run
(same log lines), which makes it a clean baseline for whichever direction is taken.

### Secondary: the desktop wedge after the child dies

After RDR2 #2 and `Launcher.exe` exit, explorer (tid 0024) sits on two
unsignalled Events for ~51s with `(no server thread for tid 0024)`; 198
`[srv-stuck]` lines. This resembles the deferred
`project_child_init_death_wedges_desktop` bug. ⚠️ Not asserted — early in this
session that doc was correctly ruled out because the failure was the inverse
shape; now a child really does die leaving explorer waiting, so it may apply.
Independent of the RDR2 path and worth confirming separately.

---

## 12. ml951–ml953: EMP.dll INITIALISES. Crash converted to a wedge.

Astra's Stage 1 succeeded. Three builds, each one variable, each confirmed by
log fingerprint before interpretation.

### ml951 — executable-range query for sub-floor windows

| | ml950 | ml951 |
|---|---|---|
| `NoExec instruction in entry block` | present | **0** |
| `EMP.dll failed to initialize` | 1 | **0** |
| `code=c0000005` | 14 | **0** |

The fallback answers in the guest's own domain, e.g.
`addr=0x131031ae -> real=0xbde2a31ae exec range low=0x130fc000 size=0x1106fc
writable=false` — the `.emp1` section, permissions preserved, RIP never moved
high. 598,017 sub-floor accesses serviced, 0 unhandled. **Stage 1's success
criterion (execution past the first refused block) is met.**

Implementation notes that matter:
- The window table is **deliberately separate from `IosAliasEntries`**, because
  `Module.S`'s `ExitFunctionEC` rewrites branch targets through that table —
  an entry there would relocate the guest RIP to the backing address, which
  Astra explicitly ruled out. (Repo already has a scar here: anon RX→RW aliases
  must never enter that table.)
- Registration reuses the existing alias callback with a **self-identifying
  discriminator** (a real JIT alias's PeBase is ≥4GB on iOS; a sub-floor base is
  <4GB), so no new export. A new `BTCpu64…` export would need a PE-side binding,
  and calling an ARM64EC PE entry point from native Mach-O code is the ml613
  crash.
- Off switch: `MADEIRA_NO_SUBFLOOR_XQUERY=1` stops the push, leaving FEX's table
  empty and the fallback inert. Checked unix-side because `getenv` is unsafe in
  FEX's early init (see `ios_fex_band_base` in `libarm64ecfex.def`).

### ml952/ml953 — KUSER_SHARED_DATA is a sub-floor window

With relocation fixed, the guest began reading `0x7ffe02e8` — the *correct*
KUSER_SHARED_DATA address, no delta — and took **2.3 million** Mach exceptions
at one pc. `user_shared_data` is declared at `0x7ffe0000`; our own code nulls it
and allocates it high because iOS cannot map there. Wine-internal readers use
the relocated pointer; guest code reading the **architectural constant** gets
nothing. General gap, not per-title.

ml952 registered the canonical page as a window — and it did nothing, because
the service hook sat **after every other path had declined**. An earlier handler
claimed the fault, resumed without fixing it, and looped silently (its own
diagnostics are capped). Evidence: the serviced counter froze at exactly 598,017
the moment the address switched, with no `UNHANDLED` line, on an encoding
(LDAPR, `0xb8bfc3a8`) the emulator covers.

🔑 **ml953 moved the hook to the FRONT of the EXC_BAD_ACCESS path.** Safe by
construction — the address is below iOS's 4GB floor, so nothing in the task can
be mapped there and no other handler can legitimately own it; anything outside a
registered window falls through with its ordering unchanged. This is the same
argument the segv-side gate already used. "Place it last to be conservative" was
wrong reasoning, not a conservative choice.

Result: `0x7ffe02e8` exception samples **1.32M → 0**, and the serviced counter
moved only 598,017 → 602,113 — about 4,000 reads, not millions. The spin was
waiting for a timestamp to advance and could not while the read faulted.

### Where it stands now (ml953)

- `EMP.dll failed to initialize` **0**; `c0000005` **0**; `UNHANDLED` **0**;
  `NtTerminateProcess` **1**. Nothing crashes and nothing terminates.
- RDR2.exe's own image is **50 MB dirty** at `0x172cf0000+0x3288000` — its 52 MB
  first `.text`, i.e. decrypted in place.
- ⚠️ But it is **not progressing**: footprint flat at 1159 MB, **no `.rpf` reads**,
  `0 top-level windows`. (Correcting my own first read: the 3,185 MB figure was
  `[phys-map] tag 0 totals res=`, reserved across bands including the 1 GB JIT
  pool — not the footprint.)

### 🔴 New wall: a blocking op under the fexlock hold

```
[census-hold] idx=21 port=0x1110f teb=0xdc1fd0000 tid=008c HOLDS fexlock @0xb610f0108
              run_state=1 susp=0 pc=0x12f2bf328 lr=0x12f3ce4ac
[thread-stacks] port=0x1110f pc=?+0x13689393c run=3 (TH_STATE_WAITING) cpu=0
thread-sample  teb=0xdc1fd0000 cpu=0% wait GUEST rip=0x131be5da
```

RDR2's main guest thread is parked at 0% CPU **holding fexlock**, with guest RIP
`0x131be5da` — inside the sub-floor window, i.e. in EMP's own code. 148
`[srv-stuck]` lines show explorer's threads (0024/0030/0038/003c) waiting
21–27 s behind it.

This is the documented **CodeInvalidationMutex / fexlock wedge** class:
`project_codeinvalidation_wedge_routes` — *every whole-app freeze is a blocking
op under a shared hold, which stops the JIT process-wide*, and its recorded
conclusion is **fix the hold discipline, not each route**. Route 1 was fixed in
ml618; this is a new route.

**Not yet known:** what the thread is blocked *on*. `NtWaitForMultipleObjects`
(7) and `NtWaitForSingleObject` (5) totals for the whole log are too low for
this to be an obvious Wine object wait, and 008c shows no `[srv-stuck]` entry of
its own, so it is likely a native/kernel wait reached from guest code rather
than a server wait. That is the next thing to identify, and it should be done
before touching the hold discipline — the documented lesson from that file is
that fixing routes individually does not converge.

Build: `Madeira-ml953-hookfirst.ipa`. ml936/938/943/948/949/950(→953)/951/952
all verified present.

---

## 13. ml954: the whole-app wedge is fixed. RDR2 now blocks in ReadFile.

### ✅ The fexlock wedge is gone

| | ml953 | ml954 |
|---|---|---|
| `HOLDS fexlock` | present | **0** |
| top-level windows | **0** | **7** |
| sub-floor faults | 598,017 | 462,849 |
| init failures / `c0000005` / `UNHANDLED` | 0 / 0 / 0 | 0 / 0 / 0 |

Explorer recovered and the virtual desktop is responsive; the user-visible
"freeze" is gone. Nothing crashes, nothing terminates.

### How §12's wedge was diagnosed (technique worth reusing)

The blocked thread's PCs were pool addresses. Reverse-mapping them through the
log's own `[jit-pool] image … → pool …` lines gave:

```
census-hold pc  0x12f2bf328 -> ntdll.dll + 0x63328        = memcpy + 0x180
census-hold lr  0x12f3ce4ac -> libarm64ecfex.dll + 0x274ac
last faults     pc = libarm64ecfex+0x27278, insn 0x38696900 = ldrb w0,[x8,xM]
```

🔑 **Naming an ARM64EC `.text` body needs the redirection metadata, not the
export table.** Every export points at an FFS thunk (rva ≥ `0x90000` in ntdll),
so nearest-export-below over the EAT finds nothing for a `.text` address. Invert
`RedirectionMetadata` (FFS rva → body rva) and look up the body instead.
⚠️ And treat a large offset as a failed match: `LdrShutdownThread+0x810` /
`LdrGetDllPath+0x1070` in the §13 backtrace are internal functions being
mis-attributed, not real hits. A small offset (`ReadFile+0xac`,
`memcpy+0x180`) is credible.

So FEX was copying guest code out of the sub-floor window **one byte at a time
through Mach exceptions** (~3 µs each) while holding `fexlock` — the documented
CodeInvalidationMutex shape, a blocking op under a shared hold stopping the JIT
process-wide.

### The fix: decode from backing memory, not through faults

`Decoder::AdjustAddrForSpecialRegion` (`FEXCore/Source/Interface/Core/
Frontend.cpp`) already performs exactly the required split for VSyscall:
`InstStream` holds the **guest** address while `AdjustedInstStream` — the
pointer every decoder byte read goes through (`ReadByte`/`ReadData`) — points at
**separate backing memory**. ml954 does the same for a sub-floor RIP:
`AdjustedInstStream = IosSubfloorToReal(RIP)`.

Guest address semantics are untouched: RIP arithmetic, block identity, SMC
tracking and every address the decoder reports still come from `InstStream`.
Only the byte source moves. This is Astra's suggested direction, implemented.

Residual: 462,849 faults remain, all **data** accesses from compiled guest code
reading its own low-address data. Inherent to the fault-service model for data,
and no longer fatal now that nothing holds the lock across them. If throughput
becomes the wall, that is the next thing to attack.

⚠️ Known edge: a decoded block spanning the window's end would read past the
backing. The VSyscall precedent has the same property and the window is a whole
image, so it is unlikely — but real.

🔑 Third repeat of the same trap: the ml954 code change emits **no string
literal**, so the build would have carried no log fingerprint. A capped
`[iOS-subfloor-decode] ml954 …` line was added for that reason alone. **Check
every build for a log-visible marker before shipping it.**

### 🔴 New wall: EMP blocks in ReadFile

```
[thread-stacks] pc=?+0x12f452c28 run=3 susp=0 cpu=0 x8=0x103 x18=0x0
  pc is PE code: kernelbase.dll+0x22c28   = ReadFile + 0xac
  bt[0] ?+0x130fdbec                       <- sub-floor frame, i.e. EMP code
  bt[1] PE ntdll.dll+0x323b4               <- internal, name untrustworthy
  bt[2] PE ntdll.dll+0x30044               <- internal, name untrustworthy
```

RDR2's thread sits in a synchronous `ReadFile` at 0% CPU. `Launcher.exe` is
**still alive** at the end of the run (late modmap:
`peb=0x8c9eb0000 modules=6 first=Launcher.exe@0x15c8c0000`), so this is not a
read on a dead peer's handle.

Still no `.rpf` reads and footprint flat at 1160 MB, so the engine proper has
not begun loading data.

**Not established:** what the handle is. The log carries 27 "pipe", 26
"ws2_32" and 14 "socket" mentions plus a single `NtReadFile`, so a pipe and a
socket are both plausible; no handle was captured. ⚠️ The thread's process was
inferred from the EMP frame in its backtrace, not from its TEB (`x18=0`).

**Next probe (ml955):** in `NtReadFile`, for a process that owns a sub-floor
window, log the handle value plus the server's object type and name, capped.
Each outcome points somewhere different — a socket implies Social Club
networking, a pipe implies launcher IPC, a plain file implies something about
the install. Identify it before changing anything.

## 14. §13 WAS WRONG. The wedge is my own fault handler faulting inside itself (ml956, 2026-09-17)

Astra reviewed rdr20/ml954 and refuted §13's conclusion. I verified all four of
its claims against the binaries before acting on any of them. **§13's "RDR2 now
blocks in ReadFile" is retracted** — there is no evidence of file I/O,
launcher IPC, or authentication being the blocker.

### 14.1 What ReadFile+0xac actually is

`kernelbase.dll` rva 0x22c28, disassembled from the shipped
`app/Madeira/arm64ec-windows/kernelbase.dll`:

```
22c1c  mov w8, #0x103        ; STATUS_PENDING
22c20  mov x0, x21
22c24  mov x2, xzr
22c28  str w8, [x26]         ; <== the sampled pc
...
22c44  blr x8                ; NtReadFile is called HERE -- never reached
```

Source is `wine/dlls/kernelbase/file.c:3619`, `io_status->Status = STATUS_PENDING;`
*before* the `NtReadFile` call. So the thread is **storing** the IOSB's initial
status and that store is faulting. `NtReadFile` is never entered.

The sampled `x8=0x103` in the same log line is the value being stored, not a
returned `STATUS_PENDING` from an async read. I had read it as the latter. The
register was in the log I already had; I did not connect it.

### 14.2 The exception thread is stopped in the emulator it uses to service faults

Same run, rdr20.txt:6820:

```
port=0xc203 "wine-x18-exc" pc=ios_emulate_store+0x70 run=3 susp=0 cpu=0
  bt[0] ios_subfloor_service+0x668
  bt[1] ios_mach_exception_thread+0x1710
```

Astra decoded `Madeira.debug.dylib` (not just the source): `ios_emulate_store`
= 0x2d9e18, and +0x70 = `str w9, [x2]`, where `x2` is the translated backing
address and `w9` the emulated value.

**The mechanism.** `ios_emulate_load` / `_store` / `_store_rel` dereference
their address argument naked. That contract was written for the W^X pool path,
where the caller had already resolved a known-writable RW anon alias.
`ios_subfloor_service` passes a raw PE **backing** address instead, which can
be read-only (`.text` / `.rdata`), stale, or unmapped. When that dereference
faults, the faulting thread **is** the Mach exception thread: its own exception
goes to the port that only it services, so it waits on itself for ever, and
every guest thread waiting for a fault reply parks behind it. Nothing is
logged, because a handler that faults inside itself never reaches its own error
path — so the absence of `c0000005` / `UNHANDLED` lines exonerates nothing.

This is the exact hazard flagged earlier in this session and not followed up:
"my emulators memcpy from rd_addr (the real backing address). If that read
faults … the Mach handler thread faults, and then nothing services anything →
every thread parks forever."

### 14.3 Offline findings (no run spent)

- **Window identities.** The two sub-floor windows are `0x7a800000+0x160000` =
  **opengl32.dll** and `0x13000000+0x20f000` = **EMP.dll** (sizes match the
  `[jit-pool]` lines exactly). All 462,849 serviced faults are against EMP.dll.
- **The two "latest serviced" encodings are loads**, decoded by hand:
  `0xb8bfc379` = `ldapr w25,[x27]`, `0x38bfc362` = `ldaprb w2,[x27]`. Consistent
  with FEX's TSO lowering. The load path is working; the wedge is a store.
- **Alias ownership is NOT the proximate cause in this run.** Both windows were
  RE-POINTED ("last loader wins") at 5135/5227, but pseudo-process `007c` had
  already exited at 4090, *before* `008c` re-loaded them. No cross-process
  aliasing occurred here. It remains a real latent hazard — nothing enforces
  that ordering — and stays on the Stage 2 list, but it did not cause this.
- **The `[data-align]` shift is exonerated.** `virtual_ios.c:8627` shifts the
  **JIT pool offset**, not the image mapping, so it cannot skew `real_base`.
- The ml951 xquery lines already record `writable=false` for EMP.dll's code
  range backing, which is consistent with a store into a non-writable section.

### 14.4 ml956 — the handler is now incapable of faulting

Two new helpers in `build/ntdll-unix/signal_arm64_ios.c`, ahead of
`ios_subfloor_service`:

- **`ios_subfloor_classify(insn, &bytes, &is_write)`** — decides what the
  access needs *without touching it*. Masks and order mirror the three
  emulators exactly, including their `opc == 0 -> return 0` guards, so a
  classification can never disagree with the emulator that will run. RMW
  atomics (CAS, SWP, LDADD/CLR/EOR/SET) are classified as writes.
- **`ios_subfloor_backing_probe(real, bytes, need_write, …)`** — walks every
  region the span touches with `mach_vm_region_recurse`, requiring mapped +
  READ (+ WRITE for stores). It rejects holes (query returns the first region
  *at or above* the address), span overflow, and zero-size non-progress.
  `mach_vm_region_recurse` deliberately, **not** `mach_vm_region`: the latter
  returns an object-name port on every call. The existing `mach_vm_region`
  sites in this file sit on rare diagnostic paths and leak it harmlessly, but
  this gate runs once per serviced fault — 462,849× in rdr20 — and would
  exhaust the task's port table.

`ios_subfloor_service` now (a) reads the faulting instruction via
`mach_vm_read_overwrite` instead of dereferencing `PC_sig` (same self-fault
class), (b) classifies, (c) probes, and (d) on failure **captures and
declines**. Declining lands on exactly the path an unknown encoding already
takes, so it cannot regress anything, and unlike a dereference it cannot wedge.

The refusal line is the capture Astra asked for, taken *before* any access —
a post-store success counter cannot expose a store that never returns:

```
[subfloor] ml956 via=%s REFUSED %s of %u byte(s): guest %#llx -> real %#llx
  insn=%#010x pc=%#llx | backing region %#llx+%#llx cur_prot=%#x max_prot=%#x
  kr=%d | anon_rw_alias=%#llx pool_copy=%#llx
```

Keyed on the distinct guest page, capped at 16 (ml945's lesson: a flat cap on a
hot path is spent before the interesting event). The serviced counter line is
retagged ml953 -> ml956 and now also reports the refused count.

### 14.5 The coherence trap the next build must resolve

`anon_rw_alias` and `pool_copy` are logged because they decide the real fix,
and they are in tension. The image has a **dual-mapped pool copy**
(`[jit-pool] image 0xb2e1a0000+0x20f000 (EMP.dll) -> pool 0x131aab000`) whose
RW alias is a *different copy of the bytes* from the backing — and ml954
deliberately made FEX read block bytes from the **backing**. So a
self-modifying guest store has two candidate destinations that must stay
coherent:

- write the **backing** → FEX's decoder sees it (ml954), the executing pool
  copy does not;
- write the **pool RW alias** → execution sees it, FEX's decoder does not.

Getting this wrong silently desynchronises code from what executes. This is why
ml956 captures rather than guesses, and why no RW-alias write is attempted yet.

### 14.6 Decision tree for the next run

- **`REFUSED WRITE`, `cur_prot=0x5` (R-X) or `0x1` (R--), `pool_copy` non-zero**
  → guest self-modifying / in-place unpack into a protected section. Fix =
  resolve the writable backing **and** invalidate FEX's cached blocks for the
  corresponding low guest address, for both aliases (Astra requirement 2 +
  Stage 2 code invalidation). Must decide backing-vs-pool per §14.5.
- **`REFUSED WRITE`, `kr != 0` or region base above the address** → backing is
  unmapped/stale. Points back at window ownership/lifetime (Stage 2), not
  permissions.
- **`REFUSED` with `cur_prot` already writable** → my classifier or span is
  wrong; fix the classifier, not the memory.
- **No `REFUSED` line and the run proceeds** → the store was legitimately
  serviceable and the wedge was elsewhere; read the new stop.
- **A genuine guest access violation** → deliver it to the *original* guest
  thread (Astra requirement 3). Declining is the bounded interim; a real
  guest-fault delivery is still owed.

### 14.7 Corrected: ml955's fd-type table

`enum server_fd_type` (`wine/include/wine/server_protocol.h:1922`) is
`INVALID, FILE, DIR, SOCKET, SERIAL, CHAR, DEVICE` — there is no `FD_TYPE_PIPE`
and no `FD_TYPE_MAILSLOT`. My table invented both, so it reported CHAR as
"PIPE" and DEVICE as "MAILSLOT". Now indexed by the enum constants themselves
so it cannot drift again. Note a named pipe end returns **FD_TYPE_DEVICE**
(`wine/server/named_pipe.c:1169`), and an fd class identifies neither the peer
nor any protocol — a socket is not evidence of authentication. Astra's other
limits on that probe (global 48-entry cache keyed on bare handle values, which
different pseudo-processes reuse) stand; it is a weak probe and §13's plan to
lean on it is withdrawn.

### 14.8 Still owed (Astra Stage 2)

1. Alias ownership across pseudo-processes — "last loader wins" is unsafe even
   though it did not fire in rdr20.
2. Invalidating low-address blocks when backing bytes change through either
   address (§14.5).
3. The fixture regression: execute / modify / re-execute, non-executable
   rejection, two-process isolation.
4. Real guest-fault delivery instead of declining (§14.6).
5. Audit the load path for the same class — rdr19 sampled the handler inside
   `memmove` via this service. ml956 gates loads too, so the next run tests it.

**Build:** `Madeira-ml956-subfloor-safe.ipa` (73,568,689 bytes, 345 files,
signed). Verified from inside the IPA: dylib carries 3 ml956 format strings +
ml936/938/949/951/952 and no `MAILSLOT`; EC `ntdll.dll` carries ml943/ml948 at
the padded 1,638,400; `xtajit64.dll` carries ml951/ml954.

## 15. rdr21/ml956 RESULT: the wedge is fixed and the blocker is named (ml957, 2026-09-17)

### 15.1 The gate worked

- Handler **never faulted**: 0 `DECLINING` (unreadable pc), 0 unhandled encodings.
- Serviced accesses **462,849 -> 471,041** (+8,192 past where rdr20 wedged).
- No whole-app freeze. The app failed fast with a diagnosis instead of hanging.
- Exactly **two** refusals, both on the same guest page, so the page cap printed
  one line — the probe behaved as designed.

### 15.2 The single captured refusal

```
[subfloor] ml956 via=mach REFUSED WRITE of 4 byte(s): guest 0x1303aaa0 ->
real 0xcac1daaa0 insn=0xb9000348 pc=0x12f3eac28 | backing region
0xcac1d4000+0x30000 cur_prot=0x1 max_prot=0x7 kr=0 | anon_rw_alias=0
pool_copy=0x131a7daa0
```

`insn=0xb9000348` decodes to **`str w8, [x26]`** (size=2, STR-unsigned-offset
family, opc=00 = store, imm12=0, Rn=26, Rt=8). `pc=0x12f3eac28` is kernelbase
rva **0x22c28 = ReadFile+0xac**. The SEGV dump corroborates: `x8=0x103`
(STATUS_PENDING), `x19=0x1303aaa0`, `rax=0x1303aaa0`. So this is precisely the
instruction Astra identified in §14.1, caught at the moment it faults.

`kr=0`: the backing is mapped. `cur_prot=0x1`: read-only. `max_prot=0x7`: we
are permitted to grant write.

### 15.3 EMP.dll's own section table settles whose bug it is

Parsed from the VM prefix copy of EMP.dll (machine 0x8664, RELOCS_STRIPPED,
ImageBase 0x13000000, 12 sections):

```
.data2  rva 0x01a000-0x031000  chars=0xc0000000  READ WRITE
.EMP    rva 0x031000-0x064000  chars=0xe0000020  CODE EXECUTE READ WRITE  <== rva 0x3aaa0
.data3  rva 0x064000-0x0fc000  chars=0xc0000000  READ WRITE
.emp1   rva 0x0fc000-0x20c6fc  chars=0x68000060  CODE INITIALIZED_DATA NOT_PAGED EXECUTE READ
```

The refused write is in **`.EMP`, an RWX self-modifying section**. The image
itself declares it writable, so the guest was entitled to that store and **we**
had taken the permission away. EMP.dll unpacks code into `.EMP` and is also
using it as scratch for the ReadFile IOSB.

Two things this rules out:

- **Not a missed protect call.** All 8 `NtProtectVirtualMemory` calls in the run
  are on high addresses; the guest never calls it on a sub-floor address. So
  "the guest made it RW and our window didn't forward the call" is refuted.
- **`.emp1` is correctly read-only.** It has no WRITE bit, so ml951's
  `writable=false` verdict for the exec range was right, and the 462,849 loads
  (all rva 0xff…, i.e. `.emp1`) need no change.

The measured region boundary confirms the mechanism: `.EMP` starts at rva
0x31000, which shares a 16 KB page with `.data2` (RW), so only rva
0x34000-0x64000 remained `R--` — exactly the 0x30000-byte region reported.

### 15.4 Root cause: an RWX section could not be granted write anywhere

`build/ntdll-unix/virtual_ios.c`, two guards that together strip the bit:

1. `mprotect_exec`'s write-granting `vm_protect` ladder is gated
   `if ((unix_prot & PROT_WRITE) && !(unix_prot & PROT_EXEC))` — so an RWX
   request skips it **for being executable**.
2. The image pool-copy path then ended with an unconditional
   `mprotect( base, size, PROT_READ ); return 0;` ("leave original code section
   as read-only") — discarding the caller's `PROT_WRITE` **for being code**.

`map_image_into_view` does pass the right thing (`prot |= PROT_WRITE` when
`IMAGE_SCN_MEM_WRITE`, virtual_ios.c:11914); both guards then dropped it.

### 15.5 ml957

The pool-copy path now honours a requested `PROT_WRITE`, using the same
`vm_protect` -> `vm_protect(+COPY)` -> `mprotect` ladder the non-exec path uses,
and keeps the read-only downgrade only for sections that did **not** ask for
write. Logged either way (`W RESTORED` / `could not be restored`).

This cannot create a W+X mapping: the backing never carries X — execution is
from the pool copy, a separate mapping — so it is R+W on a page with no X. And
for a sub-floor image the backing is the canonical byte source, since ml954
made FEX's decoder read block bytes from it, so a write that lands there is the
one FEX sees.

General, not per-game: any image section declaring `MEM_WRITE` keeps write on
its backing. Nothing about this is specific to EMP.dll or this title.

### 15.6 The remaining defect rdr21 exposed, and what is now latent

**Exposed.** Declining produced a *malformed* guest exception:
`c0000005 addr=0 ecRip=0`, then `CompileBlock: REFUSING low/invalid RIP=0x170`,
re-fault, 2000 identical redeliveries, and the `ml465` guard terminated the
process. Declining was the bounded interim; Astra requirement 3 (deliver a real
fault to the *original* guest thread, with a correct address and RIP) is now
due, and is what the crash-to-home-screen actually was.

**Latent.** `.EMP` is CODE as well as WRITE, so once writes land, the guest can
modify bytes FEX has already JITted. Invalidation is **not** wired up. The good
news is the sub-floor window makes it tractable: a guest write to a low address
still traps through `ios_subfloor_service` even with the backing writable, so
that is the hook point — via a FEX-registered callback in the ml951 style, not
a direct call into an ARM64EC export from native code. Do not attempt the
latter (see the ml613 rule).

Ordering rationale: ml957 alone can only improve on rdr21, where the write
never happened at all and the process died. It is one variable. If the next run
shows misexecution inside `.EMP`, that is the signal to wire invalidation.

**Build:** `Madeira-ml957-secwrite.ipa` (73,570,109 bytes, 345 files, signed).
Verified from inside the IPA: dylib carries both ml957 lines, 3 ml956 format
strings, ml936/938/949/951/952, no `MAILSLOT`; EC `ntdll.dll` ml943/ml948;
`xtajit64.dll` ml951/ml954.

## 16. rdr22: ml957 NEVER FIRED — the write is lost on a different path (ml958, 2026-09-17)

### 16.1 The fix was on the wrong exit

rdr22 is byte-for-byte the same failure as rdr21 (same rva 0x3aaa0, same
`cur_prot=0x1 max_prot=0x7`, 2 refused, 471,041 serviced), and **`ml957: 0`** —
zero occurrences in the log.

Checked the installed binary before drawing any conclusion, per the rdr9
lesson: the installed `Madeira.debug.dylib` on the VM contains **2 ml957
strings**, so the build was deployed. `dprintf` reliably reaches the log
(ml949/951/952/956 all appear). Therefore ml957's branch genuinely was never
taken — not a deploy failure.

### 16.2 Why: `mprotect_exec` copies the WHOLE image on the FIRST exec section

`[jit-pool] image 0xa8a1a0000+0x20f000 (EMP.dll) -> pool 0x131d73000` appears
**once** per load, for the whole 0x20f000 image. EMP.dll has three executable
sections (`.text`, `.EMP`, `.emp1`). The first one to call `mprotect_exec`
triggers the whole-image pool copy and falls through to the `return 0` at the
end of the image branch — the site ml957 patched, reached with `.text`'s prot
(R-X, no WRITE), so ml957's condition was false and its `else` ran.

The other two sections then call `mprotect_exec` again and hit the
**already-copied early-out** (`virtual_ios.c:8180`, `"iOS JIT: %p already in
mapping %d"`). That is where `.EMP` is handled, and it has its own
write-granting block — which failed.

### 16.3 Two defects on the early-out, both provable offline

```c
if (unix_prot & PROT_WRITE)
{
    kern_return_t kr = vm_protect(mach_task_self(),
        (vm_address_t)base, size, FALSE,
        VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);   /* +COPY, and ONLY +COPY */
    if (kr == KERN_SUCCESS) { ERR(...); return 0; }
    ERR("iOS vm_protect RW failed kr=%d ...");
}
mprotect( base, size, PROT_READ );
```

1. **Page alignment.** `base` is `view + sec->VirtualAddress`. PE section VAs
   are 4 KB-granular; this device's page is 16 KB. `.EMP` has VirtualAddress
   0x31000, so base was `0xa8a1d1000` — 4 KB-aligned, **not** 16 KB.
   `vm_protect` refuses an unaligned address. The measured read-only region
   was exactly `[0xa8a1d4000, 0xa8a204000)` — starting at the *next* 16 KB
   boundary — which is the signature of a protect call that could not act on
   the unaligned start.
2. **`+COPY` tried first and alone.** ml391 established the correct ladder for
   the non-exec write path: plain RW first, `+COPY` only as fallback, because
   `VM_PROT_COPY` forcibly privatizes the mapping object and silently
   disconnects a MAP_SHARED section view. This path never received that fix.
   rdr22 measured `max_prot=0x7`, so plain RW is permitted and is the correct
   request.

### 16.4 Why this was invisible for three runs

**Every diagnostic on this path is an `ERR`**, and `ERR` is swallowed by
MADEIRA_QUIET — the same trap the file already documents for `mprotect_exec`'s
own entry log ("Its existing ERR() output is swallowed by MADEIRA_QUIET, so log
via dprintf"). So the `"iOS vm_protect RW failed kr=%d"` line that would have
named this on day one never reached the log. ml958's diagnostics are `dprintf`.

Generalisable: on this codebase, **a diagnostic that matters must be dprintf**.
An ERR is a diagnostic that does not exist.

### 16.5 ml958

The early-out now rounds the range out to whole `vm_page_size` pages and tries
plain RW before `+COPY`, reporting both `kr` values, the original and aligned
ranges, and the verdict (`W GRANTED` / `still read-only`), capped at 24.

Rounding out can widen the grant into a partial page shared with a neighbouring
section — unavoidable with 16 KB pages against 4 KB section alignment, and for
`.EMP` both neighbours (`.data2`, `.data3`) are already READ|WRITE. The
alternative is the protect failing entirely, which is what was happening.

ml957 is kept: forcing a MEM_WRITE section's backing to `PROT_READ` is wrong on
that path too, it is simply not the path this image takes.

### 16.6 What the next run distinguishes

- **`ml958 ... W GRANTED`** and no ml956 refusal -> the store lands; read the
  next stop. §15.6's latent code-invalidation hazard becomes live.
- **`ml958 ... plain RW kr=0`** but a refusal still at rva 0x3aaa0 -> the grant
  did not stick (silent iOS downgrade); needs a post-grant region re-query.
- **`ml958 ... still read-only`** with both kr values -> the kr numbers name the
  real obstruction; neither alignment nor +COPY was it.
- **No ml958 line at all** -> `.EMP` does not reach this early-out either;
  instrument the remaining `mprotect_exec` exits (8088/8110/8196/8202/8398/8593)
  rather than guessing a fourth time.

**Build:** `Madeira-ml958-secwrite-align.ipa` (73,570,155 bytes, 345 files,
signed). Verified from inside the IPA: dylib ml958 x1, ml957 x2, ml956 x3,
ml936/938/949/951/952, no MAILSLOT; EC ntdll ml943/ml948; xtajit64 ml951/ml954.

## 17. rdr23/ml958: THE SUB-FLOOR WRITE BLOCKER IS CLEARED. New wall = a 32-bit pointer assumption (ml959, 2026-09-17)

### 17.1 ml958 worked, and the memory rise is the unpack starting

- **`ml956 REFUSED: 0`** (was 2). The sub-floor write refusal is gone.
- All **24** ml958 grants succeeded on **plain RW, `kr=0`** — `+COPY` was never
  needed, confirming ml391's ladder was the right shape and that the old
  `+COPY`-only request was the wrong one.
- Serviced accesses 471,041 -> **606,209+**. Log 1.27 MB -> **1.86 MB**
  (14,845 -> 23,185 lines). Footprint peak 1157 -> **1332 MB**.

The footprint rise is explained, not mysterious: the ml958 grants are
**RDR2.exe's own sections** — `[jit-pool] image 0x16e010000+0x7528000 (?) ->
pool 0x11c91f000` (122 MB; module name resolves to "?"), with granted sections
of 0x3288000 (52 MB), 0x2360000 (35 MB), 0x15c4000 (22 MB) and others, all
`prot=rwx`. Those became writable and EMP.dll began **unpacking into them**.
ml957 also fired for RDR2.exe, `libarm64ecfex.dll`, `oo2core_5_win64.dll`,
`amd_ags_x64.dll`, `bink2w64.dll`, `launc.dll`.

### 17.2 The predicted stale-JIT hazard did NOT fire

§15.6 flagged that making `.EMP` writable would make code invalidation live.
It did not happen: only **3** distinct fault addresses land in `.EMP`
(0x13037c00, 0x1303ea42, 0x1303eaa8, plus the original 0x1303aaa0), and the
every-4096th serviced samples are overwhelmingly `.emp1` reads. A
self-modifying unpack of `.EMP` would produce thousands of write faults across
its 0x33000 bytes. `.EMP` is being used as small scratch, not rewritten.

Invalidation remains owed for RDR2.exe's sections (§17.5), which *are* being
written in bulk — a different and larger instance of the same hazard.

### 17.3 The new wall, diagnosed to the instruction

Ordered facts from rdr23:

```
line 5771  [file-wfail] status=0xc0000035 disp=2  ...\AppData\Roaming\EMPRESS
           (STATUS_OBJECT_NAME_COLLISION on the directory -- benign, it exists)
line 6001  [file-wfail] status=0xc0000043 disp=5 access=0x00100082
           ...\AppData\Roaming\EMPRESS\15a3.bin
           (STATUS_SHARING_VIOLATION on a FILE_OVERWRITE_IF WRITE open)
line 6004  [valloc] base=0x15d5e0000 size=0x1000 type=0x3000 prot=0x4 tid=008c
line 6610  SEGV #1 pc=0x136fa96c0 addr=0x5d5e0010 esr=0x92000006 (read)
           guest rip=0x1313bd8b  r11=0x5d5e0010  x19=0x15d5c0000
```

`0x15d5e0000 + 0x10 = 0x15d5e0010`; `& 0xFFFFFFFF` = **`0x5d5e0010`**, the
faulting address exactly.

Guest rip 0x1313bd8b is EMP.dll **rva 0x13bd8b**, inside `.emp1`
(rva 0xfc000-0x20c6fc, `chars=0x68000060`, **no WRITE bit**) — so `.emp1` is
plain non-self-modifying code and its on-disk bytes are what execute. Pulled
them from the VM prefix at file offset 0xf7c00 + (0x13bd8b - 0xfc000) =
**0x13798B**:

```
4d 63 db    movsxd r11, r11d
```

REX.WRB (0x4D) + opcode 0x63 (MOVSXD r64, r/m32) + modrm 0xDB (reg=r11,
rm=r11). **EMP.dll sign-extends R11 from its own low 32 bits.** With
R11 = 0x15d5e0010 (bit 31 clear) the result is 0x5d5e0010.

So this is **not** FEX mis-emulating a 64-bit op. EMP.dll packs pointers into
signed 32-bit values and therefore requires its memory **below 2 GB** —
entirely consistent with `ImageBase 0x13000000` + `RELOCS_STRIPPED`, i.e. a
module that demands a sub-2 GB image base.

### 17.4 The accuracy gap, and the open architectural question

Windows' `VirtualAlloc` with no preferred base allocates **bottom-up from the
lowest free address**; ours does not. Any guest that packs pointers into 32
bits depends on that. On iOS we cannot map below 4 GB at all (the mandatory
`__PAGEZERO`, measured four ways), so the requirement is unsatisfiable by a
real mapping — but the ml938 sub-floor window machinery already makes
sub-4 GB addresses work for *images* and could serve *allocations* the same way.

**This is the decision Astra should weigh.** Three shapes, none obviously right:

1. **Sub-floor allocation arena.** Reserve a low guest range (avoiding the
   registered image windows at 0x13000000+0x20f000, 0x7a800000+0x160000,
   0x7ffe0000), back it high, register a window, return the low address.
   Hazard: **every access faults**. rdr23 already pays 606k faults for `.emp1`
   reads alone; a general heap behind fault emulation could be ruinous. Fine
   for EMP.dll's 0x1000 bookkeeping allocations, potentially fatal if the game
   allocates in bulk expecting low addresses.
2. **Honour the caller's limit or fail.** If EMP.dll passes `zero_bits` (for a
   64-bit caller Wine accepts a value >32 as an address mask, so 0x7FFFFFFF is
   legal and means "below 2 GB"), then returning 0x15d5e0000 breaks an explicit
   contract and we should satisfy it or return STATUS_NO_MEMORY rather than a
   violating pointer. Narrow and unambiguous — **if** zero_bits is set.
3. **Caller-keyed placement:** allocations made by code running out of a
   sub-floor image get sub-floor memory, on the principle that a module
   requiring a sub-4 GB image base necessarily packs pointers. Principled and
   tied to an observable property rather than a title, but still an inference.

Which applies is decided by one field that the old probe did not print.

### 17.5 The second, independent blocker: the sharing violation

`STATUS_SHARING_VIOLATION` on EMP.dll's own state file is why `15a3.bin` sits
at **0 bytes** on disk (verified in the prefix; created 15:40 by an earlier
run). It happens at line 6001, ~600 lines **before** SEGV #1, so it is the
earlier failure and may well change EMP.dll's whole subsequent path.

A sharing violation means a conflicting open handle exists. Candidates:
- a handle leaked by the launcher pseudo-process (007c), which exited at line
  3738 — before the violation, so `process_exit_wrapper` should have closed the
  master socket and let wineserver reap it (it does call
  `ios_fd_cache_release` and `ios_jit_reclaim_process`);
- EMP.dll opening the file twice within 008c, where Windows would permit the
  second open and our wineserver's verdict is stricter.

Not resolvable offline: the probe never logged the requested **share mode**.

### 17.6 ml959 — pure diagnostics, answering exactly those two questions

No behavioural change. Two probes extended:

- `[valloc] ml959` now prints **`req_hint`, `zero_bits`, `limit`** alongside the
  result, and flags `LIMIT-VIOLATED` when the result exceeds the requested
  limit. It also logs unconditionally (outside the census budget) whenever
  `zero_bits` is set or a limit is violated, so the interesting allocation
  cannot be crowded out — the ml945 lesson.
- `[file-wfail] ml669` now prints **`sharing=`**.

Next run then decides:
- `zero_bits != 0` / `LIMIT-VIOLATED` -> §17.4 option 2; narrow fix.
- `zero_bits == 0` and `req_hint == 0` -> §17.4 option 1 or 3; architectural,
  Astra's call.
- `sharing=` on the 15a3.bin line -> whether wineserver's violation verdict is
  correct, and if not, where the stale handle comes from.

**Build:** `Madeira-ml959-allocprobe.ipa` (73,570,867 bytes, 345 files, signed).
Verified from inside the IPA: ml959 valloc line x1, `file-wfail ... sharing=`
x1, old `[valloc] ml842` gone (0), ml958/957/956 kept (1/2/3).

## 18. rdr25/ml959: both diagnostic questions CLOSED. Two blockers, one is Astra's call (ml960, 2026-09-17)

### 18.1 Housekeeping: rdr24 was a duplicate run

The log pulled before rdr25 carried 376 `[valloc] ml842` lines (the pre-ml959
format) and ml958/957/956 counts identical to rdr23, with
`madeira-log.prev.txt` exactly rdr23's size. It was a **second run of the
ml958 build**, byte-equivalent in every metric: peak 1332, cycle=10, 872,449
serviced, 0 refused, 3 SEGVs, 4 wfails. The run is deterministic.

Note for future reports: the device's own memory gauge read ~1400 MB while our
`[footprint] phys=` probe read 1332 MB. Different measurements — `phys_footprint`
for the task versus the system gauge. Do not read the difference as progress.

### 18.2 Blocker B is NOT a broken contract — it is bottom-up placement

```
[valloc] ml959 # base=0x15d090000 size=0x1000 type=0x3000 prot=0x4 tid=008c
                 | req_hint=0x0 zero_bits=0x0 limit=0x0
```

All **380** allocations in the run report `zero_bits=0x0`, and there are **zero**
`LIMIT-VIOLATED` lines. So EMP.dll supplies no preferred address and no limit.

This **refutes §17.4 option 2**. We are not violating an explicit `zero_bits`
contract; ours matches upstream Wine exactly, including the `#ifndef _WIN64`
guard around the `zero_bits >= 32` rejection, so a 64-bit caller's mask form
would have been accepted had one been passed.

What remains is the unstated dependence: **Windows' `VirtualAlloc` with no
preferred base allocates bottom-up from the lowest free address**, EMP.dll
assumes that, and `movsxd r11, r11d` (verified from the file, §17.3) then
requires the result below 2 GB. The crash reproduced identically in rdr25 with
only ASLR shifted — `addr=0x5d090010`, `x19=0x15d080000`, same
`guest rip=0x1313bd8b`.

iOS's floor is ~0x104000000, so **no real mapping can satisfy it**; the ml938
sub-floor window is the only mechanism that can. That makes this architectural,
with a real cost cliff (rdr25 already pays 872,449 serviced faults for `.emp1`
reads alone), so §17.4 options 1 and 3 stand as written and the choice is
Astra's. One addition learned since: serving low memory to *arbitrary* callers
is actively dangerous, because Wine's own native code would then hold low
pointers and fault on every access — so any arena must be restricted to the
requester that needs it, which is precisely the hard part.

### 18.3 Blocker A is earlier and is probably ours

`sharing=0x00000001` = **FILE_SHARE_READ** on the `15a3.bin` open
(`disp=5` FILE_OVERWRITE_IF, `access=0x00100082` = SYNCHRONIZE |
FILE_READ_ATTRIBUTES | FILE_WRITE_DATA). It fails `0xc0000043` at line 5904,
~6 lines **before** the allocation that crashes, so it is the earlier failure
and may change EMP.dll's whole subsequent path if fixed.

Reading `check_sharing` (`build/wineserver/fd_ios.c:2198`, standard Wine): with
`FILE_WRITE_DATA` requested and `sharing` lacking `FILE_SHARE_WRITE`, the
verdict requires **another open fd on the same inode holding write access**
(check 1 via `existing_sharing`, or check 3 via `existing_access`).

The launcher pseudo-process (007c) self-terminated at lines 3604-3681, well
**before** the violation, and `process_exit_wrapper` does close the master
socket, release the peb-keyed fd cache and reclaim the pool. So the holder is
either an fd leaked past that teardown — **fd ownership at pseudo-process
teardown is a known unresolved area in this tree** — or a second live open
inside 008c that Windows would permit.

These need opposite fixes, and the client-side probe structurally cannot tell
them apart: it only sees its own failed request.

### 18.4 ml960 — name the holder

`check_sharing` is the only place with the authoritative list
(`fd->inode->open`). All three sharing-violation returns now report, capped at
16 and only on the violation path:

- the requesting `access` / `sharing` / `options` and unix name;
- **which of the three tests fired**;
- every other fd on the inode: `unix_fd`, `access`, `sharing`, `options`, name;
- or explicitly that there is **no** other fd, meaning the verdict came from the
  mapping/access bits rather than a second opener.

Next run then decides blocker A:
- **one holder with write access and no other opener alive** -> leaked fd from
  the exited launcher; fix is fd ownership at teardown.
- **a holder inside the live process** -> a real second open; compare against
  Windows' rules before changing `check_sharing`.
- **no other fd** -> the verdict is from `FILE_MAPPING_*` bits, i.e. the file is
  mapped as a section somewhere, which is a different bug again.

Nothing about allocation placement changed in this build; blocker B is
untouched pending Astra.

**Build:** `Madeira-ml960-sharingholder.ipa` (73,568,532 bytes, 345 files,
signed). Verified from inside the IPA: 3 ml960 strings, ml959 valloc line,
`file-wfail ... sharing=`, ml958/957/956 = 1/2/3.

## 19. RETRACTION + the confirmed teardown bug (ml961, 2026-09-17, Astra review)

### 19.1 RETRACTED: §17.3's `movsxd r11, r11d` was read from the wrong file offset

**My claim that EMP.dll truncates its own pointer is withdrawn.** Astra caught
it, and I reproduced the error exactly.

I computed the offset correctly in prose — `0xf7c00 + (0x13bd8b - 0xfc000) =
0x13798b` — and then typed **1275787 (0x13778b)** into the `dd` command. A
`79`->`77` transposition, exactly **512 bytes early**, landing on RVA 0x13bb8b.
Re-pulled at the correct offset 1276299, and the bytes are:

```
1313bd8b  49 8b 12          mov   rdx, [r10]
1313bd8e  41 0f 99 c3       setns r11b
1313bd92  40 f6 d5          not   bpl
1313bd95  49 0f bf e9       movsx rbp, r9w
1313bd99  66 44 0f b6 1a    movzx r11w, byte ptr [rdx]   ; the fault
```

Decoded independently and they match Astra's disassembly instruction for
instruction. Three further corrections that follow:

- The faulting guest RIP is **0x1313bd99**, not 0x1313bd8b — the log
  (`rdr25:6523`) already resolved the exact RIP and I read the block *entry*
  instead.
- The faulting pointer register is **RDX / x1**, not R11. R11's low byte is
  written by `setns`, so building the story on R11 was doubly misleading.
- The emitted ARM64 (`rdr25:6529-6533`) is `ldapr x1,[x4]` — a **64-bit** load
  — then `ldaprb w6,[x1]`. There is no 32-bit truncation between the two.

So the block **loads an already-bad pointer**; it does not create one. The
correlation (bad address == low 32 bits of the new allocation + 0x10) is real
and still worth chasing, but **"EMP.dll requires sub-2 GB allocations" is a
hypothesis, not an established requirement.** §17.4 and §18.2 are downgraded
accordingly, and the low-address arena is **off the table** until the producer
of the operand at `[R10]` is identified. Per Astra: do not add caller-keyed low
allocation, do not fake a low alias by subtracting 4 GB, do not patch MOVSXD.

Process note: this is the second time a confident causal story rested on bytes
I had not cross-checked. The offline disassembly was free — but only the
*arithmetic* was checked, not that the command used the number I had derived.
Echo the offset back from the command before trusting the bytes.

### 19.2 The confirmed bug: `ios_fd_cache_release` closed `fd + 1`

`add_fd_to_cache` stores **fd + 1** so that 0 can mean "unset"
(`server_ios.c`, its own comment). Both readers decode it: `get_cached_fd` does
`*fd = cache.s.fd - 1`, `remove_fd_from_cache` does `fd = cache.s.fd - 1`.
`ios_fd_cache_release` was the only place that passed the stored word straight
to `ios_fdt_note_close` and `close`.

It therefore closed **the descriptor after the one it owned**, leaking the real
one and closing an unrelated live fd. rdr25 shows the collateral damage in
three consecutive lines:

```
3663  CROSS! close fd=30 why=fd-cache-release kind=request_wr
             owner_peb=0x6fdff0000 closer_tid=007c dead_peb=0x48ff84000
3665  read_request EOF tid=0024 pid=0020 request_unixfd=27 -> kill_thread
3666  kill_thread tid=0024 pid=0020 violent=0
```

The exiting launcher closed **another pseudo-process's request-pipe write end**,
and wineserver killed that thread on the resulting EOF. The neighbouring lines
show the same shift (`close fd=221/225/229 ... untracked` for real fds
220/224/228).

Two adjacent defects fixed with it:

- **`FD_TYPE_INVALID` entries hold a cached NTSTATUS, not a descriptor**
  ("if fd type is invalid, fd stores an error value"), so closing them closed
  an arbitrary number. Now skipped.
- **Secondary blocks come from `anon_mmap_alloc` (mmap) but were released with
  `free()`** — undefined, and corrupts the malloc heap. Now `munmap` at the
  matching size; only the enclosing cache object goes to `free`, and
  `initial_block` is an inline array released with it.

The populated test is now on the **stored word** being non-zero rather than the
decoded fd being positive, because **descriptor 0 is valid** (stored as 1) and
the old `s.fd > 0` test combined with no decode hid that entirely.

### 19.3 Verified independently, not taken on faith

Wrote my own harness (`scratchpad/fdtest.c`) extracting both loops with
intercepted close/free/munmap, over real fds 29, 0 and 220, an unset entry, a
cached NTSTATUS and a secondary block. It reproduces Astra's numbers exactly:

```
original:  close=[30,1,221]  wrong-allocator-free=1  unmapped=0
ml961:     close=[29,0,220]  wrong-allocator-free=0  unmapped=1 (size=512)
```

512 bytes matches the allocation size at the `anon_mmap_alloc` call. The
emitted ARM64 in `obj/server.o` confirms the shape Astra showed was missing in
the ml959 binary:

```
1c68  ldr  w8, [x22, x27]   ; stored
1c6c  cbz  w8, ...          ; skip unset
1c7c  b.eq ...              ; skip FD_TYPE_INVALID
1c80  sub  w23, w8, #0x1    ; real_fd = stored - 1
1c90  bl   ...              ; note_close(real_fd)
1c98  bl   ...              ; close(real_fd)
1cb4  bl   ...              ; munmap
```

Not claimed: that this resolves concurrent teardown. Registry removal and
freeing a cache while another thread may still hold a lock-free pointer needs a
lifetime/quiescence contract; the harness is single-threaded.

### 19.4 CORRECTED: §18.4's ml960 decision tree was wrong

Astra corrected three things and they stand:

- **Check 1 can fail against an existing READ-ONLY open** that does not share
  WRITE. An existing *write-access* holder is **not** required. §18.3's claim
  that the verdict "requires another open fd holding write access" is wrong.
- **A second open from the same process must still satisfy sharing checks.**
  Shared process ownership is not evidence Windows would permit it.
- **"NO other fd" would be an invariant/diagnostic bug**, not evidence of
  mapping bits outside the list: `existing_access` accumulates only from other
  inode-open entries, so with no other entry none of the three checks can fire.
  Mapping-access bits are likewise carried by another fd entry.

Also: ml960 reports `unix_fd`, access, sharing, options and path, but **not**
owner PID, handle, or file-object identity — so by itself it cannot separate a
leaked launcher object from a live or inherited reference. **A leaked
client-side raw fd does not prove a stale server-side sharing object**;
`fd_destroy` removes the inode-open entry and teardown normally closes server
handles. If a conflict persists, add ownership by associating the holder's
`user` object with the server handle tables, and do not equate a host `unix_fd`
number with a Wine process owner. **Do not relax sharing checks and do not
delete the state file as a "fix".**

### 19.5 Sequence from here (Astra's, adopted)

1. **ml961** — cache teardown corrected and regression-tested. *(this build)*
2. Retain ml960's conflict detail; add ownership only if a conflict persists.
3. If the pointer fault survives, capture **R10 and the full eight bytes at
   `[R10]`** with a failure-returning read, plus the preceding guest block
   identity — trace the *producer*, not the innocent final byte load. This
   run's R10 was 0x16b7ff1b6; do not hard-code it across launches. Note the
   ordinary guest stack still held full-width values at the fault
   (`rdr25:6469 RSP-24 = 0x15d090000`, `rdr25:6470 RSP+8 = 0x15d09001a`), so a
   same-instant capture can split "operand was already low" from "load result
   came back low".
4. Decide on address-space emulation **only** if that evidence establishes a
   real low-address dependency.

Acceptance for this build: **no cross-owner close from launcher cache cleanup**,
and the desktop's tid 0024 must not receive EOF as a consequence of it. Then
assess the sharing refusal and the guest fault separately.

### 19.6 Distance to a window

Still pre-window initialization, and not promised by this fix. The run reports
zero top-level windows at the relevant census points and there is no evidence
of engine archive loading (`.rpf` reads remain 0). What improved is that the
next step is a small demonstrably-necessary repair instead of several builds
spent on an arena whose justification was incorrect.

**Build:** `Madeira-ml961-fdcache.ipa` (73,567,819 bytes, 345 files, signed).
Verified from inside the IPA: ml961 release line x1, old ml571 release line
gone (0), ml960 x3, ml959 valloc, ml958/957/956 = 1/2/3. Contains the ml960
probe as well, which has not yet run; ml960 is a pure diagnostic with no
behavioural effect, so the one behavioural variable in this build is ml961.

## 20. rdr27/ml961: teardown fix CONFIRMED; sharing question ANSWERED; crash unchanged (2026-09-17)

Build fingerprint: log carries `ml961 released` x1, `rev=ml571 released` x0,
ml960 x2. Installed dylib matches. (The two prior pulls were ml959 repeats —
ml961 was installed *during* a running ml959 session, so the running process
kept its already-mapped binary. Fingerprint the log, not the bundle.)

### 20.1 ml961 works — acceptance met

- `[fdtrace] close fd=29 why=fd-cache-release` where rdr25 closed **fd=30** for
  the same real descriptor. The off-by-one is gone.
- `[fd-cache] rev=ml961 released peb=..., closed 4 cached fd(s) (decoded fd+1;
  skipped FD_TYPE_INVALID; munmap for secondary blocks)`.
- **No EOF-driven kill after the cache release.** rdr25's direct consequence
  (`3665 read_request EOF tid=0024 pid=0020 -> kill_thread`) does not recur;
  nothing follows the release but normal progress.

### 20.2 The two remaining `CROSS!` lines are a PROBE ARTIFACT, not a bad close

This looked like a residual bug and is not one. Chain:

- `1810 [fdtrace] pipes tid=0074 peb=0xc29f1c000 master=143 reply=195/202
  wait=203/204` — fds 195 and 203 are that thread's reply_rd / wait_rd.
- `2417 read_request EOF tid=0074 pid=0050 -> kill_thread` / `2418 kill_thread`
  — tid 0074 dies, **1,350 lines before the cache release**.
- No `ios_fdt_note_close` record exists for 195/203 at that point: whatever
  closes a dying thread's comm pipes does **not** route through the tracker, so
  their entries kept `kind=reply_rd`/`wait_rd` and `peb=0xc29f1c000`.
- The OS then recycled 195/203 to the exiting launcher's own *file* opens
  (file fds are untracked — note `close fd=29 ... untracked`,
  `close fd=207 ... untracked`).
- At teardown `ios_fdt_note_close` matched the stale record, saw
  `e->peb != dead_peb`, and printed `CROSS!` for a descriptor the launcher
  legitimately owned.

`ios_fd_cache_release` already has the machinery for this — it sets
`e->kind = FDT_CLOSED` and the untracked branch prints `(prev comm fd)` — it
simply never runs for thread-exit pipe closes. **Probe hygiene fix owed:** route
comm-pipe closes at thread death through `ios_fdt_note_close` (or clear the
entry), so the tracker stops reporting false cross-owner closes. Until then,
treat a `CROSS!` on a number previously used by a comm pipe as unproven.

### 20.3 ml960 answered the sharing question, and §18.3/§19.4 stand corrected

```
ml960: SHARING VIOLATION (check 1) on ".../EMPRESS/15a3.bin"
       request access=00100082 sharing=00000001 options=00000060
ml960:   holder #1 unix_fd=217 access=00100081 sharing=00000001 options=00000060
```

**Check 1**, and exactly **one** holder whose access `0x00100081` decodes to
SYNCHRONIZE | FILE_READ_ATTRIBUTES | **FILE_READ_DATA** — a **read-only** open
sharing only `FILE_SHARE_READ`.

Astra's correction is confirmed against runtime: check 1 fails because an
existing read-only open does not share WRITE; **no write-access holder is
required**. My §18.3 claim was wrong.

Consequence: **wineserver's verdict is correct Windows behaviour.** Holding a
file with `FILE_SHARE_READ` does block a later `FILE_WRITE_DATA` open. So there
is nothing to fix in `check_sharing`, and relaxing it would be wrong. The defect
— if any — is that the read handle is **still open** when the write open is
attempted; on Windows the module would have closed it first.

Next step is Astra's step 2, now well-scoped because there is exactly one holder
to attribute: associate that fd's `user` object with the server handle tables to
name the owning process, and check whether a client-side `NtClose` failed to
release the server handle. Do **not** equate `unix_fd=217` with an owner, and do
not delete the state file as a workaround. Candidate producer visible at
`rdr27:5080` — `008c ... ml955 [read-src] #13 handle=0x4c type=FILE(1) len=64`,
a 64-byte read in the same process shortly before the violation, consistent
with the holder being that process's own un-closed read handle.

### 20.4 The guest crash is unchanged, and the correlation is now 4-for-4

peak 1333, serviced 872,449, 3 SEGVs — identical to rdr26. The allocation ->
fault correlation reproduces with fresh ASLR every run:

| run | allocation | +0x10 low 32 bits | fault address |
|---|---|---|---|
| rdr23 | 0x15d5e0000 | 0x5d5e0010 | 0x5d5e0010 |
| rdr25 | 0x15d090000 | 0x5d090010 | 0x5d090010 |
| rdr26 | 0x16a970000 | 0x6a970010 | 0x6a970010 |
| rdr27 | 0x168e30000 | 0x68e30010 | 0x68e30010 |

Four for four. Something discards the high bits; per §19.1 it is **not** the
instruction I originally blamed, so the producer is upstream. **Astra's step 3
is now the live item:** at entry to the faulting block capture **R10 and the
full eight bytes at `[R10]`** with a failure-returning read, plus the preceding
guest block identity. Do not hard-code R10 across launches. The ordinary guest
stack still held full-width values at the fault in rdr25 (`RSP-24 =
0x15d090000`, `RSP+8 = 0x15d09001a`), so a same-instant capture separates
"operand was already low in memory" from "the load returned a low value".

### 20.5 Honest status

This build fixed a real corruption bug and closed a real diagnostic question.
It did **not** advance the game: still pre-window, `.rpf` reads still 0, same
SEGV at the same place. No window is promised by the next fix either.

## 21. THE FAULTING CODE IS AN OBFUSCATION VM. Root cause = mixed-width stream round-trip (ml965, 2026-09-17)

Offline work only; rdr30 (ml964) plus disassembly of the shipped EMP.dll.

### 21.1 ml964's result, and why it misled at first

The field watch fired 66 times and **never shouted**. The timeline explains why:

```
#28..#64  [0x16997f1b6] = 0x000000016a100010   (FULL WIDTH, high32=1)  x37
#65       [0x16997f1b6] = 0x0000000000000202
#66       [0x16997f1b6] = 0x0000000000000040
```

Same address the SEGV then read as `0x000000006a100010`. Two earlier samples in
the same run held `0x206b000000016a10` and `0x6574000000016a10` — whose high
halves are ASCII text (`k `, `te`).

So `0x...1b6` is **not a structure field**. It is a byte position that holds
completely different things at different times, which is why "who wrote the
field" had no answer.

### 21.2 What the code actually is

Disassembled the shipped EMP.dll at the **verified** file offset (anchored on
the known bytes `49 8b 12`; the anchor check is now mandatory after §19.1):

```
1313bd8b  49 8b 12           mov   rdx, qword ptr [r10]     ; 8-byte read at cursor
1313bd8e  41 0f 99 c3        setns r11b                     ; junk
1313bd92  40 f6 d5           not   bpl                      ; junk
1313bd95  49 0f bf e9        movsx rbp, r9w                 ; junk
1313bd99  66 44 0f b6 1a     movzx r11w, byte ptr [rdx]     ; <== THE FAULT
1313bd9e  66 0f b3 f5        btr   bp, si                   ; junk
1313bda2  66 44 0f ac cd aa  shrd  bp, r9w, 0xaa            ; junk
1313bda8  49 81 c2 06 00 00 00  add r10, 6                  ; cursor += 6
1313bdb7  66 45 89 1a        mov   word ptr [r10], r11w      ; 2-byte write back
1313bdc3  41 8b 29           mov   ebp, dword ptr [r9]       ; second cursor, 4 wide
1313bdc7  49 81 c1 04 00 00 00  add r9, 4                    ; cursor += 4
1313bdd8  e9 25 f8 0c 00     jmp   0x1320b602                ; next dispatcher block
```

This is an **obfuscation VM interpreter**: two byte-stream cursors (`r10`,
`r9`) walking a stack-resident context at non-power-of-two strides, interleaved
with dead flag arithmetic, ending in a dispatch jump. `r10` is a cursor, not a
pointer to a typed field.

### 21.3 The root cause

capstone scan of 256 KB of `.emp1` for stores through that same cursor,
with the `0x66`-prefix case separated (an earlier count conflated 16-bit with
32-bit and is corrected here):

```
  16-bit  mov word  [r10], rXXw : 100
  32-bit  mov dword [r10], eXX  :  57      e.g. rva 0x1353fe: 41 89 0a
  64-bit  mov qword [r10], rXX  :  74      e.g. rva 0x130ceb: 49 89 02
```

The VM writes its stream at **three different widths through one cursor** and
reads back at other widths. A pointer written into a 4-byte slot and read as
8 bytes yields its low 32 bits plus whatever adjoins it — which is precisely
the observed value, `(allocation + 0x10) & 0xFFFFFFFF`, reproduced in **five
consecutive runs** across ASLR:

| run | allocation | fault address |
|---|---|---|
| rdr23 | 0x15d5e0000 | 0x5d5e0010 |
| rdr25 | 0x15d090000 | 0x5d090010 |
| rdr26 | 0x16a970000 | 0x6a970010 |
| rdr27 | 0x168e30000 | 0x68e30010 |
| rdr30 | 0x16a100000 | 0x6a100010 |

**So EMP.dll's VM round-trips pointers through narrow stream slots and
therefore requires its pointers to fit in 32 bits.** §17.3's conclusion was
right in substance; the instruction cited for it was wrong (§19.1), and the
real evidence is this, not a `movsxd`.

Also resolved: why the corrupting write was never caught. On ARM64 a plain
`STR` permits unaligned access and does **not** fault — only acquire/release
and exclusive forms require alignment. The stream writes FEX lowers to plain
`STR` never reach our handler, which is exactly why `off=0x1b6` showed zero
traffic while `0x1ac`/`0x1ae` showed plenty. Chasing the writer through the
fault path could not have worked.

### 21.4 ml965: the one gap left

Still unproven: that **this** pointer entered through a narrow slot (the
instruction *class* exists; the specific event is unobserved). ml965 widens the
operand dump to 256 bytes and, for each occurrence of the low 32 bits in the
stream, reports the following 4 bytes:

- `0x00000001` -> a full-width copy also exists, so the wide write happened and
  something else dropped the half;
- `0x00000000` -> narrow slot only, **round-trip confirmed**;
- anything else -> adjoined by unrelated stream bytes (consistent with a packed
  stream, weaker evidence).

Pure logging. Build: `Madeira-ml965-stream.ipa` (73,571,907 bytes), verified
from inside the IPA: ml965 x2, ml964 x2, ml963 x1, ml962 x4, ml961 x1.

### 21.5 Fix options — Astra's call, with the premise now evidenced

iOS's floor is ~0x104000000, so a sub-4 GB pointer cannot be a real mapping;
only the ml938 window mechanism can serve one.

1. **Bottom-up allocation arena** (§17.4 option 1). Now motivated, but the
   hazard I raised in §18.2 stands and is worse than first thought: serving low
   memory to arbitrary callers would give Wine's own native code low pointers,
   faulting on every access. It must be restricted to the requester, which is
   the hard part.
2. **Demand-driven truncated-alias window (new).** Leave placement alone. On a
   sub-floor fault at an address in no window, check whether
   `addr + k*2^32` matches a known guest allocation (we already track them in
   `ios_vm_note_alloc`); on a unique match, register a window and service it.
   Costs nothing until it happens, needs no placement change, cannot exhaust
   low space, and reuses proven machinery.
   **Caveat, and why this is not mine to decide:** Astra explicitly said *"do
   not fake a low alias by subtracting 4 GB"* (§19.1). This is a demand-driven
   version of exactly that. It also aliases two guest addresses onto one
   allocation and could collide if two allocations share low 32 bits. The
   premise is far better evidenced now than when that instruction was written,
   but the objection may be principled rather than evidentiary.

Not attempted in this build. Recommend Astra rule on 1 vs 2 before any
allocation or aliasing change.

### 21.6 Status

Unchanged on device: pre-window, `.rpf` reads 0, same SEGV. What changed is that
the failure is now understood rather than guessed, and the last open question
has a bounded probe instead of an architecture rewrite.

## 22. rdr31/ml965: the causal chain is CLOSED, and our emulation is exonerated (2026-09-17)

### 22.1 The stream contains BOTH forms of the pointer

```
[stream] ml965 low32=0x5cdb0010 found at R10-118, next 4 bytes = 0x00000000 -- NARROW slot only
[stream] ml965 low32=0x5cdb0010 found at R10-30,  next 4 bytes = 0x00000001 -- FULL-WIDTH copy present
[stream] ml965 low32=0x5cdb0010 found at R10+0,   next 4 bytes = 0x00000000 -- NARROW slot only
```

Corroborating values from the same run: allocation `base=0x15cdb0000` with
`req_hint=0x0 zero_bits=0x0 limit=0x0`; `[R10]=0x000000005cdb0010`;
`SEGV #1 addr=0x5cdb0010`; and `0x15cdb0010 & 0xffffffff == 0x5cdb0010`.

### 22.2 The complete chain

1. `VirtualAlloc(NULL, 0x1000, MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE)` — no
   hint, no `zero_bits`, no limit — returns **0x15cdb0000** (5.84 GB).
2. EMP.dll's VM forms `0x15cdb0010` and stores it **FULL WIDTH** into its
   stream. **Proven**: present at R10-30 with the following word `0x00000001`.
3. The VM copies that value through a **narrow (4-byte) stream slot**, losing
   the high half. Truncated copies sit at R10-118 and R10+0, each followed by
   `0x00000000`.
4. `mov rdx, qword ptr [r10]` reads the narrow copy as 8 bytes ->
   `0x000000005cdb0010`.
5. `movzx r11w, byte ptr [rdx]` dereferences it -> SEGV at 0x5cdb0010.

### 22.3 What this settles

- **A real low-address dependency is established.** This was Astra's explicit
  bar for considering address-space emulation (§19.5 step 4). The guest narrows
  its own pointer and then dereferences the narrowed form; nothing downstream
  can repair that without the pointer fitting in 32 bits.
- **Our emulation is exonerated.** The wide write happened correctly and is
  *still visible in the stream*. The narrowing is the guest VM's own copy, not
  a dropped half in our store path, not the unaligned backpatch (§20.2 proved
  the barrier slots are reserved NOPs), and not the load (§21.3: the host
  lowering is a 64-bit `ldapr`).
- **§17.3 was right in substance, wrong in evidence.** The `movsxd` attribution
  is withdrawn (§19.1); this stream round-trip is the actual mechanism.

### 22.4 Constraints measured for the fix decision

- `IOS_SUBFLOOR_MAX` is **8**; three windows are in use (0x7ffe0000,
  0x7a800000, 0x13000000). So 5 slots are free — enough for a handful of
  allocations, not for a general arena of many.
- The truncated target 0x5cdb0010 (1.45 GB) **does not collide** with any
  registered window.
- Across **340 distinct allocations** in the run there were **zero** low-32
  collisions, so the aliasing hazard I raised against option 2 in §21.5 did not
  materialise in practice — though that is one run, not a guarantee, and the
  guest is free to allocate two regions that do collide.

### 22.5 Recommendation to Astra

Diagnosis is complete; the remaining choice is architectural and I am
deliberately not making it:

- **Option 1, bottom-up arena** (§17.4/§18.2): matches Windows semantics but
  must be restricted to the requesting module or Wine's own native code gets
  low pointers and faults on every access. With only 8 window slots, a general
  arena also does not fit the current table.
- **Option 2, allocation served low through a window**: allocate the backing
  high, return a sub-4 GB address, register one window. The guest then narrows
  an address that *already* fits in 32 bits, so the round-trip is harmless and
  there is only ever one address the guest sees (no aliasing of two addresses,
  unlike the demand-driven variant in §21.5). Cost is a fault per access to
  that allocation; the allocation is 0x1000. Still needs a targeting rule, and
  Astra's "do not fake a low alias" instruction should be re-read against this
  shape before anyone implements it.

Unchanged on device: pre-window, `.rpf` reads 0, same SEGV.

## 23. The low-allocation experiment returns a NEGATIVE verdict (ml967/ml968, 2026-09-17)

### 23.1 rdr32: my first cut was a regression, cause identified

All 13 redirects went to **tid=0024, the desktop** — taskbar never loaded, run
died at 187 KB / 1,816 lines. Cause: `ios_lowalloc_process_qualifies()` asked
the **global** `ios_subfloor[]` table whether any sub-floor image existed, and
window #0 is KUSER_SHARED_DATA at 0x7ffe0000, registered for every process at
startup. The test was true from the first process onward.

Two of Astra's warnings landed exactly there: that table has global
last-writer-wins ownership (avoided for translation, then leaned on for
qualification — same flaw), and "don't redirect every allocation originating
from a low-loaded module", which is what it became.

**ml967 fix:** qualification is a **per-peb** set, populated only for an image
that is both sub-floor **and** `RELOCS_STRIPPED`. Verified against rdr27 rather
than assumed: ml949 reports RELOCS_STRIPPED only for preferred base 0x13000000
(the packer); opengl32.dll at 0x7a800000 is not stripped and does not qualify.
No module name hard-coded.

### 23.2 rdr33: the scope fix works

```
ml966: peb=0xaf832c000 QUALIFIES ... (RELOCS_STRIPPED sub-floor image, preferred base 0x13000000)
13 x LOW-ALLOC, all tid=007c        <- the launcher
0  x LOW-ALLOC tid=0024             <- desktop untouched, taskbar restored
```

Exactly one qualifying pseudo-process. Scope is correct.

### 23.3 ...and the experiment then fails, for Astra's constraint 4

```
007c SEGV #1  pc=0x18c374048  addr=0x30000000
[sym] pc=libsystem_platform.dylib`_platform_memmove+0xa8
insn_stream ... [ad000c02] ...            = stp q2, q3, [x0]   (32-byte SIMD pair)
[lowalloc] ml966 serviced 1 access(es); latest guest 0x30000000
           (32 byte span, write) -> backing 0x15d060000 owner=0xaf832c000
[rsp-forensics] callret_sp=0xafb401000 base=0xafb001000       = 4 MB, 100% full
```

The guest handed its low pointer **across the API boundary** and Wine's native
path called `memmove`; Apple's optimised `memmove` then faulted on the
unmapped low address. Our handler serviced it correctly — which is the problem:
one memmove becomes one Mach exception per 32 bytes. The launcher never reached
the point of starting the game (**zero `008c` lines in the entire run**;
cycle=34 ≈ 68 s of wall time spent not progressing, peak 1230 MB vs baseline
1332).

**Verdict: handing a low address to the guest is not viable.** Emulating CPU
accesses does not make a low pointer usable by other APIs, exactly as Astra
said it would not. Making it viable would require translating every API
boundary that can receive a buffer pointer — a far larger surface than this
experiment, and not obviously bounded at all.

### 23.4 What this says about the two designs

The result discriminates between the options in §21.5/§22.5, and it does so
**against** the one just tried:

- **Give the guest a low address** (this experiment): the canonical pointer is
  low, so *every* consumer — guest VM, Wine, Apple's libc, potentially kernel
  I/O — meets an unmapped address. Tested; fails.
- **Keep the pointer high and translate only the narrowed form** (the
  demand-driven alias, §21.5): native and API consumers keep a real high
  address and never fault; only the 32-bit form the guest computes for itself
  needs servicing.

Astra rejected the alias approach with "do not fake a low alias by subtracting
4 GB", and I am **not** building it unilaterally — that instruction stands
until Astra revisits it. But the evidence has moved: the objection was raised
when the truncation premise was unproven (§19.1), and the experiment Astra
preferred has now failed for a reason that the alias approach structurally
avoids. Worth a re-read on that basis, with the collision hazard (§22.4: zero
low-32 collisions across 340 allocations in rdr31, one run only) as the open
risk rather than the premise.

### 23.5 ml968: shipped build is baseline-equivalent

The experiment is now **opt-in and OFF by default** (`MADEIRA_LOW_ALLOC=1` to
enable; the old `MADEIRA_NO_LOW_ALLOC` inverse gate is gone). All the machinery
is kept — registry, span-checked translation, per-peb qualification, ownership,
retirement, and every probe — because it is the substrate any future attempt
needs and the negative result should stay reproducible.

**Build:** `Madeira-ml968-lowalloc-optin.ipa` (73,575,954 bytes). Verified from
inside the IPA: ml968 gate x2, `MADEIRA_LOW_ALLOC` x2, old inverse gate gone
(0), ml966 infrastructure x9, ml961 x1.

Expected on the next run: no `LOW-ALLOC` lines, `ml968: ... OFF by default`,
and the game process back to its rdr31 behaviour — i.e. reaching the same
SEGV. That is the confirmation that the regression is gone, not progress.

## 24. Where this stands, and the question for Astra (2026-09-17)

### 24.1 Two new facts that bound the problem

**One primary fault, not a cascade of many.** Of rdr31's three SEGVs, only
`#1 addr=0x5cdb0010` is a truncated-allocation dereference. `#2 addr=0x0` and
`#3 addr=0xb3ed9e0050` match no allocation's low-32 form and follow #1 — they
are the guest's own handler running on already-broken state. So this is a
single deterministic blocker; fixing it should not simply move the crash to the
next of ten.

**The recovery target is unique here, but not structurally.** Across the 340
distinct allocations tracked in rdr31, exactly **one** matches the fault when
`+0x100000000` is applied (0x15cdb0000). But those allocations span **three**
high-32 bands — k=1 (79), k=0xb (231), k=0xd (30) — so `+0x100000000` is not
universally the right recovery, and zero low-32 collisions in this run is
empirical luck rather than a guarantee. Any recovery-by-search design has to
own that.

### 24.2 The design space after the negative result

- **A. Give the guest a low address.** Tested in rdr33. **Fails** — the guest
  passes the pointer across the API boundary and Apple's `memmove` faults on it
  (§23.3). Making it work needs translation at every API boundary that can
  receive a buffer pointer.
- **B. Recover the narrowed pointer on fault by searching allocations.** Astra
  vetoed ("do not fake a low alias by subtracting 4 GB"), and §24.1's three
  bands are a real strike against it: the search is not provably unique.
- **C. Pre-register a declared alias at allocation time (new, and the best of
  the three).** For a qualifying allocation, keep the guest's pointer **high**
  — so every native, libc and kernel consumer sees a real mapping and never
  faults, which is precisely what killed A — and at the same moment register a
  window mapping its low-32 form to the same backing, with the same ownership,
  span-checked bounds and retirement-on-free the ml966 registry already has.
  Nothing is guessed on a fault: both addresses are known when the allocation
  is made, and the low form is reserved rather than recovered. Collisions
  become a registration-time check (refuse the alias, keep the allocation) not
  a runtime ambiguity.

C differs from B in the way that matters to Astra's objection: B *infers* that
an invalid pointer "must have meant" a real one; C *declares* a second name for
memory we own, at the point we own it, and can refuse when it cannot be done
safely. It also structurally avoids A's failure, because the canonical pointer
stays high.

It is still in the family Astra vetoed, so it is **not** being built
unilaterally. The ask is narrow: does the objection to "faking a low alias"
extend to a pre-registered, ownership-tracked, refusable alias whose canonical
pointer remains high — given that the alternative Astra preferred has now been
measured and failed?

### 24.3 Recommended sequence

1. Astra rules on C (or names a fourth option).
2. If C is approved: implement behind an opt-in flag, default off, so the
   default path stays baseline-equivalent and the test cannot regress the
   desktop or the launcher the way rdr32/rdr33 did.
3. Acceptance: SEGV #1 at the truncated address disappears, the cascade (#2/#3)
   with it, and the game advances past this point. Success would not prove
   every upstream operation correct, and no window is promised.

### 24.4 Process note on this stretch

Worth recording honestly: across §19-§23 my three independent design calls —
the barrier-slot clobber hypothesis, the `movsxd` attribution, and the
qualification test — were all wrong, and Astra's caution was right each time.
Two of them cost a build. The pattern is that I have been fastest when doing
offline verification (disassembly, byte scans, log arithmetic) and slowest when
inferring a mechanism and acting on it. That is the argument for asking on C
rather than building it.

## 25. ROOT CAUSE: the JIT pool's RW alias occupies RDR2.exe's required ImageBase (rdr40, 2026-09-17)

### 25.1 ml975 worked

The `arm64x_check_call` bounds check landed and did its job: SEGV #1's pc moved off
the bitmap load (ntdll rva 0x56210) entirely. `ml939 UNHANDLED` stays 0, the four
`str q` are still emulated, and `[neon] ml974` never fired (so `ARM_NEON_STATE64`
is always available and the SIMD emulation was already sound).

The fault moved to **libarm64ecfex.dll rva 0x9f48**, `ldr x16,[x9]` — FEX's own
copy of the check, dereferencing the target to test for the fast-forward
sequence (x17 is built to `48 8b c4 48 89 58 20 55`). Same class, different
site: the bad target simply flowed to the next consumer that dereferences it.

Hardening that too would have been whack-a-mole, so the guest state was read
instead.

### 25.2 We are in call_tls_callbacks, and the callback pointer is un-relocated

`[redeliv-mem]` decodes to strings that name the location exactly:

```
x20 -> "call_tls_callbacks" and "Call TLS callback (proc=%p,module=%p,re..."
x19 -> Wine debug channel names ("relay", "loaddll")
x9  -> UTF-16 "it64.dll"   (tail of xtajit64.dll)
```

And the loader's own probe gives the mechanism:

```
008c alloc_tls_slot [tls-life] module=L"RDR2.exe"     base=0x1731C0000 callbacks=0x1432BA978
008c alloc_tls_slot [tls-life] module=L"xtajit64.dll" base=0xD72BC0000 callbacks=0xD72F3FA48
```

xtajit64's `AddressOfCallBacks` is inside its module; RDR2.exe's is not.
RDR2.exe spans 0x1731C0000-0x17A6E8000, but `0x1432BA978 - 0x140000000 =
0x32BA978` — an **un-relocated absolute address based at the preferred
ImageBase**. Wine dereferences it, reads garbage, and calls it as a TLS
callback. That garbage target is what every downstream site then faults on.

### 25.3 Why it is un-relocated: it cannot be relocated

RDR2.exe's headers, read from the install:

```
ImageBase          = 0x140000000
Characteristics    = 0x22      RELOCS_STRIPPED = False
DllCharacteristics = 0x8120    DYNAMIC_BASE    = False
BASERELOC          rva=0x0 size=0x0      <== NO relocation directory
TLS                rva=0x37dfe90 size=0x28
```

No relocation directory and no ASLR opt-in: the image **must** load at
0x140000000. Nothing downstream can repair its absolute pointers.

### 25.4 Why it did not get 0x140000000

```
JIT pool: RX=0x11913c000, RW=0x13913c000, size=512MB
[pool-va] SKIP scan over JIT pool at 0x139140000 -> 0x15913c000
[pool-va] anon_mmap_tryfixed addr=0x140000000 size=0x7528000 INSIDE RW pool off=0x61c4000
```

The RW alias is placed **adjacent to the RX pool** (RX + 512MB) and therefore
spans [0x13913c000, 0x15913c000) — which contains **0x140000000**, the default
ImageBase of every x64 Windows executable. The loader's fixed map there fails
and the exe is relocated to 0x1731C0000 despite having no relocations.

Correction for the record: the long comment block in
`app/Madeira/StikJITHelper.swift` discusses placing this alias at
0x7000000000 / 0x7fc8000000 / 0x7c00000000 and warns "DO NOT relocate this
alias without new evidence". Those are historical placements; the runtime value
is RX+poolSize, so the alias currently sits low and collides. I initially
mis-read the comment as describing the live placement.

### 25.5 Why this has not bitten before

Ordinary x64 executables ship a relocation directory, so being moved off
0x140000000 is harmless. RDR2.exe has none — the packer stripped it to pin the
base. So the collision was always there and only a non-relocatable image
exposes it.

### 25.6 The decision, which is Astra's

0x140000000 must be free before the Windows loader maps the main exe. The pool
base is constrained (>= 0x119000000, so the dispatcher at +0x7ffc130 keeps top
byte 0x12, which bounds RX to roughly [0x119000000, 0x128000000]), and RW is
RX + poolSize, so with a 512MB pool the alias lands on 0x140000000 for almost
any legal RX.

Options, none free:

1. **Move the RW alias only**, to just above the exe window (e.g. 0x150000000),
   leaving [0x140000000, 0x150000000) = 256MB for the exe. This is what the
   "DO NOT relocate" comment covers — but that comment demands *new evidence*,
   and this is new evidence. Its three measured alternatives were all far away
   (448G / top-of-space / 496G) and all regressed; a near placement has not
   been tried. The comment also admits the coupling between alias base and pool
   stability is **still unidentified**, so the risk is real but uncharacterised.
2. **Raise RX to the top of its legal range** (~0x128000000) so RW starts at
   0x148000000, leaving [0x140000000, 0x148000000) = 128MB. RDR2.exe needs
   0x7528000 = 122MB, so it fits with 6MB to spare — too tight to rely on.
3. **Shrink the pool** so RW ends below 0x140000000 (<= ~324MB). Contradicts the
   recorded rule that the pool is footprint-exempt and must not be shrunk on a
   guess, and 512MB is already down from 896MB.
4. **Alias 0x140000000 via the ml938 window mechanism** — rejected on
   inspection: that address is *mapped* (our RW alias), so guest accesses would
   silently read and write pool data instead of faulting. Silent corruption in
   both directions, strictly worse than the crash.

Recommendation: option 1, with the exe window sized generously rather than
minimally, and a self-check at pool-init that asserts the alias does not cover
[0x140000000, 0x140000000 + 256MB) so the collision can never come back
silently. But the "do not relocate" note is explicit and its hazard is
unidentified, so Astra should rule before it is implemented.

Status unchanged on device: pre-window, `.rpf` reads 0.

## 26. ml977: RDR2.exe LOADS AT ITS PREFERRED BASE. No crash, guest executing (rdr43, 2026-09-17)

### 26.1 ml976 was a regression; the cause was mine, not the guard's

ml976 tried a list of FIXED candidates for the RW alias. All six returned
**KERN_NO_SPACE (kr=3)** -- those ranges are occupied, so the non-overwriting
remap correctly refused to clobber anything. ml976 then `return nil`d, which
produced "JIT pool allocation FAILED -- not starting Wine" and no virtual
desktop. The refusal was right; aborting the launch over a placement preference
was the bug.

### 26.2 ml977: reserve first, then let the kernel choose

Inverted: reserve [0x140000000, +256MB) with `vm_allocate(FIXED)` *before* the
pool is placed, then ask for RW with `VM_FLAGS_ANYWHERE` exactly as before. The
kernel cannot pick a range overlapping a mapping we already hold, so adjacency
is ruled out without naming an address. `rwAddr` is seeded to 0x150000000 as a
floor hint so the search starts near rather than jumping far. Every failure is
non-fatal. Switches: `MADEIRA_NO_EXE_WINDOW=1`, and ntdll's release is gated on
a 64MB size floor.

Result:

```
ml977: reserved executable window [0x140000000,0x150000000)
ml977: RX=[0x1190bc000,0x1390bc000) RW=[0x300000000,0x320000000)
       offset=0x1e6f44000 windowHeld=true rwOverlap=false
```

RW landed at 12GB with a ~7.6GB alias offset, and FEX's
`DualMap::WriteOffset` handled it -- no pool coherence faults appeared.

### 26.3 The size floor worked exactly as designed

Eight small requests for the window were refused and the large one granted:

```
ml977: NOT releasing the window for 0x140000000+0x70000  (under the 64MB floor)   x8
       (0x60000, 0x70000, 0x100000, and 0x148000000+0x100000)
ml977: RELEASED the executable window to 0x140000000+0x7528000 (fixed-base main image)
```

First-come would have handed it to a 384KB relocatable image.

### 26.4 The decisive result

```
007c  RDR2.exe base=0000000140000000   <== ITS PREFERRED BASE
008c  RDR2.exe base=0000000157E90000
```

and 008c's callback traversal:

```
call_tls_callbacks module=0x157E90000 reason=1 count=1 first=0x140116990
```

**count=1 with a valid target**, against count=264 and
first=0x53057d6a6b0a01ff before. `AddressOfCallBacks` is still the on-disk
0x1432BA978 -- it was never corrupt, only un-relocated -- and it now resolves
to real image data because an RDR2.exe mapping exists at 0x140000000.

Run state: **0 SEGVs** (was 3), 0 `c0000005`, 0 redelivery terminations, and the
process was still alive when sampled. A guest thread runs at 35% CPU with
`rip=0x1318916b` (EMP.dll rva 0x18916b) while `wine-x18-exc` burns 65%
servicing sub-floor faults, and the serviced counter keeps climbing
(1,105,921 -> 1,118,209). Executing, not wedged.

### 26.5 The lifecycle hazard Astra predicted DID occur

The **bootstrap** (007c) claimed the window; the **final** game process (008c)
did not, and is instead resolving its absolute pointers through the bootstrap's
mapping at 0x140000000. Since all pseudo-processes share one address space and
it is the same executable, the code it reaches is correct -- which is why it
runs. But this is precisely the "do not share writable image state between
pseudo-processes" hazard: 008c's TLS callback is fetched from, and executes in,
another pseudo-process's image, whose `.data` is not its own.

Not claimed as fixed. What is owed, in order:
1. Transfer the window at bootstrap teardown so the final process claims it
   directly (`ios_jit_reclaim_process` retires pool copies, NOT the image view).
2. Decide whether two fixed-base live images should be refused outright with a
   diagnostic rather than silently sharing one mapping.

### 26.6 Where it actually is

Module count is **52 in both rdr36 and rdr43**, so the dependency graph was
already complete before -- that is not new. `.rpf` reads remain **0**, and the
7 top-level windows all belong to tid 0024 (the desktop); the game has created
none. So this is still pre-window: the crash family is gone and execution
continues inside EMP.dll's VM, dominated by fault servicing (~65% of a core in
the exception thread). Throughput of the sub-floor path, not correctness, is the
next visible wall.

## 27. The stall is a VM interpreter sweeping, not a spin — and my profiler is unreliable (rdr50-52, 2026-09-17)

### 27.1 State after ml977 + clearing the stale state file

`.rpf` reads 0, no game window (7 top-level, all tid 0024), 0 SEGVs, sub-floor
frozen at 978,945, memory flat ~1335 MB, and the process stays alive
indefinitely with 3-4 guest threads at ~35% CPU each.

The stale 0-byte `15a3.bin` mattered: renaming it aside removed the
`STATUS_SHARING_VIOLATION` (0 in every run since) and moved the stall point
forward. EMP.dll then creates its own `15a3.bin` and **never writes it** (zero
`NtWriteFile` in the entire run), so the 0-byte file is a CONSEQUENCE of the
stall that then becomes a CAUSE of the next run stalling earlier. It must be
cleared before each experiment, and the real fix is that a stalled run should
not leave one behind.

### 27.2 THREE failed probe iterations -- record them so they are not repeated

Chasing "tight loop or slow sweep?" cost three runs, all my errors:

1. **ml979** -- no output at all. I skipped `thread_suspend()` around
   `thread_get_state()`, reasoning that a sampling profiler tolerates torn
   reads. On Darwin that is wrong: state is not reliably returned for a thread
   that is not suspended, and the ml876 sampler immediately above has always
   suspended for exactly that reason. 600 samples resolved nothing.
2. **ml980** -- found 2-3 guest threads, resolved ZERO RIPs, six profiles
   running. Cause: `ios_native_rip_from_hostpc()` INFERS the guest RIP from the
   host pc plus block metadata, so it only works when the pc sits inside a block
   with an intact tail. At arbitrary sample points it is usually in a dispatcher
   or thunk. This also explains why all earlier RIP data was so thin -- ml876
   uses the same helper and resolved **2 RIPs in a 15-minute run**, so earlier
   conclusions were drawn from 3 data points.
3. **ml981** -- produced a confident-looking but GARBAGE profile:
   `threads=1 samples=200 buckets=1 span=0x0 [0x9a4c14f00..0x9a4c14f00] 100%`.
   That address is at the **end boundary of kernelbase.dll**, constant across
   400 samples, while ml876 in the same run resolved real guest RIPs inside
   EMP.dll at 35% CPU. So it selected one NON-guest thread (x28 is just a
   callee-saved register there, and x28+0x18 landed on a stale pointer) and
   missed the real guest threads. The plausibility gate
   (`> 0x10000 && < 0x8000000000`) is far too loose to tell those apart.

Lesson worth keeping: the frame layout (`x28+0x18` = rip, then gregs[0..15]) is
only valid when x28 actually points at a FEX CpuStateFrame. Identifying a guest
thread needs a positive test for that, not a range check on whatever the offset
happens to contain.

### 27.3 What the existing ml876 data does establish

Aggregating every guest RIP ml876 resolved across rdr44-rdr52:

```
rva 0x157cb2 0x15c8d1 0x1665f1 0x186586 0x18916b 0x18cdd9
    0x1933bc 0x195f5a 0x19ac27 0x1ab5fd 0x1b0b46 0x1cca05
```

12 samples, **12 distinct addresses**, spanning 0x74d53 (~467 KB) = ~42% of
`.emp1` (rva 0xfc000..0x20c6fc). Within a single 1-second burst in rdr48 the
four samples alone covered ~230 KB of range.

**So it is not a tight spin on a failing check.** It is exactly the shape an
obfuscation VM produces: a dispatch loop whose bytecode handlers are scattered
across the code section, so each sample lands in a different handler.

### 27.4 What it does NOT establish, and the measurement that would

A wide RIP spread proves "a VM interpreter is running". It does **not**
distinguish a long computation that terminates from a large loop that never
does -- both scatter identically across the handler table. And the supporting
evidence is ambiguous: 42% coverage suggests real work, but flat memory, zero
I/O and a frozen sub-floor counter suggest nothing is being produced.

The right measurement is the VM's OWN program counters, not the host RIP.
Section 21.2 disassembled the interpreter and identified them:

```
1313bd8b  mov   rdx, qword ptr [r10]     ; r10 = bytecode cursor
1313bda8  add   r10, 6                   ; advances by 6
1313bdc3  mov   ebp, dword ptr [r9]      ; r9 = second cursor
1313bdc7  add   r9, 4                    ; advances by 4
```

Sample **guest r9 and r10** over time. Monotonically advancing cursors mean it
is executing a long program and will finish; cursors that revisit the same
values mean a loop, and the values themselves then bound the loop body. The
registers are reachable at `x28 + 0x20 + 8*n` (gregs follow the rip at +0x18),
so r9 is `x28+0x68` and r10 is `x28+0x70` -- but only once guest-thread
identification is made sound (27.2 item 3).

### 27.5 Recommendation

Do not build a fourth probe iteration blind. Two things are worth Astra's
offline judgement:

1. **A sound guest-thread test.** What positively identifies a thread as
   running under FEX, rather than range-checking a speculative offset? If FEX
   exposes a per-thread frame registry or a magic/signature field in the
   CpuStateFrame, that is the test to use.
2. **Whether cursor sampling is even the right question.** If the VM is
   genuinely interpreting a long protection program, the answer is throughput
   (we pay ~65% of a core in the exception thread) rather than any missing
   condition -- and that is a different piece of work from hunting a stalled
   wait.

Status honestly: a whole crash family is gone and the process survives
indefinitely, which is real. But we are still in loader/anti-tamper
initialisation, before the game's entry point, with no archive loading and no
window, and I cannot put a number on the distance to renderer init.

---

## §28 — rdr53: the stale-image bug, measured end to end. Root cause is the PREFERRED BASE, not the IAT handoff

Run: 16,601 lines, 7s wall, 15 footprint cycles, peak 1330 MB. Terminated by our own
ml465 guard (2000 identical redeliveries), not by a hang.

### 28.1 Fix 1 (ml982) is confirmed working — acceptance met

Astra's acceptance test was "the exception server remains serviceable after an
intentionally rejected diagnostic read". It is:

* All **6** `[mach_exc] UNHANDLED` faults ran their **complete** diagnostic
  sequence (`store-noalias`, `av-detail`, `fault_rip`, `pool-ledger`,
  `rsp-trunc`, `guest-code`, `ec-fault-regs`, `caller_insn`, `insn_stream`,
  `sym`, 17 × `exit-stk`, `vm_region`, `x86_seg`, `x86_live`, `x86_callret`).
  Previously the handler self-faulted partway through and parked at 0 CPU.
* `[mach_exc] sym pc=?`?+0x0 lr=?`?+0x0` — printed 6/6. This is the exact line
  that used to wedge: `dladdr` fails here, and before ml982 the uninitialised
  `Dl_info` was dereferenced anyway. Now `pc_named == 0` prints placeholders and
  execution continues.
* `segv_handler` then ran and delivered to the guest (SEGV #1, #2), and the
  process died through the normal path.
* `ml982` marker count = **0**: no read had to be rejected, so the *hardening*
  never fired. What fixed the wedge was the `memset` + retained `dladdr` return,
  not the checked-read helper. Worth stating plainly — a 0 count here is not
  evidence the change was unnecessary.

### 28.2 The real defect: RDR2.exe cannot be relocated, and the window went to the wrong generation

Offline from the PE on the phone prefix (free, exact):

```
ImageBase          = 0x140000000
SizeOfImage        = 0x7528000
DllCharacteristics = 0x8120   -> HIGH_ENTROPY_VA | NX_COMPAT | TERMINAL_SERVER_AWARE
                                 DYNAMIC_BASE (0x0040) is CLEAR
Data directories   : IMPORT, RESOURCE, EXCEPTION, DEBUG, TLS
                     ** no BASERELOC directory at all **
TLS dir RVA 0x37dfe90 -> VA 0x1437dfe90 (absolute, ImageBase-relative)
```

So RDR2.exe has no relocation table *and* opts out of ASLR. Mapping it anywhere
other than 0x140000000 leaves every absolute VA inside it pointing at
0x140000000.

Two generations of RDR2.exe run in this log:

| gen | tid | PEB | main image | outcome |
|-----|-----|-----|-----------|---------|
| A | 007c | 0x95de10000 | **0x140000000** (claimed the ml977 window) | `NtTerminateProcess` inside **launc.dll**, `abort_process`, reclaimed |
| B | 008c | 0x95df24000 | **0x158110000** | ran the fault, died |

`[main-exe] virtual_map_module = 0x40000003` for gen B — `STATUS_IMAGE_NOT_AT_BASE`.
Wine said so out loud and nothing acted on it.

Gen A's process was reclaimed (`[jit-pool] RECLAIM peb=0x95de10000: 54 ranges …
52 mappings tombstoned`) but **its image view was never unmapped** —
`[phys-map] 0x140000000+0x7528000 dirty=116 MB res=117 MB` is still there at the
end, alongside gen B's own `0x158110000+0x7528000`. 232 MB for two copies of one
117 MB image.

### 28.3 The exact execution path into the corpse

```
008c call_tls_callbacks  module=0x158110000  reason=1 count=1 first=0x140116990
                         ^ gen B's image                       ^ gen A's code
```

`alloc_tls_slot` prints `callbacks=0x1432ba978` for **both** generations —
identical, un-rebased. Gen B read its TLS directory, got gen A's
`AddressOfCallBacks`, and because gen A's image was still mapped the pointer
**resolved instead of faulting**. Everything after that ran in the corpse:

* `[jit-pool] image 0x140000000+0x7528000 → pool 0x11c0bb000` appears a second
  time (line 7847) — a *fresh* copy off the freelist (`reused freed range
  off=0x2ae0000`, exact-fit, hence the same pool address), not a stale cache hit.
* ~100 × `[iat-sync] region 0x140…/0x146…/0x147… (owner=0x95df24000)` — we
  re-synced gen A's IAT under gen B's ownership.
* Then a slot that could not be translated: `kernelbase!VirtualAlloc` at
  **0xbc8c14f00** = gen A's kernelbase (`[jit-pool] image 0xbc8b90000+0x2f0000
  (kernelbase.dll)`), whose pool copy was reclaimed with gen A. Gen B's own
  kernelbase is at 0xbc46e0000.

### 28.4 Why the fault is unrecoverable (corrects the "reclaimed translations" framing)

```
[fault_rip] rip=0xbc8c14f00 pc=0xbc8c0829c kr=2(PROTECTION_FAILURE)
            region=0xbc8ba0000+0x80000
vm_region:  prot=0x1  max_prot=0x3  offset=0x10000  shared=0
[guest-rip] base=0xbc8c08000 alloc=0xbc8b90000 state=0x1000 protect=0x20 (PAGE_EXECUTE_READ)
insn_stream PC-12..PC+8: ad4127e8 acc59fe6 d61f0020 [adba9fe6] ad0127e8 ad022fea ad0337ec
```

* `0xbc8c14f00 − 0xbc8b90000 = 0x84f00` — Astra's VirtualAlloc export RVA, confirmed.
* `adba9fe6` = `stp q6, q7, [sp, #-0xb0]!` — the entry thunk's first instruction,
  matching `Module.S:289–300`. The bytes are correct and present. PC−4 is
  `d61f0020` (`br x1`) — the tail of the preceding thunk; the table is intact.
* **Wine's view says PAGE_EXECUTE_READ; the Mach mapping is `R--` with
  `max_prot = RW-`.** It is file-backed (`offset=0x10000`, `shared=0`). Per
  `reference_wx_file_backed_mappings`, iOS can never grant EXECUTE on a
  file-backed mapping — so `mprotect` cannot fix this and no retry will.
* This is therefore **not** "the bootstrap's translations were reclaimed". Every
  PE image view is `R--` by design in this architecture; code executes only from
  the pool. The defect is that control reached a *PE* address whose pool copy
  belongs to a dead process, so `[guest-code] … NO pool copy for this address
  (identity translate)` handed execution to a page that can never be executable.

`x86_live` shows the call was perfectly well-formed:
`RCX=0 RDX=0x6000 R8=0x3000 (MEM_COMMIT|MEM_RESERVE) R9=0x4 (PAGE_READWRITE)` —
`VirtualAlloc(NULL, 0x6000, …)` from RDR2's own TLS callback. The game's code was
running and doing the right thing.

`[x86_callret]` chain: `launc.dll`-era frames (0xbc243…) → `0x1401169a9`
(TLS callback + 0x19) → `0x140b0823d` → VirtualAlloc.

### 28.5 ml983 — the fix (built, deployed, unlaunched)

Astra's fix 2 is right in substance; the mechanism is the reverse of "adopting
the bootstrap's IAT". Gen B *did* map fresh and resolve its own imports — that
was useless, because a non-relocatable image at the wrong base is a decoy that
the previous generation's corpse makes functional.

`ios_exe_win_claim` previously returned 0 forever once the window was released.
It now re-grants:

1. `ios_exe_win_note_occupant()` records base/size/generation at each grant.
2. `ios_exe_win_note_owner(module, peb)` — called from `init_peb` in `env_ios.c`,
   the first point where the owning PEB and the main module base are both known
   (the image is mapped **before** the PEB is published, so the mapping thread
   cannot name its owner).
3. `ios_exe_win_note_dead_peb(peb)` — called from the **tail** of
   `ios_jit_reclaim_process`, deliberately after the ledger walk: the flag is
   what authorises another thread to delete the view, so it must not be
   observable while that PEB's pool mappings and anon aliases are still live.
   Completing the walk is a stronger quiescence statement than server EOF.
4. `ios_exe_win_retire_dead_image()` — takes `virtual_mutex` via
   `server_enter_uninterrupted_section`, requires `find_view` to match the
   recorded interval **exactly**, then `delete_view` (so `unmap_area`'s
   reserved-area handling, the per-page vprot bytes and `clear_arm64ec_range`
   stay consistent). `delete_view` does no server I/O — which matters, because
   this runs after the dead owner's `fd_socket` is closed. `unmap_view_of_section`
   would not be safe here for that reason.
5. Live previous owner ⇒ `ml983: CONFLICT … declining`.

Scope guards, all deliberate:
* Only `[0x140000000, 0x147528000)` is touched — never the 256MB window. Thread
  stacks (0x147f…, 0x148…) and relocatable images sharing the default ImageBase
  live inside it and must survive.
* Re-grant requires an exact base+size match, so it cannot fire for the desktop
  (all pre-RDR2 requests for 0x140000000 are 0x2c000–0x100000, under the 64MB floor).
* Every rejection path logs and falls back to today's behaviour — the failure
  mode is degradation, not corruption.

**Expected next-run evidence:** `ml983: gen 1 … owned by peb=…` → `now
ownerless` → `delete_view` → `RE-GRANTED` → gen B's `[Wine child] PE loaded:
ImageBase=0000000140000000`, `virtual_map_module = 0` (not 0x40000003), and
`call_tls_callbacks module=0x140000000 first=0x140116990` with module and
callback in the **same** image.

### 28.6 Still open / not addressed here

* ml982's rejection-path regression cases (unknown PE PC, unmapped code pointer,
  short read across a page, valid native symbol) are still untested in a harness.
* `ios_exe_win_*` statics are read/written from two threads without a barrier.
  Measured ordering is safe (reclaim at line 4209, next map at 4501) and a stale
  read only declines, but it is unsynchronised.
* `[fdtrace] CROSS!` on comm-pipe closes at thread death is still a false report.
* `build/wine-pe/build-ntdll.sh`'s padding assertion is still broken.

---

## §29 — rdr56/rdr57: an UNEXPLAINED silent death between two adjacent dprintfs

This section exists because I cannot account for it and would like another pair
of eyes. Everything below is measured, not inferred.

### 29.1 What the probes settled (good news first)

`ml985`, an ungated probe at the top of `map_image_view` for images >= 64MB:

```
ml985: map_image_view size=0x7528000 base=0x140000000 info_base=0x140000000
       map_addr=0 flags=0 charact=0x22 limits=0x10000..0x73fffeffff
```

* **`flags=0`** -- `IMAGE_FLAGS_ImageDynamicallyRelocated` (bit 2) is CLEAR for
  RDR2.exe, confirming the PE header reading in §28.2 from the runtime side.
  The desktop's images print `image_flags=0x4` for contrast. So the original
  ml984 gate was semantically CORRECT.
* `base` is non-NULL and equals `info_base`, `map_addr=0`. So `map_image_view`
  does reach `if (base)` and `map_view( base )` for the main exe.
* rdr56 also revealed a self-inflicted blind spot: the refusal probe's
  `failn++ < 12` budget was consumed by 12 desktop refusals before the main
  image reached it. "Count events, not log lines" in a new costume; now uncapped
  for >= 64MB.

### 29.2 The anomaly

Both rdr56 and rdr57 end with exactly this as the final line of the log:

```
ml983: delete_view 0x140000000+0x7528000 protect=0x180002d (dead owner's fixed-base main image; the rest of the window is untouched)
```

The code at that point was:

```c
        dprintf( 2, "ml983: delete_view %p+%#lx protect=%#x ...", ... );   /* printed */
        ios_retiring_stale_image = 1;                                       /* a store */
        delete_view( view );                                                /* a call */
          -> if (ios_retiring_stale_image)
                 dprintf( 2, "ml986: delete_view step1 unmap_area ..." );   /* NEVER printed */
```

rdr57 was built specifically to print that `step1` breadcrumb. It did not appear.
Checks performed, all negative:

| check | result |
|---|---|
| Installed dylib identity | sha256 **identical** to the local build |
| `ml986` strings in installed dylib | **7** (deploy asserts it) |
| `ml986` strings in `virtual.o` in the archive | present at 0x298d9 etc. |
| Number of `delete_view` definitions | exactly **1** |
| Log line truncated? | no -- complete, ends with `\n`, no NUL padding |
| fd 2 buffered/piped? | no -- `WineProcessBridge.m:597 dup2(logfd, STDERR_FILENO)`, so `dprintf(2,...)` is a direct write to the log file. Writes are durable; the "lost tail" theory is REFUTED |
| SIGSEGV/SIGBUS blocked in the critical section? | no -- `server_block_set` is ALRM/IO/INT/HUP/QUIT/USR1/USR2 only, so a fault there would have been reported by our handlers |
| Process still alive (hang rather than crash)? | no -- `ps` shows nothing |
| iOS crash report | none for this app, ever, on this VM (ReportCrash is not capturing it) |

So: the process died between two adjacent `dprintf` calls separated only by a
static store and a function call, on a thread whose fault handlers are live, with
no crash report and no output. I have no mechanism that explains this.

The best remaining hypothesis, unconfirmed: `free_ranges_remove_view` (reached
via `unregister_view`, step 4) contains **five `assert()`s**, one preceded by
`ERR("range %p - %p is already partially unmapped")`. `ERR` is swallowed by
MADEIRA_QUIET and `abort()` leaves nothing. That would explain a silent death --
but NOT why the step-1 breadcrumb (before `unmap_area`, three statements
earlier) also failed to appear. Unless the breadcrumb itself never executed,
which brings back the same wall.

**Ask for Astra:** is there a mechanism by which a `dprintf(2, ...)` inside a
function called from `virtual_map_image`'s `server_enter_uninterrupted_section`
can fail to reach an fd that is a plain file, while the `dprintf` immediately
before it succeeds? Or a way the call itself dies before the callee's first
statement (stack, PAC/BTI on the inlined call, something in the ARM64EC
hybrid-code path)?

Related precedent worth re-reading: rdr55's `ml984` diagnostic also never
printed on a path the evidence says was taken, with the same verification
results. Two occurrences now.

### 29.3 What I changed instead (ml987) -- and what it does NOT claim

The retirement is **moved off that path entirely** rather than instrumented
further. It now runs in `ios_retire_own_fixed_base_image()`, called from
`process_exit_wrapper` BEFORE the master socket closes:

* on the **owner's own thread**, with its own PEB current, so
  `NtUnmapViewOfSection( NtCurrentProcess(), base )` is the ordinary supported
  call and not a foreign-process edit;
* while the **server connection is still live**, so the
  `SERVER_START_REQ(unmap_view)` inside it succeeds. The old approach called
  `delete_view` directly and left wineserver's own `memory_view` for the dead
  image in place -- an inconsistency I introduced and never reported;
* **not** nested inside `virtual_map_image`'s uninterrupted section.

On success the interval is re-reserved `PROT_NONE` and recorded in
`ios_exe_win_held_base/size`; `ios_exe_win_claim` releases **exactly that
interval** and nothing else. The 256MB window is never unmapped wholesale --
`ios_exe_win_claim`'s existing `vm_deallocate` of the full window is safe only on
the first release, before live stacks land at 0x147f.../0x148...; re-arming
`ios_exe_win_state = 1` would have been a landmine and is deliberately not done.

`MADEIRA_NO_IMAGE_RETIRE=1` disables it.

Deleted outright: `ios_exe_win_retire_stale_occupant`,
`ios_exe_win_retire_dead_image`, `ios_exe_win_reclaim_for_fixed_base`,
`ios_exe_win_regrant_inflight`, and the `map_image_view` retry. The ml985 probes
stay.

**This is a relocation of the work, not an explanation of §29.2.** If rdr58 dies
the same way at teardown, the mechanism is in `delete_view` on this image and
§29.2's question is the one to answer. If it survives, §29.2 is still open and
should be treated as a latent hazard, because whatever it is was not fixed.

### 29.4 Also fixed

`scripts/deploy-vm.sh` cleared the log BEFORE checking whether the transfer
succeeded. A failed deploy therefore handed the next launch a fresh log written
by the OLD binary -- indistinguishable from a deployed build that changed
nothing, which is precisely what that script exists to prevent. The clear now
happens only on success, and a failure says the installed build is unchanged.
This bit once already this session (an ssh auth hiccup, `marker: 0`).

---

## §30 — ml988/ml989: Astra's ml987 review accepted in full

All three defects confirmed in source before changing anything:

* `anon_mmap_fixed` really is `mmap(..., MAP_FIXED | ...)` at virtual_ios.c:5005
  -- **overwrite**. My ml987 re-reservation could have destroyed an unrelated
  allocation that landed in the gap after `NtUnmapViewOfSection` dropped
  `virtual_mutex`. Astra's isolated native test reproduced exactly that.
* `unix_init_startup_info()` (which runs `map_image_view`) precedes
  `init_thread_stack( teb, 0, 0, 0 )` in `loader_ios.c` -- confirmed by reading
  3262-3290. So my rdr57 claim "SIGSEGV isn't blocked, therefore a fault would
  have been reported" is **unsound**: that thread is not yet at a stage with a
  usable guest exception path.
* A complete final log line does not prove its `dprintf` **returned**. I treated
  "line ends with \n" as proof the call completed. It is not.

### 30.1 ml988 -- synchronized ownership handoff

State machine, `ios_exewin_lock` as a strict leaf:

```
FREE -> OWNED            (claim committed after a SUCCESSFUL map)
OWNED -> RETIRING        (owner exiting; claims refused, address NOT released)
RETIRING -> HELD_NOT_READY   (image unmapped, exact interval held no-overwrite)
HELD_NOT_READY -> HELD_READY (after ios_jit_reclaim_process)
HELD_READY -> CLAIMING   (interval released to one claimant)
CLAIMING -> OWNED | HELD_READY   (commit, or rollback re-holding the interval)
```

* **Phase 1** `ios_retire_own_fixed_base_image()` -- still in the exiting
  owner's context before the socket closes, because
  `SERVER_START_REQ(unmap_view)` needs a live connection. Validates
  `state == OWNED && dying_peb == owner` under the lock, snapshots the exact
  base/size/generation, transitions to RETIRING, then **releases the lock**
  before `NtUnmapViewOfSection` (which takes `virtual_mutex`). Lock order is
  acyclic: `claim()` runs with `virtual_mutex` held and then takes the leaf;
  retirement never holds the leaf while acquiring `virtual_mutex`.
* The hold is **`anon_mmap_tryfixed`** (no-clobber). If the interval was taken
  in the gap, that is reported as `ml988: LOST the interval ... Leaving it
  alone; a MAP_FIXED hold here would have destroyed it` and the state goes FREE.
  Never overwritten.
* An unmap refusal returns to OWNED and says so, rather than pretending.
* **Phase 2** `ios_exe_win_mark_ready()` -- called from `process_exit_wrapper`
  **after** `ios_jit_reclaim_process`. Documented in the code as NOT a proof
  that every native thread stopped (`thread_ios.c:1764`: a non-main-thread exit
  can leave siblings running; the pool reuse grace is a delay, not a lifetime
  proof) -- it is the strongest barrier currently available and strictly
  stronger than the server EOF ml987 effectively relied on.
* A claimant arriving while `state != HELD_READY` gets a **named conflict**:
  `ml988: CONFLICT -- ... the handoff is state=%s, not HELD_READY; ... the image
  will be placed elsewhere, which for a non-relocatable PE is NOT equivalent`.
  No silent fallback.
* Claims are **pending** until `map_image_view` calls
  `ios_exe_win_commit_claim( base, size, mapped )`. On failure the interval is
  re-held no-overwrite and the state returns to HELD_READY; no phantom owner.
* Only `[0x140000000, 0x147528000)` is ever touched. `ios_exe_win_state = 1` is
  deliberately never re-armed, because `ios_exe_win_claim`'s existing
  `vm_deallocate` covers the whole 256MB window and is safe only on the first
  release, before live stacks land at 0x147f.../0x148....

Deleted: `ios_exe_win_retire_stale_occupant`, `ios_exe_win_retire_dead_image`,
`ios_exe_win_reclaim_for_fixed_base`, `ios_exe_win_regrant_inflight`, the
`map_image_view` retry, and the ml986 `delete_view` instrumentation.
`MADEIRA_NO_IMAGE_RETIRE=1` disables the handoff.

### 30.2 ml989 -- the two diagnostics Astra asked for

**Allocation-free markers.** A pre-opened fd to `Documents/madeira-retire-trace.txt`
and fixed 4-byte `write()`s, no formatting, no locks, no allocation, with the
write result retained and reported. Independent of fd 2 entirely:

| marker | meaning |
|---|---|
| `U0>` | about to call `NtUnmapViewOfSection` |
| `U1<` | it returned |
| `D0>` | `delete_view` entered, **before any field of `view` is read** |
| `D1u` | `unmap_area` returned |
| `D2v` | `set_page_vprot` returned |
| `D3e` | `clear_arm64ec_range` step passed |
| `D4r` | survived `unregister_view` / `free_ranges_remove_view`'s 5 asserts |
| `D5<` | `delete_view` complete |

`D0>` present with `D1u` absent isolates the unmap; `D0>` absent with `U0>`
present isolates call entry; both absent with the dprintf line written isolates
printf/return failure. The rdr57 observation could not separate these.

**Running-image identity.** `ios_log_running_image_identity()` walks the loaded
Mach-O's load commands and logs `LC_UUID`, base, path and pid at `virtual_init`.
Disk sha256 does not identify a running mapping -- that was my evidence twice
and Astra is right that it proves nothing. Expected UUID for this build:
`2A372288-3914-34BA-BBD9-9F1B168046D3`.

### 30.3 Still owed from Astra's plan

* Her step 4: a synthetic-PE fixture exercising map -> server unmap -> held
  reservation -> fresh same-base map, with sentinel allocations immediately
  outside the image, a concurrent claimant and an injected map failure. Not
  built yet; the ownership change is currently only validated on the live path.
* External native stop/termination reason capture. No iOS crash report is
  produced for this app on this VM at all, so this needs another mechanism.
* The rdr57 termination cause remains **unresolved**. ml988 relocates and
  hardens the work; it is not a diagnosis. If ml989's markers stop at `D0>`/`D1u`
  the mechanism is in `delete_view` on this image.

---

## §31 — rdr58: §29's "unexplained death" was STALE CODE. §29.2 is withdrawn.

### 31.1 The finding

rdr58's log is a single cold-looking run (first line `CS_DEBUGGED flag` at
04:49:17) and it printed:

```
ml983: window occupant is now gen 1 at 0x140000000+0x7528000
ml987: this process owns the fixed-base main image ... retiring it through NtUnmapViewOfSection
ml987: NtUnmapViewOfSection(0x140000000) = 0
```

`ml983: window occupant` is wording that **ml988 deleted**, and `ml987` strings
do not exist anywhere in the installed bundle (`grep -rl ml987` over the whole
`Madeira.app` returns nothing; the dylib has `ml988=12 ml989=5 ml987=0`,
installed 04:39, i.e. ten minutes BEFORE the run). A process cannot print a
string its mapped image does not contain.

Mechanism, then confirmed directly: **`LogStore.init` runs on RESUME as well as
on cold start.** It rotates/recreates `madeira-log.txt`, so a resumed process
emits a brand-new log with a current mtime and the usual `CS_DEBUGGED` first
line -- indistinguishable from a fresh process. Replacing a file never affects a
process that already has it mapped. Adding `killall -9 Madeira` to
`deploy-vm.sh` reported **`was_running=1 now=0`**: the app was still resident.

(My own check minutes earlier said it was not running. `ps aux | grep -i madeira`
returns nothing on this VM; `ps ax | grep '[M]adeira'` finds it. Do not trust the
first form.)

### 31.2 What this retroactively explains

| run | anomaly | consistent with |
|---|---|---|
| rdr55 | no `ml984` line on a path the evidence said was taken | the **ml983** build -- re-grant at the mmap layer, never reached, gen 2 at 0x158120000, which is exactly what rdr55 showed |
| rdr57 | `ml983: delete_view` then total silence, `ml986 step1` absent | the **ml985** build -- that breadcrumb did not exist in it |
| rdr58 | `ml987` strings absent from the bundle | the **ml987** build |

**§29.2 is withdrawn.** There was no silent death between two adjacent
`dprintf`s: the second `dprintf` was not in the binary that ran. Astra's
disassembly of the preserved ml986 object was correct -- the object was fine, it
was simply never loaded. Her refusal to accept the
`free_ranges_remove_view`-assert story was the right call, and her insistence on
identifying the *running* image rather than the file is exactly what closed this.
Roughly five runs went into chasing a phantom.

Note the one thing rdr57's death and this share: nothing is yet known about
whether the ml985-era `delete_view` call would have died. It was never
instrumented, because the instrumented build never ran.

### 31.3 The one real result rdr58 did produce

```
ml987: NtUnmapViewOfSection(0x140000000) = 0
```

From the **teardown context**, on the owner's own thread with the server
connection live, the unmap **succeeds**. That is new and it validates Astra's
choice of context for phase 1. Under ml987 the log then stops, i.e. at or just
after the `anon_mmap_fixed( ..., PROT_NONE, MAP_FIXED )` re-reservation -- the
exact defect Astra found and reproduced in her isolated native test. ml988
already replaced that call with no-overwrite `anon_mmap_tryfixed`, so the next
cold run exercises the fixed form.

### 31.4 deploy-vm.sh hardening (this is now load-bearing)

* Kills any running/suspended instance FIRST, then re-checks, and **fails** if
  anything survives -- a launch after a surviving instance would keep the old
  mapped dylib.
* Prints `was_running=N now=N` so the transition is in the record.
* Still verifies sha256 per file and asserts the marker string.
* Clears the log only on success (§29.4).

Disk sha256 identifies a FILE and never a running mapping. ml989's
`LC_UUID` + pid line at `virtual_init` is the durable check; expected for the
current build: `2A372288-3914-34BA-BBD9-9F1B168046D3`. **Read that line before
believing anything else in a log.** A string that a build *removed* is the
strongest tell of all.

---

## §32 — rdr59: THE OWNERSHIP HANDOFF WORKS, AND RDR2 CREATES A D3D12 DEVICE

First run of the ml988/ml989 build (`ml989: running image ... LC_UUID=2A372288-3914-34BA-BBD9-9F1B168046D3`
matches the deployed build -- that check is now mandatory before reading anything else).

### 32.1 The fixed-base handoff completed, exactly as designed

```
ml988: fixed base 0x140000000+0x7528000 CLAIMING (pending a successful map)
ml988: fixed base 0x140000000+0x7528000 is OWNED by gen 1 (map succeeded)
ml988: gen 1 owns ... and is exiting -- OWNED -> RETIRING
ml988: NtUnmapViewOfSection(0x140000000) = 0
ml988: ... HELD PROT_NONE (no-overwrite) -- RETIRING -> HELD_NOT_READY
ml988: pool mappings for peb=0xad6474000 reclaimed -- HELD_NOT_READY -> HELD_READY
ml988: releasing the held image interval 0x140000000+0x7528000 kr=0 (HELD_READY -> CLAIMING)
ml988: fixed base 0x140000000+0x7528000 is OWNED by gen 2 (map succeeded)
[main-exe] virtual_map_module = 0x0                       <-- NOT 0x40000003
[Wine child] PE loaded: ... ImageBase=0000000140000000     <-- the goal
```

Every transition fired in order. **The final RDR2 process owns its preferred
base.** `call_tls_callbacks module=0x140000000 first=0x140116990` -- module and
callback in the SAME image, so the corpse-execution failure of rdr53..rdr58 is
gone. Consequences:

* **0 faults.** `UNHANDLED=0 SEGV=0 BUS=0 redeliv=0 c0000005=0`. Every prior run
  had 6 UNHANDLED and died through the ml465 guard.
* One image copy instead of two: `[phys-map] 0x140000000+0x25b0000 dirty=37 MB`
  (was 116 MB x2). Peak footprint 1218 MB.
* 55 footprint cycles (~110 s) and still resident.

The ml989 markers also close §29 for good: `U0> D0> D1u D2v D3e D4r D5< U1<` --
every `delete_view` step returned, including `D4r` past
`free_ranges_remove_view`'s five asserts. There was never anything wrong with
`delete_view`.

### 32.2 RDR2 reaches graphics init and CREATES A D3D12 DEVICE

It loaded `d3d12.dll`, `dxgi.dll`, `winemetal.dll`, `madeira_d3d12.dll` and:

```
[madeira-d3d12] D3D12CreateDevice(adapter=0000000000000000, feature level 0xb000, create)
[wmt-remote] ml762 REMOTE MODE via 192.168.64.1:47821
[wmt-remote] ml881 newResidencySet: op=77 status=0 handle=0x8000000d00000002
[madeira-d3d12] residency set: attached to the queue
[madeira-d3d12] device created: madeira-d3d12 M2 Sep 16 2026 16:51:10 [arm64ec]
[madeira-d3d12] destroyed Device
```

Feature level 0xb000 = D3D_FEATURE_LEVEL_11_0. Create -> query -> destroy is a
capability probe; the game then reloaded dxgi/winemetal for the real
initialisation. The native D3D12 path served it.

It also created real Win32 windows on its own thread:
`[winios-tree] 0x3005e "Error" style=94c801c4 vis=1 tid=008c win={313,209,647,330}`
with an icon, a text static and an OK button -- that is the on-screen
"Red Dead Redemption 2 exited unexpectedly!" dialog, drawn by the game.

### 32.3 The new wall: a 28 GB VA reservation, and why it is OUR bug

```
[jumbo#1] ... nrip=0x1425a589a size=0x700000000 (28672 MB) hint=0x0 -> 0x0 st=0xc0000017
[alloc-fail] ml814 #1 status=c0000017 size=0x700000000 type=0x2000 protect=0x1
[mmap-tag] ml815 #1 BOTH FAILED size=0x700010000 untagged errno=12 tagged errno=12
[va-gaps] FREE 0x0..0x100a34000 = 4106 MB
[va-gaps] FREE 0x34b404000..0x458000000 = 4299 MB
[holes<64G] free=6603 MB largest=4299 MB       (steady across all 55 cycles)
```

RDR2 asks for **28,672 MB of MEM_RESERVE | PAGE_NOACCESS** (`type=0x2000
protect=0x1`), `hint=0`, from its own code at RVA 0x25a589a. Largest available
hole: 4299 MB. Total free: 6603 MB.

**Why it asks for that much is the accuracy gap.** From the same log:

```
[va-profile] ml749 post-limit TASK_VM_INFO.max_address=0xfc0000000 (63.0 GB)
[va-profile] ml749 post-limit host_addr_space_limit=0x1000000000
             address_space_limit=0x7fffffff0000 user_space_limit=0x7fffffff0000
```

`user_space_limit` was still Windows' theoretical **0x7fffffff0000 = 128 TB**
while the device's task map tops out at 63 GB. And
`GlobalMemoryStatusEx` computes `ullTotalVirtual` from `HighestUserAddress`
(kernelbase/memory.c:1436), which is `user_space_limit - 1`
(virtual_ios.c:13146). `GetSystemInfo`'s `lpMaximumApplicationAddress` comes
from the same place. **We were advertising 128 TB of user address space on a
63 GB task map**, so an engine sizing a reservation against it asks for
something that cannot exist.

Also confirmed genuinely unusable, not merely unlucky:
`[window] 0x7020000000..0x73ffff0000 OUTSIDE TASK MAP (ceiling 0xfc0000000) --
this is NOT free space`. The jumbo allocator's four 16 GB slots all live at
0x7000000000+, i.e. beyond this device's ceiling, so they read "CLEAR" only
because nothing is mapped there. Same class as ml706's hardcoded FEX band.

### 32.4 ml990 (built, deployed, unlaunched)

`ios_clamp_user_space_limit()` clamps `user_space_limit` to the measured
`host_addr_space_limit`, called both where the ceiling is first measured in
`virtual_init` and again where `virtual_set_large_address_space` re-widens it to
`address_space_limit`. Correct on both targets with no special case -- the VM
measures 63 GB, the A15 phone 512 GB, and nothing above the ceiling is mappable
on either. ml124 already used this same measured ceiling to size the ARM64EC
code bitmap; this extends it to what we report to applications.
`MADEIRA_WIDE_USER_VA=1` restores the old claim.

**What this does and does not claim.** Reporting 128 TB is wrong regardless, so
the fix stands on its own. Whether RDR2's 28 GB derives from that number is a
HYPOTHESIS -- the next run settles it, because the requested size is printed by
`[jumbo#1]`/`[alloc-fail]`. If the size is unchanged it is hardcoded or derived
from something else, and the real options become: satisfy a large sparse
reservation with commit-time backing, or find 28 GB contiguous under a 63 GB
ceiling of which ~56 GB is already mapped (unattributed -- worth a full region
dump, since [va-gaps] currently prints only free gaps).

### 32.5 deploy-vm.sh caught a live one

`was_running=1 now=1` -> `STILL RUNNING -- a launch now would keep using the old
mapped dylib` -> `DEPLOY FAILED`, log not cleared. The §31 guard did exactly its
job on its first real outing; the modal error dialog was keeping the process
alive and `killall` did not reach it.

---

## §33 — rdr60: the 28 GB reserve is 7/8 of the physical memory WE ADVERTISE

Build verified by UUID before reading anything (`LC_UUID=52EC68E4-...` = deployed ml991).

### 33.1 ml990 worked and correctly changed nothing

```
ml990: virtual_init user_space_limit 0x7fffffff0000 -> 0x1000000000
ml991: => ullTotalPhys=34335600640 (32744 MB)  ullTotalVirtual=68719411200 (65535 MB)
[jumbo#1] ... size=0x700000000 (28672 MB) hint=0x0 -> 0x0 st=0xc0000017
```

`ullTotalVirtual` fell from 128 TB to 64 GB, and the requested size was
**unchanged**. That is the predicted result: offline disassembly of the caller's
neighbourhood showed the cached getter keeping struct **+0x8 (ullTotalPhys)**,
not +0x28 (ullTotalVirtual), so the virtual clamp could not have moved it.
ml990 stands on its own merits (128 TB is the wrong answer for every caller) and
had no side effects -- the run reached exactly the same place.

### 33.2 The arithmetic

```
MmNumberOfPhysicalPages = 8382715   PageSize = 4096
=> ullTotalPhys = 34,335,600,640 = 32,744 MB
requested reserve = 0x700000000    = 28,672 MB
32,768 x 7/8                       = 28,672      <-- exact
```

**RDR2 reserves 7/8 of the physical memory we report.** We report 32 GB because
`hw.memsize` on the jailbroken VM returns the **host Mac's** RAM, and
`virtual_get_system_info` passes it through untouched
(virtual_ios.c, the `__APPLE__` branch). The jetsam limit is 4096 MB.

So the wall was never really the game's appetite; it is that we advertise 8x the
memory the process can ever hold. Every engine sizing texture pools, streaming
budgets or quality presets off `GlobalMemoryStatusEx` has been doing so against
a number that would get it killed -- this is a long-standing accuracy gap that
only became fatal here because RDR2 turns it into an address-space reservation.

Predicted effect of reporting the real limit: 4096 x 7/8 = **3,584 MB**
(0xE0000000), against a measured `largest=4299 MB` free hole. It fits, with
little margin.

### 33.3 The run is otherwise stable and repeatable

* `UNHANDLED=0 SEGV=0 redeliv=0` again.
* `D3D12CreateDevice` x2, `device created` x2 -- the device creation of §32.2 is
  reproducible, not a one-off.
* The ml989 markers show the retirement ran **six** times, each a complete
  `U0> D0> D1u D2v D3e D4r D5< U1<`. §29's "silent death in delete_view" is
  conclusively dead; it was always stale code.
* `[holes<64G] free=6594 MB largest=4299 MB` -- steady, matching rdr59.

### 33.4 ml992 (built, deployed, unlaunched)

Clamp the reported physical memory to what the process may actually use. The
limit is **measured**, not assumed: `os_proc_available_memory()` (bytes remaining
before jetsam) + `TASK_VM_INFO.phys_footprint` (bytes already charged), cached on
first use so the advertised total cannot shrink as we allocate.

`os_proc_available_memory` is declared explicitly at the call site rather than
relying on `<os/proc.h>` being reachable -- the first build compiled without any
include and without an implicit-declaration warning, and an implicit declaration
returns `int`, which would silently truncate a byte count. Verified afterwards:
`U _os_proc_available_memory` in virtual.o, and the app links.

**`MADEIRA_TOTAL_PHYS_MB=N` overrides it (0 = leave hw.memsize alone).** This
matters: RDR2's stated minimum is 8 GB of RAM. Reporting the honest ~4 GB may
trade an unsatisfiable reservation for a minimum-spec rejection, and if it does,
the env var finds the highest workable value without a rebuild. Note that no
value above ~4.9 GB can work regardless, because 7/8 of it exceeds the 4299 MB
hole.

**Owed, unchanged:** attribute the ~56 GB of the 63 GB ceiling that is already
mapped. `[va-gaps]` prints only free gaps, which is the half that cannot answer
it. If the reservation has to grow later, that is the number that decides whether
it can.

---

## §34 — rdr66: THE 8960 MB RESERVATION SUCCEEDS. rdr67: the next wall, with two live hypotheses

### 34.1 The request is `max(7/8 x reported_phys, 8960 MB)` -- measured, three points

| `madeira-totalphys.txt` | reported `ullTotalPhys` | 7/8 x phys | requested reserve |
|---|---|---|---|
| (none) | 32,744 MB | 28,651 | 28,672 MB -- scaled |
| 4096 | 4,095 MB | 3,583 | **8,960 MB** -- floored |
| 1024 | 1,023 MB | 895 | **8,960 MB** -- floored |

The floor is 8960 MB = 7/8 of 10,240 MB, i.e. an internal 10 GB minimum-spec.
**No memory-reporting value can lower it**, so ml990/ml992/ml993 cannot solve
this on their own -- though reporting 32 GB on a 4 GB jetsam limit was wrong
regardless and ml992/ml993 stand on that merit.

Two failed models to record, because both fit their data perfectly and both were
wrong: "7/8 x phys" (one point) and "0.6875 x phys + 6144" (two points, two
unknowns -- a fit with zero degrees of freedom). It took the third point.

### 34.2 What actually cleared it

`[va-own]` (ml994, the occupancy half of `ios_va_gap_probe`) showed the usable VA
below the 63 GB ceiling is three holes: ~4.1 GB, ~5.0 GB and ~9.95 GB. Only the
9.95 GB hole can hold either the FEX arena (8 GB) or the guest (8.96 GB), and the
arena took it first via an ANYWHERE fallback, leaving 1754 MB. Its 8 GB showed
`res=0 MB` on every 128 MB sub-block.

Working configuration, all file-driven, all opt-in:

* `madeira-arena-mb.txt` = **4096** (ml995) -- caps the arena size and skips
  oversized ladder steps. The arena then fits the ~5 GB hole. ⚠️ The arena's own
  comment says "4GB is demonstrably too tight -- FEX needs ~3.5GB of spans and
  code for ~74 threads"; 4 GB has held for these runs but it is documented-tight,
  not proven.
* `madeira-jumbo-mb.txt` = **9216** (ml996/ml997) -- reserves the largest hole at
  boot with `anon_mmap_tryfixed` (no-overwrite), placed at the TOP of the hole so
  the PEB band and furniture keep growing from the bottom, and released once to a
  large guest reservation.
* `madeira-totalphys.txt` = 4096.

Result:

```
[fex-arena] ml799 RESERVED 4GB @32-48G base=0x3542e0000
[jumbo-hold] ml996 HELD 0x781e00000 +9216 MB PROT_NONE (no-overwrite)
[jumbo-hold] ml996 releasing the holdback 0x781e00000 +9216 MB for a 8960 MB request
[jumbo-hold] ml997 mapped at the holdback -> 0x781e00000 size=0x230000000 st=0x0
```

**Two ordering lessons paid for with runs:**

1. `ios_jumbo_holdback_init()` must run **after** `ios_reserve_fex_arena()`
   (called from `loader_ios.c:2772`), not at jit-pool-init. rdr65 held 9216 MB
   first and the arena then failed all 9 candidates (`ml774 NO ARENA RESERVED`),
   costing FEX its band and producing **683** failed 64 KB RWX CodeBuffer
   allocations against a baseline of 2.
2. The holdback must be consumed on the branch that RUNS. rdr65's `take()` sat in
   the hinted-retry branch and was never reached -- no "releasing" line anywhere
   in the log -- while the live branch is the `[jumbo] kernel-pick reserve failed
   ... top window is full` early bail. **Third time in this effort that a hook
   went into a path the evidence never showed executing** (ml983 mmap layer,
   ml984 view layer, ml996 jumbo layer). Confirm the branch from a log first.

Also note: the 9.95 GB hole lies **inside CoreAnimation's declared range**
(`ml901` exists to avoid it), so `[layerkit-range] exclusion DROPPED` is expected
with the holdback on. Remote Metal mode creates no local Metal objects
(`ml762`), which is probably why nothing has broken yet -- that is reasoning, not
measurement.

### 34.3 rdr67 -- the new wall, diagnosed but NOT understood

The game now advances into EMP.dll's code decryption and faults writing into the
main image's second `.text` section:

```
[store-noalias] #1 addr=0x1464e8fc6 insn=0xb83f6828 pc=0x136c3937c NO pool/anon alias
                | region 0x1464e8000+0x4000 prot=5 max=7
[av-detail] p0=WRITE p1=0x1464e8fc6
[x86_dst] @RCX: 89fe8ec588fdf253 9431f96a1c8f8d16 ...        (high entropy)
[wr-strip] #1 write=1 wine_want=5 host_prot=5 (not a strip)
ml998: 0x1464e8fc6 vprot=0x25 { COMMITTED READ EXEC } unix_prot=5
ml998:   view 0x140000000+0x7528000 protect=0x180002d { SEC_IMAGE WRITECOPY }
ml998:   neighbours prev=0x2d this=0x25 next=0x2d
```

`0x2d` = COMMITTED|READ|EXEC|**WRITECOPY**. `0x25` = COMMITTED|READ|EXEC --
**neither WRITECOPY nor WRITE**, one page between two neighbours that still have
it. `ml958` granted the whole section W at startup. Both heal paths decline
correctly: `[wr-strip]` because `want=5`, and `virtual_handle_fault`'s COW
because WRITECOPY is already gone.

Event counts are small: **5** `[mach_exc] UNHANDLED`, **1** `bus_handler BUS`,
**0** SEGV, **0** redelivery-terminate, and `exited unexpectedly` = **0** (no
on-screen dialog this run). ⚠️ `grep -c UNHANDLED` returns 245 because
diagnostic lines mention it -- count `[mach_exc] UNHANDLED #` instead.

**Ruled out:** no site clears `VPROT_WRITECOPY` through
`set_page_vprot_bits` -- every `clear` argument in virtual_ios.c is WRITEWATCH,
GUARD or COMMITTED. So `0x25` was written wholesale by a `set_page_vprot` /
`set_vprot`, not by a bit-clear.

**Hypothesis A -- our bookkeeping lost WRITECOPY.** A WRITECOPY+EXEC page has
`get_unix_prot() == PROT_READ|PROT_WRITE|PROT_EXEC` (virtual_ios.c:7331), which
iOS can never grant, so some iOS exec path may be writing a reduced vprot to
make it mappable and dropping the COW obligation instead of discharging it
(WRITECOPY -> WRITE). Losing both bits is wrong under any policy.

**Hypothesis B -- the guest did it deliberately and we mis-deliver the fault.**
Denuvo/Arxan routinely decrypts a page, `VirtualProtect`s it to
PAGE_EXECUTE_READ, and relies on its own handler for later writes. Supporting
evidence, and it is the stronger signal:

```
[mach-deliver] rev=ml372 teb cand1=0x86bfd0000 stack=(0x149670000,0x149e70000] does NOT contain sp=0x5fe2b0000
[mach-deliver] rev=ml378 no TEB owns sp=0x5fe2b0000 (pc=0x136c3937c x18=0x0 registry=0x86bfd0000) -> BEST-EFFORT delivery on guest stack
```

`x18=0` and an SP outside every registered TEB stack, on all three faults. If the
DRM expects to service this write itself, a best-effort delivery is exactly why
it never recovers. Note fault #3's `sp=0x86bfdf670` is inside the TEB *page*, not
its stack -- also unattributed.

No `[exec-req]`/`NtProtectVirtualMemory` line covers `0x1464e8...`, but that
probe is storm-gated, so absence is not evidence (**absence of a probe string is
not absence of the event**).

### 34.4 Questions for offline work

1. Which `set_page_vprot`/`set_vprot` call writes `0x25` to that page? An
   instrumented setter that logs any transition clearing WRITECOPY **without**
   setting WRITE, with a caller address, decides A vs B in one run.
2. If the guest did it: log every guest protect on the main image's `.text2`
   range **ungated**, and check whether the DRM registered a vectored/SEH handler
   that should be receiving this AV.
3. Why does a guest thread have `x18=0` and an SP in no registered TEB stack
   (`0x5fe2b0000`)? That is a defect on its own terms and would break any
   guest-serviced fault, independent of the vprot story.
4. Is the correct policy for an exec+WRITECOPY image page on iOS to keep the
   backing RW-non-exec and let the pool copy hold X (the ml957 policy), plus FEX
   code invalidation when backing bytes change? That invalidation is still the
   open Stage-2 item from §30.3.

### 34.5 Side effects to watch

* `[holes<64G] free=1610 MB largest=721 MB` (was 11,408 / 8,796). VA is nearly
  exhausted after arena + holdback + the guest's 8,960 MB. Anything else large
  will fail, and there is no headroom left to buy.
* **335** failed 64 KB RWX allocations, `status=c0000018`
  (CONFLICTING_ADDRESSES), versus 2 at baseline. Better than ml996's 683 but not
  normal; FEX CodeBuffers failing could limit progress independently of the
  fault above and may mask it.

---

## §35 — rdr68: Astra's classification fix WORKS. The guest's own VEH services the SMC write. New blocker downstream.

Build fingerprinted before reading: `LC_UUID=143446B7-B719-3490-B2E6-505810816532`.
Settings unchanged (`jumbo-mb=9216`, `arena-mb=4096`, `totalphys=4096`).
Log 24,168 lines vs rdr67's 12,970.

### 35.1 Acceptance results against §"One-run acceptance plan"

| # | criterion | result |
|---|---|---|
| 1 | protection fault becomes AV, never a memcpy fault | **PASS.** `[bus-data-av] pc=0x13670167c addr=0x1464e8fc6 esr=9200004f rw=1` then `[exc-disp] raise code=c0000005 p0=1 p1=1464e8fc6` (p0=1 = write). `_platform_memmove` occurrences: **0**. `ios_emulate_unaligned_guest_access`: **0**. |
| 2 | FEX handles, or the guest receives the AV -- record which | **The GUEST receives and services it.** `[veh] calling handler 0x13037C00 ...` / `[veh] handler 0x13037C00 returned ffffffff` = EXCEPTION_CONTINUE_EXECUTION. FEX's `HandleRWXAccessViolation` did not service it; the DRM's own vectored handler did, 120 times. |
| 3 | writable backing, bytes stored, progress | **Indirect PASS.** Immediately after the AV: `[iat-sync] region 0x1464e8000+0x1000: translated 1 pointers`, i.e. the page became writable and was processed. And a **second, different** SMC write followed at `0x1467df915` -- the game would not reach a new address if the first had not completed. Not byte-verified. |
| 4 | no repeated original-PC loop, no `[fault-stuck]` | **PASS.** `fault-stuck`: **0**. |
| 5 | negative control | Alignment path preserved by construction (`DFSC != 0x21` required). Not exercised by a positive test this run. |
| 6 | follow the game thread, not the app heartbeat | Done -- see 35.2; tid 008c dies, footprint never exceeds 1216 MB so this is **not** jetsam. |

**ml999 never fired** (0 occurrences). So `mprotect_exec` was never asked for W+X
and told success with WRITE silently dropped -- Astra's second retry-loop hazard
did not materialise on this path. Useful negative; the weakness in that fast
path is still real and still unfixed.

### 35.2 The new blocker

```
[redeliv] 2000 identical redeliveries pc=0x1374b818c addr=0x6044c8324060
          -- unrecoverable host fault misdelivered to guest rev=ml461
[redeliv] terminating process rev=ml465
```

Distinct exception addresses in the storm: `0x1374B818C` x2001, `0x0` x2000,
everything else single-digit. So one fault at a JIT-pool PC repeats until the
ml465 guard terminates.

Two concrete oddities:

* `addr=0x6044c8324060` is ~105 TB -- far above the 63 GB task ceiling, so it
  cannot be a real fault address.
* `D 8C Reconstructing context` / `pc: 1374B818C rip: 3020396520313920`. That
  "rip" is **ASCII**: little-endian bytes `20 39 31 20 65 39 20 30` = `" 91 e9 0"`.
  `x9=0x7d22cedd8` holds more of the same (`" 0 9e 19 "`, `"3 0c aa "`,
  `"8b 33 43"`), and the VEH ran with `Rsp=0x7d22ceb70` -- so x9 is RSP+0x268,
  i.e. **the guest stack itself contains hex-dump text** and the context
  reconstruction lifted a "return address" out of it.

Read: the DRM appears to be formatting a hex dump on its own stack (plausibly an
anti-tamper report), and FEX's context reconstruction then produces a garbage RIP
from that text, which we redeliver 2000 times.

Open questions for offline work:

1. Which reconstruction path yields `rip` from stack text? `prepare_exception_arm64ec`
   logs `[guest-rip] 0x13670167c -> Wine has NO VIEW of this address (AllocationBase=0,
   Type=0) -- foreign/JIT-pool mapping`, so the pool PC has no Wine view and the
   protection verdict is meaningless -- does the reconstruction then fall back to
   scanning the stack?
2. Is `addr=0x6044c8324060` a truncation/sign-extension artifact or a genuine
   guest value? It repeats identically 2000 times, so it is deterministic.
3. Is the hex-dump-on-stack the DRM reporting a tamper detection (i.e. a
   *consequence* of something we did earlier), or ordinary diagnostics? That
   distinction decides whether this is a new bug or a symptom.
4. `[veh] handler returned ffffffff` 120 times: does each one correspond to a
   serviced SMC write, and do the written bytes match what the guest intended?
   Byte-level verification is still owed for criterion 3.

### 35.3 What did NOT advance

No new renderer milestones: `device created` still 2, `D3D12CreateDevice` still
2, and `CreateSwapChain` / `CreateCommandQueue` / `CreateGraphicsPipeline` /
`CreateCommittedResource` / DXIL / rpf all remain **0**. The doubled log is the
exception storm (4,117 `prepare_exception_arm` + 120 VEH calls after the second
`[bus-data-av]`), not new game progress.

So rdr68 removes a self-inflicted handler bug and proves the DRM's own recovery
path functions through our exception machinery -- a real structural gain -- but
the game still does not reach renderer initialisation. Astra's framing holds: the
defensible next milestone is surviving this storm and reaching sustained renderer
init, not a menu.

---

## §36 — rdr70/71/72: four hypotheses eliminated by measurement. Handing over.

Astra's CAS fix (§ rdr68 review) is fully validated and I am not re-litigating it:
`[mach-cas]` 5x, `store-undecoded` 0, `Handled self-modifying code` 5 (was 2),
**0 VEH calls in the whole run**, no ASCII rip, no wild bitmap addresses, no
redelivery termination. Plus two pre-existing warnings fixed in the diagnostics
I rely on (`state.__x[29]` read one past `__x[0..28]` -- it landed on `__fp` and
printed the right value, but was UB; and a `%p`/`uintptr_t` mismatch).
signal_arm64.o now compiles warning-free.

The user's position, which I accept: these exact files run under CrossOver on
macOS. CrossOver is Wine. So any difference is OUR divergence from Windows/Wine
behaviour, and closing it is general accuracy work. Investigated on that basis.

### 36.1 Eliminated, with the measurement

| hypothesis | result |
|---|---|
| A resolved export is missing from our DLLs | **No.** `MessageBoxTimeoutA` (user32), `SHGetFolderPathA` (shell32/shfolder), `PathFileExistsW` (kernelbase/shlwapi), `AddVectoredExceptionHandler` (kernel32/kernelbase) are all present in the tree. |
| The sub-floor emulator corrupts data (byte stores truncating strings) | **No.** ml1000: **987,137** serviced accesses, **0 MISSERVICE** (emulator-vs-classify disagreement), 0 refused. The single NUL byte-store (`strb w10`, w10=0) landed in already-zero memory. |
| The 28 failing `LoadLibrary` names are our corruption | **No.** The set is **byte-identical across rdr69, rdr70, rdr72**. A million emulated accesses producing corruption would vary; a fixed table does not. `a`/`k` are the loader's own strings. |
| The SMC decryption writes are dropped or land wrong | **No.** ml1001 (decode-free before/after snapshot): 4/4 verified stores landed, exactly 4 bytes at the right offset, ciphertext -> **valid x86-64**: `55 48 8d 2d` (`push rbp; lea rbp,[rip-..]`), `48 63 4c 24 50` (`movsxd rcx,[rsp+0x50]`), `48 8d 4d d0` (`lea rcx,[rbp-0x30]`). **This closes the criterion Astra recorded as only indirectly satisfied.** |
| The sub-floor EMP.dll drives the decryption | **No.** ml1002 (below). |

### 36.2 The structural divergence, and why it is NOT the SMC driver

```
ml936: image base 0x13000000 below the 0x100000000 floor, unmappable here;
       relocating anyway (dynamic_base=0 relocs_stripped=1)
ml949: sub-floor image RELOCS_STRIPPED, mapped at 0x452850000 (delta 0x43f850000):
       ImageBase rewritten, directory NOT applied
[subfloor] ml938 window #2: guest [0x13000000,0x1320f000) -> real 0x452850000 (2108 KB)
```

`0x20f000` = 2,158,592 bytes; **EMP.dll on disk is 2,158,392 bytes**. So EMP.dll
demands base `0x13000000`, has RELOCS_STRIPPED, and iOS's mandatory 4 GB
`__PAGEZERO` makes that address permanently unmappable. CrossOver maps it there
and runs it natively with zero emulation. We map it 17 GB higher, deliberately do
**not** apply its relocation directory, and service ~1M accesses by fault.

That remains the largest known behavioural difference from Windows in this
workload, and the un-applied relocations mean its absolute addresses still read
`0x13xxxxxx` -- correct only for accesses that *dereference* through the window.
An address consumed without dereferencing (a comparison, a self-checksum, a
pointer stored and re-read from another module) sees a value Windows never
produces.

**But it is not what performs the decryption.** ml1002 walks FEX's callret stack
at each SMC write and tags each return address:

```
ml1002: [smc-write] guest callers, x28=0x33e001140 callret=0x33f41c360:
  [0] retRIP=0x142e13171  main image
  [1] retRIP=0x142e8332b  main image     (period-2 repeat)
...
  [0] retRIP=0x142e87188  main image
  [1] retRIP=0x142e7bcec  main image
  [2] retRIP=0x142e8022d  main image     (period-3 repeat)
```

Every non-empty chain is in the **main image at its correct base 0x140000000**,
clustered around `0x142e1xxxx`/`0x142e8xxxx` -- note RDR2.exe's
`TransferAddress=0x142e133d0`, so this is entry-area unpacker code. **No caller
is inside the sub-floor window.** 2 of 5 chains read all-zero (empty callret
stack at that moment).

⚠️ Treat the repetition cautiously: exact period-2 and period-3 cycles are
consistent with a real tight loop, but also with my 16-byte-stride walk
re-reading the same entries. x28 is taken from the SAVED context (a signal
handler's live x28 is the handler's own), which is correct, but the stride
assumption is inherited from the fault path and not independently verified here.

### 36.3 Where the run ends

`ERR_NO_LAUNCHER` in a MessageBox (the standard Wine button templates --
OK/Cancel/&Ignore/&Try Again/&Continue/Help -- appear immediately before it).
The 28 names the loader resolves are a coherent API set for exactly that code
path: `GetModuleFileNameW`, `PathFileExistsW`, `SHGetFolderPathA`,
`CreateFileA/W`, `ReadFile`, `GetFileSize`, `CreateProcessA`, `MessageBoxA`,
`MessageBoxTimeoutA`, `ExitProcess`, `_itoa`, `memcpy`.

Environment facts, offered as facts rather than conclusions: the prefix has no
`Program Files/Rockstar Games`; `socialclub.dll`/`orig_socialclub.dll`/
`steam_api64.dll` exist on disk but are **never loaded and never even
requested** (0 mentions in 13,923 lines); the game creates
`AppData\Roaming\EMPRESS` and `AppData\Local\Rockstar Games\...\CrashLogs`
(FILE_CREATE -> OBJECT_NAME_COLLISION, benign). Process chain is
`RDR2.exe -> Launcher.exe -> RDR2.exe`, the last spawned with a bare
`cmdline="RDR2.exe"`.

### 36.4 Questions I could not answer cheaply

1. Which API call immediately precedes the MessageBox, and what does it return?
   The `[dll-missing]` emitter is in `wine/dlls/ntdll/loader.c` -- the **PE-side**
   ntdll, whose build script (`build/wine-pe/build-ntdll.sh`) still has the
   broken padding assertion recorded in §29.3 and was not rebuilt. A caller dump
   or return-value trace there is the most direct evidence available and needs
   that chain fixed first.
2. Does any consumer of a `0x13xxxxxx` address use it WITHOUT dereferencing it?
   That is the failure mode the ml938 window cannot cover, and it is testable by
   logging every value in that range that crosses an API boundary.
3. Is `ml949`'s "directory NOT applied" still the right call? It was reasoned
   about ("entries are delta-injection immediates, not pointers") rather than
   measured against what the module actually needs.
4. Still open from earlier sections: `mprotect_exec` reporting success when a
   requested WRITE was dropped (ml999, never fired); the emulator chain trying
   LOAD before consulting `is_write` (structurally unsound, 0 occurrences);
   and 987,137 sub-floor faults as a standing performance wall.

---

## §37 — rdr73/74: ERR_NO_LAUNCHER CLEARED. ⛔ §37.2/37.4 RETRACTED — see §38.

> **RETRACTION (2026-09-18).** The "check_call points into the wrong module"
> conclusion below is WRONG. It compared rdr73's check_call values against
> rdr74's ntdll pool bases. Recomputed per run, all six instances resolve to
> **ntdll + 0x56200** -- the correct function:
>
> | run | ntdll pool | check_call | delta |
> |---|---|---|---|
> | rdr73 | 0x123448000 / 0x126fc4000 / 0x12ef44000 | 0x12349e200 / 0x12701a200 / 0x12ef9a200 | 0x56200 |
> | rdr74 | 0x123e94000 / 0x127a10000 / 0x12f990000 | 0x123eea200 / 0x127a66200 / 0x12f9e6200 | 0x56200 |
>
> The "identical 0x9f5e00 delta" I called the sharpest clue was the signature of
> the cross-run comparison itself -- the common pool slide between runs. My own
> note "constant delta = STALE" exists for exactly this and I inverted its
> meaning, treating it as evidence of a systematic bug in the code rather than
> in the comparison. **Compare at matched checkpoints, from ONE run.**
>
> Also: `cc[0] is NOT a B` is an OBSOLETE diagnostic, for a reason already in
> my own notes -- ml975 prepended two instructions to `arm64x_check_call`
> (`lsr x16,x11,#39` / `cbnz x16,exit`), so the patchable
> `ldr x16,[x18,#0x60]` moved to **+8**, and rdr74 logs exactly that prefix with
> +8 replaced by `B` (0x1403b527). The x18 patcher was working correctly all
> along, on the right module (`text=ntdll_pool+0x10000 patched=746`).
> The expectation in the message needs updating, not the patcher.



### 37.1 Astra's parent-memory fix works

`ERR_NO_LAUNCHER` count: **0** (was 22). Log 25,688 lines vs 13,923. Verified her
diagnosis independently before applying:

* Lowercase Jenkins one-at-a-time: `hash("err_no_launcher") == 0xf4c2a92f`,
  matching the identifier she disassembled at `0x1401258e2`. Exact.
* `read_process_memory` (mach_ios.c) does `if (!process_port) { set_error(
  STATUS_ACCESS_DENIED ); return 0; }` before attempting any read, and
  `send_server_task_port()` is compiled out under `WINE_IOS`, so `trace_data` is
  always 0 and **every** cross-process read failed.
* Launcher is pid 0x80 (tid 0084), RDR2 pid 0x88 (tid 008c) -- the parent exists.

This was a general Wine defect: any application introspecting another process got
ACCESS_DENIED where Windows succeeds. Applied reads-only, leaving
`get_process_port()` and `write_process_memory()` untouched -- the wider version
of this change is recorded in `get_process_port`'s own comment as having
regressed Steam. Added **ml1003**: `madeira-no-local-read.txt` = 1 disables it
without a rebuild, since env vars are not reachable on the device.

### 37.2 The new blocker: check_call points into the WRONG MODULE

Death is `pc=0x0`, 2000 redeliveries, footprint only 1301 MB (not jetsam). The
SEGV handler's own diagnostics:

```
call_site@(lr-16): mov x1,x28 / mov x3,#0 / ldr x4,<literal> / blr x4
callee prologue: 00000001 00000000 50960000 00000004        <- DATA, not code
EcBitMap: x11_page=0x149570 bit=0 (NOT EC: dispatch path taken)
arm64x_check_call@0x12ef9a200: cc[0] is NOT a B (opcode top6=52) — patcher missed it
```

**The "patcher missed it" conclusion is wrong, and so was my first reading of
it.** Measured with ml1005 (see 37.3):

| tid | metadata check_call | that process's ntdll pool copy | delta |
|---|---|---|---|
| 007c | 0x12349e200 | 0x123e94000 | 0x9f5e00 |
| 0084 | 0x12701a200 | 0x127a10000 | 0x9f5e00 |
| 008c | 0x12ef9a200 | 0x12f990000 | 0x9f5e00 |

An **identical** 0x9f5e00 delta in all three. And the address resolves:

```
0x12ef9a200 is inside pool image pe=0x140000000 pool=0x128467000+0x7528000
            at offset 0x6b33200   (the MAIN IMAGE's copy, in its second .text)
```

So the ARM64EC metadata's `check_call` points ~10.4 MB away from ntdll, into
RDR2.exe's own pool copy. `cc[0]` is "not a B" because it is not
`arm64x_check_call` at all -- it is game bytes. The x18 patcher had in fact done
its job correctly on the right module: `text=0x12f9a0000 +0x770a5 patched=746`,
i.e. ntdll pool base + 0x10000, 746 instructions.

Chain: bad `check_call` in the EC metadata -> boundary call dispatches into data
-> `pc=0` -> ml461 redelivery storm -> ml465 terminate.

### 37.3 ml1005: the summary that had never once reached a log

`ios_jit_patch_x18`'s result line was `ERR(...)` on the `virtual` channel.
**rdr73 contains ZERO `err:virtual:` lines in 25,688** -- that channel is fully
muted, so "the x18 patcher did not run" was unprovable in either direction, and
I briefly concluded it from the absence. The file itself already knew (`"dprintf,
not ERR (err-virtual muted)"` sits three lines below the offending call) and the
most load-bearing line in the function was still an ERR.

ml1005 makes it a `dprintf` and prints the **RANGE**, not just counts -- counts
alone cannot answer "did any run cover 0x12ef9a200". Measured: 159 runs, 31,810
instructions patched, bases spanning 0x119..0x133, i.e. it runs for every
pseudo-process, and **0 runs cover any of the three check_call addresses** --
consistent with those addresses simply not being in ntdll.

### 37.4 What I could NOT establish

**How the wrong value is produced.** `arm64x_check_call` is a PE-side ntdll
symbol; its address is taken in PE-side code
(`signal_arm64ec.c:600 RtlGetCurrentPeb()->WerRegistrationData = arm64x_check_call;`
and the per-module metadata write), which I cannot instrument without the PE
ntdll chain.

The code comment at `signal_arm64ec.c:617` describes exactly this class of
failure as a known, worked-around defect:

> re-running arm64ec_update_hybrid_metadata on ntdll's .data triggers the iOS
> NtProtect IAT-sync path (in our virtual_ios.c) which over-aggressively
> rewrites one pointer in ntdll's .data to a JIT-pool address, breaking ntdll's
> own internals immediately after.

But this log does **not** support that as the mechanism here: `ml454`
`exact-owner miss` count is **0**, and there is no `[iat-sync]` line for ntdll's
pool region at all. So the comment names the right shape and the wrong cause,
or the rewrite happens somewhere that does not log.

The constant 0x9f5e00 delta is the sharpest clue: a random mis-translation would
not reproduce the same offset in three processes. It is consistent with
`&arm64x_check_call` being formed against a wrong base, landing at a fixed
relative position that happens to fall in the main image's copy.

**Questions for offline work:**

1. Disassemble how `arm64x_check_call`'s address is formed in the built PE ntdll
   (adrp/add pair, IAT slot, or relocated .data word?) and which of those the
   0x9f5e00 delta is consistent with.
2. Is `check_call` in the EC metadata supposed to be a pool address at all, or
   should it be the PE-side VA with translation happening at use time?
3. Does `arm64ec_update_hybrid_metadata` read a value that was already wrong, or
   compute a wrong one? The per-module log line reports the same value for every
   module in a process, so it is a single source.

### 37.5 Also fixed: build-ntdll.sh (owed since §29)

```python
assert len(d) == soi                    # never passed: stripping SHRINKS the file
open(p,'ab').write(b'\0' * 0x50000)     # and a fixed append is the wrong total anyway
```

Stripping yields 0x120000 against `SizeOfImage` 0x140000, so this script has
**never completed** -- the file was padded by hand. It now pads **up to** the
documented target (`SizeOfImage + 0x50000`), verifies the result, and refuses
loudly if the stripped file ever exceeds it. Validated against the deployed
artifact: 1,638,400 bytes = 0x140000 + 0x50000, exactly what the fixed formula
computes. Chain is now usable for the PE-side work item 1 needs.

---

## §38 — rdr74's ACTUAL first fault: the virtual display cannot be opened

Astra's rdr74 review. Verified in the log before implementing:

```
16988 [vmode] synthesized EnumDisplayDevices adapter idx=0
16990 err:d3d:wined3d_adapter_create_output Failed to initialise output L"\\.\DISPLAY1", hr 0x80070057
16991 err:d3d9:d3d9_ios_probe Direct3DCreate9Ex called -> d3d9_init FAILED
17003 SEGV #1: pc=0x1356881d0 addr=0x0
17010 [guest-exact] rip=0x1426b8b77 x86=48 8b 01 ff 50 10 ...
```

`48 8b 01 ff 50 10` = `mov rax,[rcx]; call [rax+0x10]` with rcx=NULL: RDR2 calls
**Release on the NULL output object** it never checked after
`Direct3DCreate9Ex` failed. Per Astra's disassembly the success and failure
paths converge at 0x1426b8b70, so the failure branch falls into the same
cleanup. That unguarded Release is the first fatal fault; every dispatch storm,
ASCII rip and `pc=0` redelivery after it is recovery noise.

**Root cause, in our own file.** `sysparams_ios.c`'s `NtUserEnumDisplayDevices`
synthesizes a primary adapter `\\.\DISPLAY1` under `ios_virtual_monitor_active()`
"because the sources list is empty, so the normal lookup finds nothing" -- and
`d3dkmt_open_adapter_from_gdi_display_name` in the same file had **no matching
branch**. It calls `find_source()`, which cannot find a source that was never
registered, and returns STATUS_UNSUCCESSFUL -> `wined3d_output_init` E_INVALIDARG
-> D3DERR_NOTAVAILABLE. We advertised a display we could not open.

### ml1006 (built, deployed, unlaunched)

A `#ifdef WINE_IOS` branch in `d3dkmt_open_adapter_from_gdi_display_name`:

* only under `ios_virtual_monitor_active()`, and only for the exact name the
  enumeration branch advertises, matched the same way (length + `wcsnicmp`).
  Unknown displays are refused and logged.
* **one stable identity** for the process lifetime, initialised once under
  `display_lock`. A fresh `NtAllocateLocallyUniqueId` per open would hand callers
  a different adapter across close/reopen and break identity comparisons.
* adopts a **registered GPU's** luid when `gpus` is non-empty; only allocates
  when the list is genuinely empty, and then logs it as SOFTWARE so it is not
  mistaken for the remote GPU's identity.
* a real managed handle from `NtGdiDdDDIOpenAdapterFromLuid`, **failure
  propagated**, no invented handle.
* `VidPnSourceId = 1`, matching the real path's `source->id + 1` for source 0.

**Not claimed:** that D3D9 rendering works. This block gathers adapter
information, and a `D3D12CreateDevice` call follows it in the same function.
WineD3D may still fail for lack of a GL/Vulkan backend -- a separate gate, and
`renderer=no3d` does NOT bypass it because no3d's primary-LUID helper uses this
same KMT display-open API.

**Owed:** Astra compiled `/private/tmp/rdr74-adapter-canary.exe` (enumeration,
KMT open by name, three close/reopen cycles with stable LUID/source id, open by
LUID, unknown-display rejection, `Direct3DCreate9Ex`, identification, cleanup).
Run it under the same prefix and virtual desktop BEFORE spending another full
game launch. I have not run it.

---

## §39 — rdr76: ml1006 + no3d clear the D3D9 gate. New blocker: EC entry thunk called with a trap address in x9.

### 39.1 Results

```
Direct3DCreate9Ex called -> d3d9_init OK          (was FAILED)
```

No `wined3d_caps_gl_ctx_create` / `adapter_gl_init` error at all. ml1006 made
the advertised display openable (`Failed to initialise output L"\\\\.\\DISPLAY1"`:
1 -> **0**), and a per-app `renderer=no3d` supplied the software adapter-query
object. Both were needed: no3d's own primary-LUID helper uses the same KMT
display-open call ml1006 fixed.

Config (scoped, not global -- do not regress other D3D9 titles):

```
[Software\\Wine\\AppDefaults\\RDR2.exe\\Direct3D]
"renderer"="no3d"
```

`user.reg` backed up to `user.reg.pre-no3d`.

| | rdr75 | rdr76 |
|---|---|---|
| D3D12CreateDevice / device created | 2 / 2 | **5 / 5** |
| SEGV | 2 | **0** |
| redelivery termination | 1 | **0** |
| runtime | ~30 s | **~5 min, still alive** |

3 of the 5 device creations pass a **real adapter pointer** (`adapter=0x14ADD2240`),
not NULL. The game then queries format support across ~15 DXGI formats and our
runtime answers honestly (`texture format 5/6/7/8 has no Metal mapping` -- the
96-bit R32G32B32 family, which Metal genuinely lacks). It also created a
`"WineD3D fake window"`. 27 threads, peak 1437 MB.

### 39.2 The new blocker, traced offline in the deployed dxgi.dll

```
[task-exc] BREAKPOINT #1 pc=0x11c9a9090 insn=0xd4200020 imm=0x1
[rip-leak] guest RIP 0x11c9a9090 IS POOL addr = PE 0xa03852090 (base 0xa03850000 rva 0x2090)
  HOST x9=0x11c9a9090  x30=0x11ca602f4  x23=0xb7244  x16=0xd63f0200
  GUEST R12=0x887a0002  RSP=0xb7244
-> code=c000001d (STATUS_ILLEGAL_INSTRUCTION), NtRaiseException Unhandled
```

Disassembling the bytes at `x30` (dxgi rva 0xb92f4) from the on-device DLL:

```
-32  adba9fe6  stp q6,q7,[sp,#-0xb0]!     <-- ARM64EC ENTRY THUNK prologue
-28  ad0127e8  stp q8,q9,[sp,#0x20]
...
 -8  910283fd  add x29,sp,#0xa0
 -4  d63f0120  blr x9                     <-- the call that trapped
```

That is the standard ARM64EC entry thunk (same `stp q6,q7,[sp,#-0xb0]!` sequence
as `Module.S:289-300`, identified back in §28). Its job is to call the real
ARM64EC function whose address is in **x9**. Here **x9 = dxgi rva 0x2090**, and
the word at that RVA is `0xd4200020` = `brk #1` -- padding/guard, not a function.

So **the EC dispatch handed the entry thunk a trap address**. The trap is
incidental; the bad target is the defect.

### 39.3 Three readings of mine that the data does NOT support

Recorded so they are not repeated:

1. **"dxgi has an unimplemented trap stub we must implement."** No. rva 0x2090 is
   not an export (nearest above is `CreateDXGIFactory` at 0xbf010) and is not a
   function entry. It is a target that should never have been called.
2. **"`0x000b7245` before the brk is an entry-thunk RVA."** No. rva 0xb7244 holds
   ordinary mid-function code (`str w11,[x25,#0x14]` / `sub w10,w10,w12` /
   `subs w27,w10,w21` / `b.le`). Those words are data, not thunk pointers.
3. **"`DXGI_ERROR_NOT_FOUND` in R12 means DXGI found no adapters, so we must
   register a virtual GPU."** Not established. `DXGI_ERROR_NOT_FOUND` is the
   NORMAL loop terminator for `EnumAdapters`/`EnumOutputs`, and wined3d only ever
   builds `adapters[0]` (directx.c:3508) via `EnumDisplayDevicesW` --
   which ml1006 already unblocked, hence the real adapter pointer above.

**Consequence:** registering a virtual GPU is still worth doing (it removes the
real divergence between `EnumDisplayDevices`, D3DKMT and DXGI, and would let
ml1006 adopt a registered LUID instead of the software one it logs today) but it
is **not** the fix for this trap and cannot be validated by the next run, since
the trap comes first.

### 39.4 Questions for offline work

1. Who sets x9 before that entry thunk, and why is it dxgi+0x2090? The caller
   sequence two functions earlier is a COM vtable call
   (`ldr x8,[x8,#0x38]; ldr x16,[x8]; blr x16`), so a vtable slot is a candidate
   source of the bad pointer.
2. `x16=0xd63f0200` is the *encoding* of `blr x16` sitting in a register used as
   a branch target. The same value appeared in §28's rdr53 fault. Is that a
   recurring signature of one dispatch path, or coincidence?
3. `GUEST RSP=0xb7244` -- an RVA in a stack-pointer register. Is the guest
   context being reconstructed from the wrong save area?
4. Does the EcCodeBitMap correctly mark dxgi's pool pages? rdr74's SEGV printed
   `EcBitMap: x11_page=... bit=0 (NOT EC: dispatch path taken)`, and a wrong bit
   would route an EC call down the x64 path.

⚠️ I have been wrong twice on this machinery today (the retracted §37 check_call
conclusion, from a cross-run comparison). Astra's disassembly-led analysis has
resolved every EC/thunk question so far; this one should go the same route rather
than through another in-session theory.

---

## §40 — rdr78: 🏆 THE GAME WINDOW OPENS. Swapchain created, 240 GPU flush cycles, black output.

Astra's identification of `dxgi+0x2090` was exact. `llvm-nm` on the deployed DLL:

```
180002090 T MTLDXGIAdatper::RegisterVideoMemoryBudgetChangeNotificationEvent
```

RVA 0x2090 = the trapping address, to the byte. A real COM method whose whole
body was `assert(0 && "TODO")` with **no return statement**, compiling to one
`brk #1`. My §39 reading ("padding, not a function entry") was wrong -- COM
methods are not exported, which is why an export scan missed it.

### 40.1 What ml1007 fixed, and the mistake inside it

Both `Register/UnregisterVideoMemoryBudgetChangeNotification*` implemented, and
the compiler immediately exposed a **second identical landmine one vtable slot
away** -- `Register/UnregisterHardwareContentProtectionTeardownStatus*`, same
`assert(0 && "TODO")`, same missing return, same `brk #1` ("non-void function
does not return a value" at line 227). Fixed in the same change, but differently:
content protection is genuinely UNAVAILABLE here, so it returns
DXGI_ERROR_UNSUPPORTED rather than promising a teardown notification.

**⛔ My budget implementation returning S_OK was wrong, and the A/B proves it.**
Same build, only `madeira-dxgi-budget.txt` flipped:

| value | behaviour | result |
|---|---|---|
| (absent) -> S_OK, real cookie, event never signalled | rdr77 | **HUNG** on an auto-reset Event, infinite wait, 8 min, no window |
| `0` -> DXGI_ERROR_UNSUPPORTED | rdr78 | **swapchain + game window + 240 GPU flushes** |

RDR2 really does wait on that event. "Register successfully and never signal" is
not an acceptable reading of a static budget -- Astra's warning against fake
success was correct. **The default is now the honest failure**; `=1` re-enables
real registration for when Budget becomes dynamic and can actually be signalled.

(Also: `arm64ec-windows/dxgi.dll` was NOT in `scripts/deploy-vm.sh`'s file list,
so DXMT changes had no route to the device. Added.)

### 40.2 The milestone

```
[madeira-d3d12] dxgi asked the queue for its Metal device; swapchain bridge engaged
[madeira-d3d12] swapchain: 960x540, 3 buffers, format 87, hwnd 0000000000080054
```

* **First swapchain ever**: 960x540, triple-buffered, DXGI_FORMAT_B8G8R8A8_UNORM,
  bound to a real HWND -- the on-screen game window.
* **A device SURVIVES**: 6 created vs 5 destroyed (every prior run destroyed all
  of them -- they were capability probes).
* **Real GPU traffic**: 240+ `[wmt-remote] flush` cycles, 34 buffers,
  ~178 MB uploaded through the remote Metal transport.
* **508 root signatures parsed** (version 1.1), descriptor/query heaps created.
* Peak footprint 2135 MB (from 1380). ~20,000 log lines between swapchain
  creation and death.
* The on-screen `"Minimum Recommended Hardware Check Failure?"` popup is OURS:
  `madeira-totalphys.txt=4096` against RDR2's 8 GB minimum. A warning, not a
  gate -- the user clicked OK and the game proceeded.

### 40.3 Why the screen is black -- the next work item

```
187  [madeira-d3d12] CS conversion failed: compile/link failed
189  [madeira-d3d12] converter service: could not save bytecode to .../wine/Document
  7  [madeira-d3d12] CreateRootSignature: more descriptor ranges than this build handles
  2  [madeira-d3d12] unimplemented: ID3D12Device10::CreatePipelineLibrary / ClearState
 23  [madeira-d3d12] GraphicsCommandList QueryInterface refused {553103fb-...}
```

**187 compute-shader conversions fail to compile/link.** Nothing draws, so the
presents are black. That is in our DXIL->Metal converter, on RDR2's real shader
set -- the first time it has been exercised at this scale (previous D3D12 work
was UE5 demos). The truncated `.../wine/Document` path in the cache-save message
is worth checking on its own: it may be a genuine path bug rather than log
truncation.

### 40.4 The crash

Illegal instruction, `c000001d`, tid 00a4, `pc=0x2c1f7d9a4` (11.03 GB) --
**not** the JIT pool (0x119e4c000-0x139e4c000), **not** the FEX arena
(0x3502e0000, 4 GB @16-32G), **not** the guest's 8960 MB reserve
(0x795e00000). So it is executing in guest-allocated memory. `armPc == ecRip ==
addr` and `insim=0`, i.e. native/EC rather than translated x86. All GUEST
registers read zero.

After it, `abort_process status=0xc000001d` tears down the game pseudo-process,
and then 12 `brk #0xb001` traps fire in `libsystem_platform.dylib` on threads
with no guest state -- teardown fallout, not the cause.

⚠️ Do not read the 11 `[srv-stuck]` threads as the hang: tids 0030-0064 are
idle shell workers and an infinite wait on an unsignalled Event is normal for
them (recorded previously in this handoff, and I nearly misattributed it again).
My ml1007 registration log printed the cookie but **not the HANDLE**, so the
rdr77 hang could not be correlated directly -- the config A/B is what proved it.

### 40.5 Suggested order

1. The **DXIL->Metal compute-shader conversion failures** (187) -- this is what
   makes the screen black, and it is our converter on a real AAA shader set.
2. The converter's bytecode-cache path (`.../wine/Document`).
3. `CreateRootSignature` descriptor-range limit (7 hits).
4. `CreatePipelineLibrary` / `ClearState` -- both legitimately stubbable, but
   they should return defined HRESULTs, never `assert(0)`. **Audit dxmt for any
   remaining `assert(0 && "TODO")` in a non-void method**: that pattern
   compiles to `brk #1` and cost two runs already.
5. The `0x2c1f7d9a4` illegal instruction.

---

## §41 — The black screen is a container-format gap: RDR2 feeds D3D12 **DXBC SM 5.1**, our D3D12 path only accepts **DXIL**

**Status: root cause found and fully measured offline. No device run was spent on this section.**

### 41.1 What the rdr78 log actually said

189 shader conversions failed, **zero succeeded**: 187 CS, 1 PS, 1 VS, every one of them
`converter code 14`. Blob sizes 116–1372 bytes. Nothing draws, so every present is black.

### 41.2 The blob is DXBC/SHEX, not DXIL — byte-verified

The failure logger dumps any container <= 1024 bytes, so 24 of the 189 were recovered in full
from the log. All 24 parse cleanly and self-consistently. The first one:

```
0x00 "DXBC"   0x04 checksum   0x14 version=1   0x18 totalSize=0x1a4=420 (matches the logged size)
0x1c chunks=3   0x20 offsets 0x2c / 0x3c / 0x4c
   ISGN size=8      OSGN size=8      SHEX size=0x150=336      -> 0x4c+8+336 = 420  OK
0x54 version token 0x00050051 -> programType=5 (compute), major=5, minor=1   => cs_5_1
0x58 length token  0x54 = 84 dwords = 336 bytes                             => matches SHEX size
```

**There is no `DXIL` chunk in any of the 24.** Every container is exactly `('ISGN','OSGN','SHEX')`.
`SHEX` is the SM5.x bytecode chunk; a DXIL shader would carry a `DXIL` chunk instead.

WARNING for anyone re-deriving this: an earlier pass of mine printed `totalSize=932` and
`SHEX size=848` for this same blob. Both were wrong -- a bad offset in the throwaway parser.
The correct figures are 420 and 336, and they reconcile with the container arithmetic. Trust the
arithmetic check (`chunkOffset + 8 + chunkSize == totalSize`), not a one-off script.

### 41.3 Code 14 = `IRErrorCodeUnrecognizedDXILHeader` — read out of the dylib, not recalled

I did not trust my memory of Apple's `IRErrorCode` enum (and I was right not to: the enum in the
shipped dylib contains members -- `NullHullShaderInputOutputMismatch`,
`InvalidRaytracingUserAttributeSize`, `IncorrectHitgroupType` -- that are not in the ordering I
would have written down). Disassembling
`app/Madeira/d3d12/libmetalirconverter.dylib`, in
`DXILContainer::ValidateProgramHeader(const char*, size_t, std::unique_ptr<IRError>*)`:

```
8fc060:  mov  w8, #0xe                 <-- the error code, 14
8fc090:  add  x8, x8, #0xe17           ; "Unrecognized DXIL program header"
8fc108:  add  x3, x3, #0xa13           ; "UnrecognizedDXILHeader : %s"
```

and the validator's own scan requires a chunk whose fourcc is `DXIL` (`mov w9, #0x5844`, the `DX`
half of the fourcc) with length >= 0x20. Our containers hold `ISGN`/`OSGN`/`SHEX`, so it never
matches and every shader is refused. `DXILContainer::BytecodeType()` returns 2 for DXIL bytecode;
there is no branch for `SHEX`.

Note the failure is NOT in `IRObjectCreateFromDXIL` -- that call *succeeds* (the shim's
`if (!input) status = MADEIRA_IR_BAD_DXIL` does not fire). The container parses; it is
`IRCompilerAllocCompileAndLink` that finds nothing it can compile.

### 41.4 Apple's converter has no DXBC input path at all

`dyld_info -exports` on the dylib lists exactly one input entry point: `_IRObjectCreateFromDXIL`.
No `_IRObjectCreateFromDXBC`, no SM5 path, and `strings` finds no `DXBC`/`SHEX`/`Shader Model 5`
handling. **This is not a flag we forgot to set. Apple's Metal Shader Converter is DXIL-only.**

### 41.5 Why this is an accuracy gap and not an RDR2 quirk

SM 5.1 is a **D3D12** shader model. It was introduced *for* D3D12, is emitted by `fxc /T cs_5_1`,
and uses the DXBC container with register spaces and 3-index resource declarations. A conforming
D3D12 runtime accepts DXBC for SM <= 5.1 and DXIL for SM >= 6.0. Ours accepts only the second half.
That is exactly why these files run in CrossOver (D3DMetal compiles DXBC) and not here.

This is also the same wall the UE5 work hit and recorded as "D3D12 runtime built through
swapchain+pipelines+compute, blocked only by SM5-only shaders". It was never fixed, only avoided
by picking an SM6 title.

### 41.6 We already own a DXBC->Metal compiler, and it is already on the phone

`research/dxmt/src/airconv` is a complete DXBC->AIR/Metal compiler with its own metallib writer.
It is the production D3D11 shader compiler for DXMT -- the one that drives Thumper, ULTRAKILL,
Stray and Marvel Cosmic Invasion. Its public C API is exactly the shape the shim needs:

```c
int  SM50Initialize(const void *bytecode, size_t size, sm50_shader_t *out,
                    struct MTL_SHADER_REFLECTION *refl, sm50_error_t *err);
int  SM50Compile(sm50_shader_t, struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *args,
                 const char *entry, sm50_bitcode_t *out, sm50_error_t *err);
void SM50GetCompiledBitcode(sm50_bitcode_t, struct SM50_COMPILED_BITCODE *out);
void SM50GetArgumentsInfo(sm50_shader_t, MTL_SM50_SHADER_ARGUMENT *cbs,
                          MTL_SM50_SHADER_ARGUMENT *args);
```

**It ships in the app already.** `build/dxmt-ios/libdxmt_combined.a` (the archive the app links --
note the standing warning that it is `_combined`, not `_unix`) exports `_SM50Initialize`,
`_SM50Compile`, `_SM50GetCompiledBitcode`, `_SM50GetErrorMessage`, and the deployed
`Madeira.debug.dylib` carries 50 `_SM50*` symbols. Both `_SM50Compile` (0x361d00) and our shim's
`_madeira_ir_convert` (0x13a5190) are **in the same image**, so calling it needs a prototype and
nothing else: no new dependency, no new build chain, no new dylib, no extra bytes on the device.

`madeira-d3d12` currently contains **zero** references to `SM50`/`airconv`. The compiler is
sitting there unused.

### 41.7 airconv already speaks SM 5.1, and covers every opcode these shaders use

`src/airconv/dxbc_converter_cfg.cpp:329-370` switches on the declaration's operand index
dimension with an explicit `case D3D10_SB_OPERAND_INDEX_3D: // SM 5.1` for constant buffers,
samplers, SRVs and UAVs, carrying `.space` through into its range records. That is precisely the
form RDR2 emits -- decoded from the bytes of the 24 recovered shaders:

```
cb17[9] space 0      s2 space 0      t104 (ret 0x5555) space 0      u1 typed space 0
dcl_thread_group 8,8,1
```

Every resource declaration in all 24 uses `indexDim=3D`. Aggregate over the sample:

* Shader models: 22x `cs_5_1`, 1x `ps_5_1`, 1x `vs_5_1`. Uniformly 5.1.
* **Register spaces used: {0: 56}.** All 56 declarations are space 0.
* Highest slot per class: `b31`, `s2`, `t104`, `u13`.
* Declaration kinds include `tgsm_structured`, `tgsm_raw`, `uav_raw`, `uav_structured`,
  `resource_structured` -- real compute shaders, not toys.
* **52 distinct opcodes, and airconv references all 52.** 0 missing.
  (Caveat: "referenced in source" is a necessary condition, not proof of correct codegen. But
  these are ordinary SM5 compute/pixel ops on the compiler that ships our playable titles.)

Opcode list checked against `libs/DXBCParser/d3d12tokenizedprogramformat.hpp` with three
validated anchors (`ADD=0x00`, `DCL_THREAD_GROUP=0x9b`, `SYNC=0xbe`) -- my first two attempts at
parsing that enum with a regex silently produced wrong names, so validate anchors before quoting
any opcode name from a generated table.

### 41.8 RETRACTED -- my binding-ABI description was wrong in three ways

**Astra reviewed this section and found three defects; I verified all three against the source
and they are all correct. The original 41.8 text is preserved below the corrections for the
record, but DO NOT BUILD FROM IT.** Corrected account in 41.8a. Astra's full review:
`/Users/willfaust/Documents/Codex/2026-08-05/users-willfaust-documents-ios-pc-game/RDR2-RDR78-SM51-AIRCONV-REVIEW-2026-09-18.md`

### 41.8a The binding ABI, corrected

**C1 -- `SM50BindingSlot` is the declaration RANGE ID, not the register number.**
`dxbc_converter_cfg.cpp:325-350`: for `D3D10_SB_OPERAND_INDEX_3D` (SM 5.1) the code takes
`RangeID = m_Index[0]`, `LB = m_Index[1]`, `RangeSize = m_Index[2] - LB + 1`, and keys
`shader_info.cbufferMap[RangeID]`. `dxbc_converter.cpp:1078+` then sets
`.SM50BindingSlot = range_id` and `.StructurePtrOffset = cbv.arg_index`.
In SM 5.0 the 2D path sets `LB = RangeID`, so the two coincide -- which is why the D3D11 path
has never needed the distinction. **In SM 5.1 they diverge, and RDR2 diverges maximally:** the
first captured shader has range ID 0 for all four of `b17`, `s2`, `t104`, `u1`; the second
captured shader adds UAV range ID 1 at `u2`. Reading a reflected slot 0 as register 0 compiles
clean and binds the wrong resource.
The register identity (`space`, `lower_bound`, `size`) IS parsed and held in the shader_info
range records, but is **not exposed through `MTL_SM50_SHADER_ARGUMENT`**. It must be surfaced.
Do this ADDITIVELY -- a new D3D12 sidecar reflection export -- and never redefine
`SM50BindingSlot`, which D3D11 depends on.

**C2 -- parser acceptance is not array support.** `dxbc_converter.cpp:159-267` installs every
resource lookup callback taking the dynamic index `pvalue` and then discarding it, with the
comment `// ignore index in SM 5.0` (8 occurrences, covering CBV, sampler, SRV and UAV). So a
multi-element or unbounded SM5.1 range silently collapses to its first resource. All 56
declarations in our sample are singleton ranges (`[17,17]`, `[2,2]`, `[104,104]`, `[1,1]`), so
this is correct-by-luck for the captured set. **Scope: support singleton ranges, and refuse
non-singleton ranges and nonzero spaces with a named diagnostic. Never silently collapse.**
My "52/52 opcodes covered" result does not and cannot detect this -- it is a semantic loss, not
a missing opcode.

**C3 -- the GPU layout is not `GetArgumentIndex`.** `dxbc_converter.hpp:460-461`:
`kConstantBufferBindIndex = 29`, `kArgumentBufferBindIndex = 30` -- two argument buffers at
fixed Metal buffer slots. `dxmt_context.cpp:155-355` encodes into a `uint64_t *`:
`encoded_buffer[arg.StructurePtrOffset] = <gpu address or resource id>`, with consecutive words
for the rest (`+1` = buffer byteLength, sampler `+1` = cube handle, `+2` = LOD bias).
**`StructurePtrOffset` is a 64-bit WORD index into that argument buffer.** `GetArgumentIndex`
is only the compiler's internal AIR argument attribute index -- it is NOT a Metal buffer slot
and NOT a word offset. Nothing binds to "Metal buffer slot 440".
Consequently my two reassurances in the original 41.8 -- "all slots fit airconv's index ranges"
and "dropping space is harmless" -- were reasoning from a false premise. The true statements
are: range IDs are small and dense so the index formula is never stressed; and space-0-only
means the *identity map* is recoverable, not that space can be ignored.
The table also carries buffer sizes, texture/view metadata, sampler and cube-sampler handles,
LOD bias and UAV-counter addresses. Copy the encoding semantics from `dxmt_context.cpp`; do not
re-derive them, and do not reuse MSC's inline top-level layout as airconv's CBV pointer table.
D3D12 meanings that must be preserved explicitly: root CBVs, inline root constants (need real
GPU-visible storage with correct lifetime), static samplers, descriptor-table offsets, register
spaces, stage visibility. Tag the backend per shader/PSO; either handle mixed-backend graphics
PSOs or reject them, and never infer all stages' ABI from whichever shader compiled first.

**Also corrected:** my claim that step 1 "cannot regress the DXIL path" is too strong. The MSC
call can stay byte-identical while shared PSO state, reflection, buffer binding and shader
caches still regress it. Keep the A/B switch and the DXIL regression gates.

### 41.8-ORIGINAL (superseded by 41.8a -- retained only to show what was wrong)

This is the whole difficulty, and it is the part to design carefully.

* **What the runtime does today (MSC ABI).** `madeira_d3d12.c` builds a top-level argument buffer
  bound at `kIRArgumentBufferBindPoint 2` (descriptor heap 0, sampler heap 1), filled with
  `IRDescriptorTableEntry` records; a descriptor-table root argument is written as the **absolute
  GPU address of its first descriptor** (measured earlier, see the d3d12 canary notes), and root
  constants go inline. `MAD_ARG_DRAWPARAMS_OFF 512` / `MAD_ARG_DRAWINFO_OFF 544` sit in the same
  buffer.
* **What airconv wants.** Each resource at a fixed argument index derived from its D3D register
  slot, via `GetArgumentIndex()`:
  `CBV -> slot`, `Sampler -> slot + 32`, `SRV -> slot*3 + 128`, `UAV -> slot*3 + 512`.
  Register space is **not** part of the index -- `MTL_SM50_SHADER_ARGUMENT` carries only
  `{Type, SM50BindingSlot, Flags, StructurePtrOffset}`.

Two measurements say the mismatch is survivable for this workload:

1. **Dropping register space is harmless here** -- all 56 declarations are space 0, so no two
   resources can collide on `(type, slot)`.
2. **Every slot fits airconv's index ranges without overflow**: CBV 31 < 32; Sampler 2+32 = 34,
   inside 32..127; SRV 104*3+128 = 440, inside 128..511 (so the SRV slot ceiling is 127 and t104
   clears it); UAV 13*3+512 = 551.

So the remaining work is: for a PSO whose shaders were compiled by airconv, resolve each
`(type, slot)` the shader asks for against the root signature + descriptor heaps and write it into
airconv's indexed binding table, instead of MSC's top-level layout. The runtime already knows how
to resolve a descriptor table to its descriptors, and `SM50GetArgumentsInfo` enumerates exactly
what a given shader needs. Consult DXMT's D3D11 runtime for how it consumes
`MTL_SM50_SHADER_ARGUMENT.StructurePtrOffset` -- that is the reference implementation, and it
should be copied rather than re-derived.

### 41.9 Plan

1. **Detect and route.** In `src/unix/madeira_ir_unix.mm`, parse the container: if it has a `DXIL`
   chunk keep today's MSC path exactly as-is; if it has `SHEX`/`SHDR` and no `DXIL`, route to
   `SM50Initialize`/`SM50Compile`. Report airconv's own error text through `ret_note` so failures
   are readable instead of a code number. This step is self-contained and cannot regress the DXIL
   path (Empire/UE5 shaders keep the same code path byte-for-byte).
2. **Bridge the bindings** per 41.8 -- the real work, and the part worth a careful review.
3. Gate the whole thing behind a config file for a clean single-variable A/B, per the
   one-variable-per-build rule.

### 41.10 Things this section deliberately does not claim

* That airconv's *codegen* is correct for these shaders. Opcode coverage is a necessary condition.
  The offline route (`src/airconv/airconv_cli.cpp` plus the recovered blobs, which are saved as
  hex in the rdr78 log) can settle this on a Mac without spending a device run, and should be run
  before any deploy.
* That 189 shaders is the whole set -- 165 of them exceeded the 1024-byte log dump and were never
  captured. The 24 we have are uniformly 5.1/space-0, but a larger shader may well use a second
  register space or a dynamically-indexed resource array, which is where airconv's space-blind
  index formula would break. Raising the dump ceiling is cheap if that becomes a question.

---

## §42 — Offline gate PASSED: 24/24 real SM5.1 shaders compile through airconv, after two patches

No device run spent. Astra's gate #1 answered on this Mac.

### 42.1 The offline harness

`research/madeira-d3d12/tests/offline/sm51/{build.sh,sm51_probe.cpp}` builds **natively on
macOS** against the SAME pinned LLVM 15 the iOS dxmt build uses
(`toolchains/llvm-host-build`, finished with `ninja` for the 34 archives airconv needs), so a
result here is a statement about the compiler that actually ships, not a different one.
All 15 airconv sources + 3 DXBCParser sources compile clean natively.

The probe calls the SAME entry points the D3D12 runtime will (`SM50Initialize` ->
`SM50GetArgumentsInfo` -> `SM50GetRangeInfo` -> `SM50Compile` -> `SM50GetCompiledBitcode`), and
reports parse / reflect / compile **separately**, because a successful compile does not prove
correct resource selection. It also decodes every declaration itself, independently of airconv,
and cross-checks -- so the identity claim is verified, not asserted.

Corpus: the 24 containers recovered from the rdr78 log, each STRUCTURALLY VALIDATED
(logged size == bytes == container totalSize, chunk directory in range, last chunk ends exactly
at totalSize, SHEX length token == chunk size) and all 24 content-unique. Saved as `.dxbc`.
(Astra's stricter recovery pass got 23; all 24 of mine pass the arithmetic, so the count is not
the interesting part -- each file is individually verified.)

### 42.2 THE REAL BLOCKER, which neither my §41 nor Astra's review caught

First run aborted immediately:

```
Assertion failed: (0 && "TODO: SM5.1"), function readSrcOperand,
                  file dxbc_instructions.cpp, line 322.
```

**airconv could not compile ANY SM 5.1 shader.** The declaration parser handles SM5.1 (the
`INDEX_3D` cases I quoted in §41.7 are real), but `readSrcOperand` for
`D3D10_SB_OPERAND_TYPE_CONSTANT_BUFFER` handled only the 2D form and asserted on everything else.

This kills my §41.7 inference outright. "52/52 opcodes referenced in source" was a necessary
condition that I let read as sufficient; the gap was an unimplemented *operand shape*, which no
opcode census can see. It is exactly the semantic-loss class Astra warned about, one level deeper
than the array-index issue they found.

**And asserts are LIVE in the shipped build** -- `build/dxmt-ios/build.sh` COMMON_FLAGS is
`-arch arm64 -isysroot ... -miphoneos-version-min=18.0 -fblocks -O2`, no `-DNDEBUG`, and
`DXASSERT_DXBC(x)` is plain `assert(x)`. Had the routing shipped before this fix, the device
would have `abort()`ed the whole app on the first RDR2 compute PSO, not failed gracefully.
(Under `-DNDEBUG` it would instead have fallen off the end of a non-void function: UB.)

### 42.3 Patch 1 -- the SM5.1 constant-buffer operand (`dxbc_instructions.cpp`)

SM 5.0 spells the operand `cb<rangeid>[<regindex>]` (2D). SM 5.1 inserts the index WITHIN the
descriptor range: `cb<rangeid>[<rangeindex>][<regindex>]` (3D), so every index read shifts up one
position. `SrcOperandConstantBuffer` **already had all three fields**, and
`readSrcOperandResource` / `readSrcOperandSampler` / `readSrcOperandUAV` **already handled both
shapes** with exactly this one-position shift. The constant buffer was the single operand type
left unimplemented. The patch adds the 3D case by direct analogy.

Cannot regress D3D11: the 2D path is byte-identical, and the only behaviour replaced is an
`assert(0)`/UB.

### 42.4 Patch 2 -- `SM50GetRangeInfo`, an additive export (Astra's correction 1)

`MTL_SM50_SHADER_ARGUMENT.SM50BindingSlot` is the declaration RANGE ID. Measured on the real
shaders, the divergence is the NORM, not a corner case: **22 of 24 shaders have
range_id != register**, and the first one reflects slot 0 four times for `b17/s2/t104/u1`.
Binding by the reflected slot binds four wrong resources, silently, with a clean compile.

The identity (`space`, `lower_bound`, `size`) is already parsed into `ResourceRange` and held in
`shader_info.{cbufferMap,samplerMap,srvMap,uavMap}[range_id].range` -- it was simply unreachable
from outside the compiler. New struct `MTL_SM50_RANGE_INFO {Type, RangeID, RegisterSpace,
LowerBound, RangeSize, StructurePtrOffset}` and
`SM50GetRangeInfo(shader, pRanges, Capacity) -> total` (call with Capacity 0 to size).
Purely additive: no existing struct, field or entry point changes meaning, so the D3D11 path --
which relies on the SM 5.0 coincidence -- is untouched.

### 42.5 Results, all 24 shaders

```
  shaders                       : 24
  SM50Initialize OK             : 24
  SM50Compile OK                : 24        (2690 - 5218 bytes of AIR each)
  with range_id != register     : 22        <-- reflection alone is insufficient
  with a non-singleton range    : 0         <-- must be refused, not collapsed
  with a nonzero register space : 0         <-- must be mapped or refused
  SM50GetRangeInfo mismatches   : 0         <-- vs my independent decode
```

Astra's corrections 1 and 3 are now confirmed EMPIRICALLY, not just by reading source:

* `cbTableBind=29`, `argBufBind=30` on every shader that has those tables -- and **`0xffffffff`
  when it does not** (e.g. sh08_PS: numCB=0, numArgs=0, both sentinels). The runtime must handle
  the sentinel; do not bind table 29/30 unconditionally.
* `argTableQwords` reconciles exactly with the reported offsets: sh00 has sampler at word 0
  (3 words: handle, cube handle, LOD bias), SRV at 3 (2 words: address, byteLength), UAV at 5
  (2 words) = 7 = `argTableQwords`. **StructurePtrOffset is a 64-bit word index**, measured.
* Threadgroup sizes come back correct and varied (8x8x1, 64x1x1, 384x1x1, 1x32x16), matching
  the `dcl_thread_group` I decoded by hand for sh00.

Also learned: **airconv NAMES the emitted Metal function from the string you pass to
`SM50Compile`**; it does not read an entry name out of the bytecode the way Apple's converter
does (D3D11 passes `"vs_" + sha1 + "_" + variant_digest`). Passing `nullptr` strlen()s a null
pointer and segfaults. The D3D12 shim therefore CHOOSES the name and then calls
`newFunction(name)` -- simpler than the MSC path, but a different cache-identity input.

### 42.6 What is still NOT proved

* **Metal has not accepted this AIR.** We produced bytecode; nothing has loaded it into an
  `MTLLibrary` or built a PSO. That is Astra's gate #4 and it needs a device or a Metal path.
* **The binding bridge is not written.** This is the remaining real work (§41.8a). Compiling is
  necessary, not sufficient -- `SM50GetRangeInfo` now makes correct binding *possible*.
* **165 of 189 shaders were never captured** (the log dumps only containers <= 1024 bytes). The
  24 we have are uniformly 5.1 / space 0 / singleton ranges; a larger shader may not be. Raising
  the dump ceiling is cheap if that becomes a question. Refuse non-singleton ranges and nonzero
  spaces with a named diagnostic -- never collapse them, since `dxbc_converter.cpp:159-267`
  discards the dynamic index.
* Refusals belong in the RUNTIME SHIM as error returns, not as asserts inside airconv: asserts
  are live on device and would kill the app.
* This is not the only remaining blocker. §40 records root-signature descriptor-range limit
  failures (7 hits) and the `0x2c1f7d9a4` illegal instruction; both still open.

---

## §43 — ml1008: the SM5/DXBC backend and its binding bridge, built and deployed to vphone

One variable: D3D12 shaders that are SM5.x DXBC now compile through the in-tree
AIR compiler and bind through its own tables. The DXIL path is untouched.

### 43.1 Gate #4 passed OFFLINE, on the GPU the guest actually renders on

Before writing any runtime code: `tests/offline/sm51/metal_load.m` hands each
compiled metallib to a real `MTLDevice`. On **Apple M4 Max** -- which in remote
Metal mode IS the host GPU -- all 24 load:

```
libraries=24  functions=24  computePSOs=22  failures=0
```

22 not 24 because the VS and PS correctly report `functionType` 1 and 2 and
cannot be standalone compute PSOs. `maxTotalThreadsPerThreadgroup` tracks the
declared threadgroup sizes exactly (384 for `tg=384x1x1`, 512 for `1x32x16`,
1 for `1x1x1`), so the metadata survived. `xcrun metal-nm` also reads the
container and lists the function. Output magic is `MTLB` -- airconv emits a full
metallib, so `MTLDevice_newLibrary` needs no new handling.

This is why the build that shipped includes the BINDING BRIDGE rather than being
staged as compile-only: compile and Metal acceptance were already answered
offline, so a compile-only device run would have told us nothing new.

### 43.2 Three airconv changes

1. **`dxbc_instructions.cpp`** -- the SM5.1 constant-buffer operand (§42.3).
2. **`SM50GetRangeInfo`** -- rewritten to iterate the SAME reflection arrays the
   encoder consumes, in the same order, joining each with its declaration
   record. So record i describes reflected argument i, and `Flags` /
   `StructurePtrOffset` cannot drift from what the encoder was told. Also
   returns `IsConstantBufferTable`, because the two tables are separate.
   A reflected argument with no declaration record reports space `~0` rather
   than a plausible-looking `space 0`, so the caller refuses it.
3. Nothing else. Both are additive or replace an `assert(0)`.

### 43.3 The flag census that defined the encoder

Measured across the corpus, so the runtime implements what is used, not a guess:

```
  15 CBV     cbtable  0x401              BUFFER|READ                 -> 1 word
   7 Sampler argtable 0                                              -> 3 words
   2 SRV     argtable 0x405              BUFFER|ELEMENT_WIDTH|READ   -> 2 words
   4 SRV     argtable 0x412              TEXTURE|MINLOD|READ         -> 2 words
   1 SRV     argtable 0x422              TEXTURE|TBUFFER_OFFSET|READ -> 2 words
   6 SRV     argtable 0x452              TEXTURE|MINLOD|ARRAY|READ   -> 2 words
   2 UAV     argtable 0x422 / 8 0x805 / 5 0x812 / 3 0x822 / 7 0x852
```

**No `UAV_COUNTER` (0x8) anywhere**, so no counter resource is modelled; the one
place it could appear writes 0 and says so in a comment.

### 43.4 Why no new per-descriptor state was needed

Our existing `mad_descriptor {gpu_va, texture_view_id, metadata}` already
distinguishes exactly the three shapes the backend encodes differently:

```
  plain buffer : texture_view_id == 0, gpu_va = address, metadata = byte length
  texture      : texture_view_id != 0, metadata bit 63 CLEAR, low half = min LOD clamp
  typed buffer : texture_view_id != 0, metadata bit 63 SET  (mad_typed_buffer_view)
```

and `TextureMetadata(array_length, min_lod) = (len << 32) | lod_bits` -- whose
low half our texture descriptor already holds. The two missing fields come from
reverse lookups that already existed or were trivial: `mad_texture_of_view`
gives the view's slice count (array length), and a `tview` scan recomputes a
typed buffer's element count and first element exactly as the view builder did.

So: no parallel aux array, no descriptor-heap registry, no `CopyDescriptors`
changes, and `mad_descriptor` stays byte-identical for the DXIL path. An earlier
plan to pack array length into the descriptor's unused high half was dropped --
it would have put a DXIL-path regression risk on an unverifiable assumption
about what Apple's shaders read.

### 43.5 Resolution, and what is refused

`mad_air_resolve` searches the root signature for the range's REGISTER
(`lower_bound`), never its range id:

* **descriptor table** -- match (range type, space, register in
  [base, base+count)); `idx = table_offset + (register - base_register)`;
  descriptor address = `root[param] + idx * 24`; then bounds-check against the
  bound heap (`e->srv` / `e->smp`, which the executor already tracks) and read
  `heap->cpu[...]`. Below-heap, unaligned and past-the-end all report distinctly.
* **root CBV** -- the root value IS the address.
* **root constants consumed as a CBV** -- the backend wants a POINTER, so the
  words are copied into a ring slot (`exec_ring_take`, factored out of
  `exec_arg_slot_for`) whose lifetime already matches the dispatch, and its GPU
  address is bound. `mad_list` gained `ring_gpu` for this.
* **root SRV/UAV** -- REFUSED: carries an address and no length, and the buffer
  encoding needs a byte length. Refusing beats inventing a bound.
* **static samplers** -- REFUSED with a named reason: the DXIL converter bakes
  them into the shader, the DXBC backend expects them in the table. RDR2's root
  signatures report 0 static samplers, so this is a diagnostic, not a blocker.

Anything unresolvable fails the DISPATCH with one named line per range and
increments `skipped` -- never a dispatch against a half-filled table, and never
a silently empty pipeline.

### 43.6 Two deploy-path defects found on the way

* `scripts/deploy-vm.sh` FILES had no entry for `madeira_d3d12.dll` or
  `d3d12.dll`, so a native-D3D12 change had **no route to the device at all** --
  the same omission `dxgi.dll` had, which previously ran old code for a whole
  round of DXMT work. Both added.
* `build/dxmt-ios/build.sh` compiled the conversion shim without `$INCLUDES`, so
  `airconv_public.h` was unreachable. Added.

### 43.7 A layout near-miss worth remembering

I first hand-rolled `MTL_SHADER_REFLECTION` in the shim as three `uint32_t` for
the union, then "corrected" it to 32 bytes by reasoning about
`MTL_GEOMETRY_SHADER_PASS_THROUGH`. Measuring it (`offsetof` against the real
header) gave **72 bytes with the union at 12** -- so the first guess was right by
accident and the correction was wrong. The shim now `#include`s the compiler's
real header. Do not restate that struct; the ABI header's own opening comment
says exactly this and I nearly ignored it.

### 43.8 Deployed state (vphone, verified by CONTENT on the device)

`strings` and `nm` do NOT exist on the VM -- an earlier check returned 0 for
every marker purely because the tools were missing, which is the
absence-of-a-probe-string trap. Verified with `grep -ac` on the deployed files:

```
  dylib: ml1008 route banner     1      d3d12: sm5 dispatch line      1
  dylib: singleton refusal       1      d3d12: cannot-bind diagnostic 1
  dylib: space refusal           1      d3d12: sm5/dxbc label         2
  dylib: SM50GetRangeInfo        1
```

Nothing resident, log cleared, next launch is cold. Config unchanged:
`jumbo-mb=9216 arena-mb=4096 totalphys=4096 d3d12=1 apicensus=1`,
`dxmt=d3d11.noMeshShaders=0`, remote Metal on, `AppDefaults\RDR2.exe` renderer
`no3d`.

### 43.9 What to read in the next log

* `[madeira-ir] ml1008 shader-model-5.1 DXBC -> in-tree AIR compiler` -- once.
* `ml1008 CS via the sm5/dxbc backend: ... ranges=N` plus one `range b17 space 0
  (id 0) -> cbtable word 0` line per range, for the first 12 pipelines. If the
  register in that line is 0 where it should be 17, the identity plumbing broke.
* `CS conversion failed` should go from 187 to ~0. Any that remain now name the
  backend (`sm5/dxbc` vs `dxil`) and carry a note.
* `ml1008 ... cannot bind ...: <reason>` -- the binding bridge's own failures,
  which is the most likely place for this build to fall short.
* Still open and NOT addressed here: the root-signature descriptor-range limit
  (7 hits) and the `0x2c1f7d9a4` illegal instruction.

---

## §44 — rdr79: the SM5 backend WORKS. Next wall was our own 32-descriptor-range cap

### 44.1 The first launch after §43 crashed in dyld -- MY error, not the code

`DYLD / Library missing: Library not loaded: @rpath/Madeira.debug.dylib`,
`Reason: missing code signature`. The app build had been made with
`CODE_SIGNING_ALLOWED=NO`, and that unsigned dylib is what was deployed. The VM
does not enforce the APP signature -- which is why in-place replacement works --
but dyld still refuses a dylib carrying NO signature.

Two lessons, both now encoded in `scripts/deploy-vm.sh`:

* **sha256 verification cannot catch this.** An unsigned file verifies against
  itself perfectly; the deploy printed "OK (sha256 verified)" for a file that
  could never load. The script now runs `codesign -dv` on
  `Madeira.debug.dylib` and `libmetalirconverter.dylib` and ABORTS before
  pushing anything.
* **The signature is what makes "no log at all" diagnostic.** The deploy clears
  the log and a launch always recreates it, so an absent log means the crash is
  before `LogStore.init` -- which rules out the entire runtime immediately.
  Presents as a catastrophic regression; is not one.

### 44.2 The real run: shader conversion is FIXED

| milestone | rdr78 | rdr79 |
|---|---|---|
| root signatures parsed | 508 | **10,154** |
| shader conversions failed | **189** | **0** |
| compute pipelines built | 0 | 8 |
| `[wmt-remote]` flushes | 22 | 64 |
| log lines | 43,087 | 82,415 |

`[madeira-ir] ml1008 shader-model-5.1 DXBC -> in-tree AIR compiler` fired once,
as designed. Zero conversion failures of any stage. The game loaded ~20x deeper
into its shader/PSO set. User-visible: FPS 2-6 climbing to 60 for several
seconds, all presents black, then a crash to the home screen.

### 44.3 Why it was still black -- 5,435 root signatures REFUSED BY US

```
5435  CreateRootSignature: more descriptor ranges than this build handles
 167  newRenderPipelineState failed
```

`MAD_ROOT_RANGE_MAX` was **32**, and exceeding it returned `E_NOTIMPL`. RDR2's
real root signatures routinely need more, and every refusal takes its pipelines
with it -- so almost nothing could draw and the presents were bare clears.
rdr78 saw only 7 of these purely because it never got this far.

None of the sibling limits fired: 0 for parameters, static samplers, version and
truncation. One single arbitrary cap of ours was the wall.

### 44.4 ml1009 -- blob-sized, not a bigger guess

D3D12 puts no small bound on descriptor-range count. The 64-DWORD budget bounds
root ARGUMENTS, and a descriptor table costs one DWORD however many ranges it
names, so any fixed cap only moves the wall. And a bigger fixed array is
expensive here: 10,154 root signatures x 256 ranges x 24 B would be ~62 MB of
mostly-unused memory against a 4096 MB jetsam limit.

So `struct mad_rootsig::ranges` became a pointer, and the parser gained a
**counting pre-pass** over the RTS0 parameter table. The array is allocated in
the SAME block as the object (`calloc(1, sizeof *r + total * sizeof range)`,
`r->ranges` pointed at the tail), because `mad_release` does a single `free(o)`
-- so no destructor change and no leak. `MAD_ROOT_RANGE_SANE` (8192) only
refuses a corrupt blob, and its message PRINTS the count it saw.
`ml1009 new high-water descriptor range count: N` reports what real signatures
actually need, so the next person has evidence instead of folklore.

Also fixed in `mad_serialize_any` (the 1.0 -> 1.1 lift), which had the SAME cap
in two places and returned `E_NOTIMPL` **with no log line at all** -- a silent
refusal in a path the application cannot see. Both now log, the range array is
sized from a pre-count, and the fixed `unsigned char out[4096]` became a
size-then-fill against the serializer's documented protocol
(`if (!out || *io_len < total) { *io_len = total; return out ? E_NOT_SUFFICIENT_BUFFER : S_OK; }`
-- verified in the implementation, not assumed).

### 44.5 The binding bridge is STILL UNTESTED

`ml1008 dispatching` = **0** and `cannot bind` = **0**: not one dispatch reached
the new sm5 table path, because the game died during PSO loading before issuing
any compute work. The bridge compiled, deployed and never ran. Do not read
rdr79 as evidence for or against it.

### 44.6 The crash is separate and still open

Not the SMC writes -- those are being serviced correctly: 12 `[bus-data-av]`
protection-AV writes into the image, and 60 guest VEH returns of `ffffffff`
(EXCEPTION_CONTINUE_EXECUTION). That mechanism works.

The fatal fault is `pc=0x13706418c addr=0x408596c0400` with the guest
`rip` poisoned to `0x0202020202020202`, repeated until
`[redeliv] 2000 identical redeliveries ... unrecoverable host fault misdelivered
to guest` then `terminating process rev=ml465`. `pc` is inside the JIT pool
(`0x119e4c000-0x139e4c000`) and Wine confirms it has no view of it, so it is
FEX-compiled guest code. `x9` points into the guest stack at a region filled
with `0x02` then `0x03` bytes, and `x20` points at a `DXBC` container
(`43425844` + `ISGN`) -- so the crash is in code handling a shader blob.
Different from rdr78's `c000001d` at `0x2c1f7d9a4`.

Whether it survives the root-signature fix is the next question: with 5,435
fewer refused signatures the game takes a different path through that code.

---

## §45 — rdr83/rdr84: the LOADING BAR RENDERED, and the guest crash was downstream of rmetald all along

### 45.1 First real rendering (rdr83)

RDR2 drew its initial loading bar. `cannot bind` went 32 -> 0 and rmetald's blank
counter collapsed from **1856 of 1856** to **3**. The unlock was ml1012:
`D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND` (0xffffffff) was being read as a literal
descriptor index, and because an unresolvable range aborts the WHOLE draw, that
one field was suppressing every affected draw. `mad_table_count` already knew the
accumulate rule; I had not reused it.

### 45.2 The guest crash is NOT an independent bug

Chased for four runs as a deterministic guest fault (`pc=0`, `RIP=0x170`,
identical `x20` across rdr81-84). It is fallout. The rdr84 ordering is explicit:

```
[wmt-remote] ml820 readback FAILED ... status=4294967295 got=0     <- host RPC failed
[wmt-remote] no drawable from the host -- this frame will be blank
[madeira-d3d12] Present: the layer gave no drawable
[madeira-d3d12] no command buffer            line 62,656  (x81, each DISCARDING a command list)
...
segv_handler SEGV #1                          line 63,681
```

`no command buffer` is `MTLCommandQueue_commandBuffer` returning 0 -- the HOST
failing. rmetald dies, every Metal call starts returning 0, the guest throws away
81 command lists, and then crashes dereferencing those failures.

**Only TWO segv events in the whole run**: the truncated-pointer read
(`addr=0xf91a47d1`, ml962 says "OPERAND ALREADY TRUNCATED -> trace its WRITER"),
then after the guest VEH returns EXCEPTION_CONTINUE_EXECUTION, a jump to 0.
Chasing that as the root cause was wrong.

⚠️ Note for next time: `ReconstructThreadState` is the AUTHORITY for the guest
rip. `[guest-state] rip=` printed by our ntdll probe is the pre-reconstruction
(stale) value, so the two disagreeing is normal, NOT a bug. I nearly filed it as
one.

### 45.3 Three separate host fatalities, all "the host must not die for a guest mistake"

* **ml1010** -- `RM_OP_COMMIT` committed with an encoder still open;
  Metal aborts on `commit command buffer with uncommitted encoder`. Encoders were
  tracked for teardown but without their command buffer, so commit could not
  close them. Now paired and closed. **Confirmed firing** ("closed 1 encoder(s)").
* **ml1013 -> ml1014** -- rmetald segfaulted in
  `-[AGXG16XFamilyBlitContext copyFromBuffer:...]`. I guessed out-of-range
  extents; **ml1013 never fired**, so that was WRONG. ml1014 instead validates
  that the handles are actually `MTLBuffer` (slots hold an untyped `id`, so a
  texture or stale object resolves just as happily), refuses zero-length copies,
  and PRINTS every copy that looks wrong plus the first 8 that look fine. It
  showed all copies valid and in range -- so the segfault has another cause,
  still open.
* **ml1015** -- `-[_MTLCommandBuffer addCompletedHandler:]: Completed handler
  provided after commit call`. Other ops commit internally (rmetald.m:1173, 1645),
  so a buffer can already be committed when the guest's own COMMIT arrives;
  `RM_OP_COMMIT` then attaches a handler to a committed buffer and Metal aborts.
  Guarded using Metal's OWN status (NotEnqueued/Enqueued = not yet committed), so
  there is no side table to keep in sync.

### 45.4 Still open

* The rmetald `copyFromBuffer:` SIGSEGV (`KERN_INVALID_ADDRESS at 0x1398`), with
  handles and extents proven valid. Suspect a released-but-slotted buffer or an
  encoder used after ml1010 auto-closed it.
* 23 pipeline failures, mostly `rasterSampleCount (8) is not supported by device`.
  Likely OUR bug: we probably advertise 8x MSAA in CheckFeatureSupport without
  asking the device. Clean accuracy fix, not yet done.
* `Fragment input(s) user(texcoord4),user(texcoord7) mismatching vertex shader
  output` and `Fragment shader does not write to render target color(0) required
  for blending` -- a handful each.
* `CryptDecodeObjectEx Unsupported decoder for 1.3.6.1.4.1.311.2.1.4`
  (SPC_INDIRECT_DATA_OBJID, Authenticode) x4 in every run. NOT the crash cause --
  that was ruled out by the ordering above -- but still a real gap.

---

## §46 — rdr85/rdr86: the host is FIXED; the guest crash is ours, in the IAT sync

### 46.1 Host: clean (ml1015 + ml1016)

rdr85/rdr86 ran with rmetald alive to the end. In the guest log:
`no command buffer` **0**, `readback FAILED` **0**, `no drawable` **0**,
`BLANK frame` **0**. ml1016 intervened exactly once per run.

Four separate host fatalities are now closed, and each only became visible after
the previous one was fixed:

1. **ml1010** commit with an open encoder (`commit command buffer with
   uncommitted encoder`). Encoders were tracked but not paired with their command
   buffer, so commit could not close them.
2. **ml1013/ml1014** blit SIGSEGV. ml1013 (extent bounds) was the WRONG guess and
   never fired; ml1014's diagnostics proved every copy had valid `MTLBuffer`
   handles and in-range extents, which is what pointed at the encoder instead.
3. **ml1015** `addCompletedHandler` after commit. Other ops (rmetald.m:1173, 1645)
   commit internally, so the guest's own COMMIT could hit an already-committed
   buffer. Guarded with Metal's own `status`.
4. **ml1016** commands issued to an encoder ml1010 had already ended -- AGX
   dereferences a torn-down context. THIS was the repeated `copyFromBuffer:`
   SIGSEGV at `KERN_INVALID_ADDRESS 0x1398`, i.e. **my own ml1010 fix caused it.**
   Guarded on all three streams (blit/compute/render), and made FAIL-PERMISSIVE:
   the 256-entry table latches an overflow flag, because otherwise an untracked
   encoder would look "ended" and we would silently drop ALL rendering.

⚠️ ml1016 DROPS that encoder's later work. It fires once per run today. Many
`ml1016 dropping` lines would mean rendering is being lost and the guest-side
leak (we commit with one encoder still open, once per run) needs a real fix.

### 46.2 The guest crash, byte-level

Deterministic and identical across rdr81-rdr86: fault address **`0xf91a47d1`
every run** (only the JIT pc moves). Exactly TWO segv events -- the bad read,
then, after the guest VEH returns EXCEPTION_CONTINUE_EXECUTION, a jump to 0.

Decoded: the block's x86 bytes are
`ff b0 b1 b2 f8 b3 | b0 40 01 00 00 00 | 55 48 8d 2d`, which read as
`push qword [rax - 0x4C074D4F]`; with `rax=0x145219520` that computes **exactly
0xf91a47d1**. So FEX translated faithfully -- the *instruction is not code*.
`55 48 8d 2d` (push rbp; lea rbp,[rip+..]) is a real prologue 12 bytes later, and
`f8 b3 b0 40 01 00 00 00` at +4 is literally the pointer **0x140b0b3f8**.
`0x1460eb684` is a POINTER TABLE, not an instruction boundary.

And our own code rewrote it:

```
[bus-data-av] addr=0x1460eb66a rw=1                 <- the DRM writes into this page (SMC)
ml1001: previous SMC store at 0x1460eb668 changed the bytes (store landed)
[x86-ptr]  region 0x1460eb000+0x1000: KEPT 3 guest x86-CODE pointers (first 0x140b0b3f8), translated 4 others
[iat-sync] region 0x1460eb000+0x1000: translated 4 pointers
```

The IAT sync walks the region as `uint64_t[]` and rewrites every translatable
value -- **into `jit_rw_dest`, the pool copy FEX executes**. Its own comment says
it covers "ALL pointers within a PE mapping (not just .text)". ml102 narrowed it
by the VALUE; nothing ever checked the DESTINATION. An 8-byte aligned window over
instruction bytes can look translatable by coincidence.

The same shape is already recorded in this file for ULTRAKILL (ml632's tripwire):
"`[iat-sync] ... translated 2 pointers` rewrote the page holding the bad block's
entry ... control ran off into zero padding."

Note the asymmetry that makes this clearly a bug: the **memcpy** immediately above
the loop carefully skips `.text` ("to avoid overwriting JIT-relocated code"), and
the translation loop never inherited that.

### 46.3 ml1017, and two wrong versions of it before the third

* **v1 (measured NOT firing, 0 hits)**: passed `jit_rw_dest` to a PE-header
  parser. `jit_rw_dest` is the destination for THIS sub-region, not the image
  base, so the MZ check failed and the guard silently never ran while the sync
  still reported "translated 4 pointers". **A guard that cannot fire looks
  exactly like a guard that found nothing -- always confirm it fired.**
* **v2 (would also not have fired)**: used the mapping's `text_offset/text_size`.
  That records only the LARGEST executable section; the offending page is at RVA
  `0x60eb000`, past `.reloc` at `0x5e88000`, so it is a packer/DRM section and a
  .text-only test misses it.
* **v3 (deployed)**: `ios_img_off_is_exec(pe_start, pe_end - pe_start, off + slot)`
  walks the REAL section table and declines any slot in ANY section marked
  `IMAGE_SCN_MEM_EXECUTE`. A real import table lives in a DATA section, so the
  case the sync exists for (the rpcss milestone) is preserved.
  Reversible: `madeira-iat-exec.txt` = `1` restores pre-ml1017 behaviour.

### 46.4 If ml1017 fires and the crash SURVIVES

Then the corruption is not (only) the IAT sync, and the next suspects are, in
order:

1. The SMC store path itself -- ml1001 says the store at `0x1460eb668` "landed",
   but verify the bytes match what the DRM intended, and that we are not also
   writing through a stale alias.
2. `[x86-ptr] KEPT 3 ... translated 4 others` appears THREE times for this page
   per run: the sync re-runs after each SMC write. Even a correct rewrite applied
   twice, or applied after decryption changed the bytes' meaning, is wrong.
3. Whoever branches to `0x1460eb684`. It is a pointer table, so a caller loaded a
   function pointer from the wrong slot -- possibly off-by-one against a table our
   sync shifted.

### 46.5 Unrelated but still open

* 23 pipeline failures, mostly `rasterSampleCount (8) is not supported by device`
  -- we likely advertise 8x MSAA in CheckFeatureSupport without asking the device.
* `Fragment input(s) user(texcoord4),user(texcoord7) mismatching vertex shader
  output`, and `Fragment shader does not write to render target color(0) required
  for blending`.
* `CryptDecodeObjectEx Unsupported decoder for 1.3.6.1.4.1.311.2.1.4`
  (SPC_INDIRECT_DATA_OBJID) x4 every run. Ruled OUT as the crash cause by the
  rdr84 ordering, but still a real gap.
* The guest-side single leaked encoder per run (see 46.1).

---

## §47 — ml1017 verdict: it FIRES, it did NOT fix the crash, and it cost a slow load

### 47.1 It fired, heavily

v3 of the guard works: **20,544 declines in one run**, e.g.
`region 0x145f65000+0x1000: DECLINED 34 rewrite(s) in EXECUTABLE sections`.
Against 51,456 rewrites still allowed. So the IAT sync was routinely rewriting
slots inside executable sections across the whole program, not just on the one
DRM page -- which is either a large real bug this stops, or a guard that is too
broad. **Unresolved, and the reason it is now opt-in.**

### 47.2 The crash SURVIVED it

Same crash. So corrupted pointer slots in executable sections are not the (only)
cause of the `0xf91a47d1` read. §46.4's follow-on suspects stand, and the most
suspicious remains that the sync re-runs on the offending page THREE times per
run, once after each SMC write.

### 47.3 It made the loading screen crawl -- two bugs of mine

1. **An unbounded `dprintf` per region.** 20,544 stderr lines; the log went
   72k -> 101k lines. On this setup stderr logging is expensive and this was most
   of the cost. Now rate-limited to 32.
2. **A PE-header parse plus a full section walk PER 8-BYTE SLOT** -- roughly
   72,000 times per run. Now ONE lookup per region (a 4 KB region lies inside a
   single section except pathologically; a straddling region tests both ends and
   is treated as executable only when both agree).

🔑 Lesson: a guard placed in the innermost loop of a hot path needs its cost
considered before its correctness is even testable -- the slow load nearly
masked the result.

### 47.4 Current state: DEFAULT OFF

`madeira-iat-noexec.txt` = `1` enables the guard (file absent = off, and it has
been removed from the VM). Default is pre-ml1017 behaviour: fast, known, and the
same state the loading bar first rendered in.

Reasons to leave it off until someone can judge 47.1: it did not fix the crash,
it suppressed 20,544 rewrites whose legitimacy is unverified, and a packed
executable can mark its import table executable -- in which case the guard
suppresses exactly what the sync exists for (the rpcss milestone depends on it).

**To resume this**: the question to answer first is not "does the guard help" but
"of those 20,544 declined slots, how many were real import-table entries?" Dump
the declined (offset, value, section name) tuples for one region and compare
against that image's import directory. If they are import entries, the guard is
wrong and the real fix is narrower -- e.g. decline only slots inside pages the
DRM has self-modified, which we already track via ml1001.

---

## §48 — rdr88/rdr89: ml1018 KILLED THE CRASH. Now a wineserver sync freeze.

### 48.1 The crash is gone (Astra's diagnosis, ml1018)

rdr88 and rdr89: **zero segv events in the entire run**, where rdr81-87 each had
exactly two (the bad read at `0xf91a47d1`, then the jump to 0). Zero
`REFUSING low/invalid RIP=0x170`, zero `redeliv terminating`. ml1018 fired 16x
(its log cap).

Declining the byte-store backpatch in FEX's SMC path was the fix. Verified before
implementing: the emitter reserves NO slot for i8 (`stlrb` bare, while every
wider size emits `nop()` commented "Half-barrier once back-patched"), the handler
writes DMB over PC[-1] regardless and returns -4, and ml657's own comment already
flagged the hole ("an ALIGNED store trapped by SMC would still loop" -- a byte
store is ALWAYS aligned).

**The PC[-1] capture Astra asked for**: `PC[-1]=0x38BFC108` = `strb w8,[x8,xzr]`
-- itself an ALREADY-BACKPATCHED byte store, not a live `add`. So this site is not
the ULTRAKILL shape exactly; the old path was destroying another store rather than
an address producer. The guard is still right, but do not describe this site as
"a destroyed address calculation".

### 48.2 ERR_GFX_STATE was a fifth host fatality (ml1019)

rdr88 ended with RDR2's own `ERR_GFX_STATE` dialog -- progress: the game survived
to run its own error handling. Cause was rmetald dying again:

```
-[AGXG16XFamilyCommandBuffer blitCommandEncoderCommon:]:891: failed assertion
    `A command encoder is already encoding to this command buffer'
```

Metal permits ONE active encoder per command buffer. ml1019 ends the outstanding
one at creation time on all three sites (render/blit/compute), reusing the
per-command-buffer tracking ml1010 added. Five host fatalities are now closed;
each only became visible once the previous was fixed.

### 48.3 The shader cache works, and pays off WITHIN a single run (ml1020)

rdr89 was visibly faster on its FIRST cached run, which looked wrong -- an empty
cache should not help. It does, because RDR2 compiles the same shader repeatedly:

```
cache entries written : 1,828
shaders compiled      : 4,139
```

So ~2,300 of those were duplicates served from cache inside the same run. The
speedup is real and intra-run; cross-run will be larger.

Cache design notes: it stores the REFLECTION as well as the metallib (bind
indices, table size, threadgroup size, slot mask, resolved ranges) because
recovering those means re-running the compiler. The key covers bytecode + Metal
version + format revision + **the resolved input-layout elements** (the vertex
fetch is generated from them, so a layout-blind key would serve a shader built
for a different layout) + the TU build stamp (a cache entry from an older
compiler must never be reused). Written temp-then-rename so a crash cannot leave
a torn entry a later run trusts.

Measurement that justified it: ~20 shaders/sec, 4,139 per run, 15,589 root
signatures. Before the SM5 backend every shader failed instantly, which is why
loading used to be fast AND black -- the cost is the price of it working, not a
regression (rdr85/86 had identical log composition).

### 48.4 THE CURRENT WALL: a wineserver wait that never satisfies

rdr89 freezes at the end of the loading bar. NOT a crash: the process is alive,
burning ~54% of a core, log still growing ~100 lines/sec, rmetald healthy,
footprint cycling (cycle=404, so ~13.5 min in).

⚠️ The `[evt-hist]` / `[evt-entry]` / `[evt-life]` flood is NOT the loop -- it is
`ios_evt_dump_for`, a DIAGNOSTIC that `[srv-stuck]` calls, reprinting history
every cycle. And `state_before=-1` is the ring's unset marker, not a bad state.
I nearly read both as the bug.

The actual evidence, 1,387 `[srv-stuck]` lines:

```
tid=N age=Nms op=1 flags=N timeout=N alertable=0 nhandles=1
  h[0]=N obj=... type=Event      raw_obj=... raw_waiters=0  sync_obj=... sync_waiters=1  state=LINKED
  h[0]=N obj=... type=Semaphore  raw_obj=... raw_waiters=0  sync_obj=... sync_waiters=1  state=LINKED
```

**The waiter is queued on the sync object with ZERO waiters on the raw object.**
That is a split between Wine's in-process sync objects (`wine/server/inproc_sync.c`)
and the classic server objects: a signal delivered to one side does not wake a
waiter parked on the other. Also present: `[alert-ring] last8: 00d0->008c
008c->00d0 ...`, a two-thread ping-pong, and `[waiters] parked=9 over60s=8` with
8 threads at age 104s on CONSECUTIVE addresses (a thread pool idling -- per
earlier notes these idle workers are normal and have been misattributed before,
so do NOT treat them as the hang).

There is **no env/registry toggle** for inproc sync in this tree, so there is no
cheap A/B to confirm it; `get_inproc_sync_fd` is reached unconditionally.

Next step: establish whether SetEvent signals the raw object while the waiter is
linked only to the sync object, i.e. whether the LINKED transition requeues
existing waiters. Instrument the signal path for one of these objects
(`obj=` from a `[srv-stuck]` line) and record which side the signal lands on
versus which side the waiter is on. Do not widen this into a general sync rewrite
before that measurement exists.

## §49 — rdr92..rdr98: the "exited unexpectedly" dialog is VA EXHAUSTION. ml1026 killed the audio crash; ml1027 cleared the 692MB wall; the blocker is now a 2.5MB Foundation allocation that kills the Metal transport

Logs (scratchpad): `ml-rdr92b.txt` `ml-rdr93.txt` `ml-rdr94.txt` `ml-rdr95.txt`
`ml-rdr96.txt` `ml-rdr97.txt` `ml-rdr98.txt`. rdr98 is the current state.

### 49.1 What is FIXED and device-confirmed

**ml1026 — one process-wide RemoteIO endpoint with a mixer.** `audio_null_ios.c`.

ml739 correctly gave each WASAPI client its own `struct ios_stream`, but it also
gave each one its own `AudioUnit`. On iOS `kAudioUnitSubType_RemoteIO` IS the
hardware I/O unit and a process gets exactly one. RDR2 opens a cutscene client
(48k/2ch float32, fb=8) over its main client (48k/2ch PCM16, fb=4), so we
instantiated a second one. The first 2-live episode survived; the second --
after the cutscene client was released and another opened -- trapped inside
Apple's own code:

```
[task-exc] BREAKPOINT pc=0x2b7f2d684 insn=0xd4200020        (brk #1)
[task-exc] TRAP-SYM  caulk.framework`caulk::thread::start+0x1e0
[int3-guest] guest_rip=0x0  GUEST RAX=0 RCX=0 RSP=0 RBP=0   (no guest context)
err:seh:NtRaiseException Unhandled exception code c000001d
err:process:NtTerminateProcess exit_code=0xc000001d
```

Our handler turned an Apple-framework `brk` on a non-guest thread into a guest
`c000001d`; the game had no handler and self-terminated. `abort_process` then
tore down only the pseudo-process, leaving 8 orphaned `BinkAsy` threads parked
forever -- which presented as a HANG, not a crash (thread count 79 -> 37 -> 36).

Byte-identical in rdr91 and rdr93 (same pc, same instruction, same symbol, same
exit code, same fb=4 -> fb=8 -> RELEASE -> fb=8 sequence). The control is clean:

| run | 2-live episodes | stream creates | caulk trap |
|---|---|---|---|
| rdr88/89/90 | 1 | 2 | 0 |
| rdr91, rdr93 | 2 | 3 | 1 |

The trap appears ONLY in the runs that reached a third stream creation -- which
are exactly the two runs that reached the end of the loading bar.

Fix: one endpoint created once and kept for the process lifetime (which also
removes the create/release/create churn), float32 canonical format, and a
render callback that walks a lock-free `g_mix[]` table and sums every live ring,
converting int16/int32/float32 and mono/stereo, clamping the sum, and respecting
Core Audio's actual buffer size. Detach clears the slot then waits two completed
callback epochs (bounded) before the stream can be freed.

rdr95 result: `caulk=0`, `c000001d=0`, `ml1026 MIX slot` fired **4x** -- four
clients on one endpoint. CONFIRMED FIXED.

**ml1027 — explicit placement after the tag ladder fails.** `virtual_ios.c`,
`anon_mmap_alloc`.

We made five kernel-chosen attempts with five allocation tags (ml902 + ml815 +
ml1025) but never one explicit placement. This kernel picks a range by tag AND
size, so all five can be confined to full ranges while space exists elsewhere.
ml1027 walks the real map, picks the SMALLEST gap that fits (best-fit -- never
bisect the largest hole to satisfy a small request), includes the tail above the
highest mapping, and places with the no-clobber `anon_mmap_tryfixed`.

rdr98 result: the 692MB allocation that had blocked every run since rdr92 now
succeeds and the game reaches the LOADING SCREEN, renders, and flashes a frame.
CONFIRMED FIXED.

### 49.2 Two of my own probes lied; both are now hardened

1. **ml1025's "genuine exhaustion" claim was unearned.** ml902 had changed the
   FIRST attempt to `VM_MEMORY_MALLOC`, which silently made ml815's "retry" a
   byte-identical repeat of it: three mmaps, two strategies. It then logged
   "genuine exhaustion of every range this allocation may use" having tried two
   ranges. Now it names exactly what it attempted.

2. **ml1027 v1 reported a verdict from a scan that never ran.** It omitted
   `raddr += rsize`, so `mach_vm_region_recurse` returned the same first region
   200,001 times:

   ```
   NO GAP FITS after 200001 regions (highest_mapping_end=0x102d10000 tail=60370 MB)
   ```

   `200001` is exactly the cap, and 4.34GB is absurd on a map whose regions run
   past 44G. The phantom 60GB "tail" was then correctly rejected because it would
   swallow the JIT pool, leaving no candidate and a confident false verdict.
   Fixed; the tail is now only considered when the walk REACHED the ceiling, and
   truncation prints `[TRUNCATED -- scan INCOMPLETE, NOT a verdict]` with
   `walk_stopped_at` and `gaps_seen`.

   **This is the fourth probe-lie in this project. `highest_mapping_end=4.34GB`
   should have stopped me before I reported it.**

### 49.3 REFUTED: madeira-totalphys is not a lever for this

I lowered reported physical memory 4096 -> 2048 -> 1024 MB believing RDR2 sized
its reservation from `ullTotalPhys` (ml991: "the 28GB reserve is 0x700000000 =
7 x 4096MB"). Measured:

| run | totalphys | layerkit-span occupancy | first failure |
|---|---|---|---|
| rdr94 | 4096 MB | 9043 MB | 692 MB |
| rdr95 | 2048 MB | -- | 64 KB (692MB SUCCEEDED) |
| rdr96 | 1024 MB | 9043 MB | 692 MB |

Occupancy is IDENTICAL at 4096 and 1024. ml995 already documents why: the
reservation is `max(7/8 x reported_phys, 8960 MB)` and 7/8 of all three values
is below the 8960 floor. rdr95's much better run was VARIANCE, not the knob --
the user said so before I did. Reset to 2048 (inert). **Do not spend more runs
on this knob.**

Also refuted: I claimed ~64GB of free VA above the GPU carveout at the CLEAR
`[slot#N]` bands. The probe says otherwise --
`[window] 0x7048000000..0x7400000000 OUTSIDE TASK MAP (ceiling 0xfc0000000) --
this is NOT free space`. Task max address is **63 GiB**; nothing above it is
usable, and `plan[]`'s own comment says so.

### 49.4 The CURRENT blocker, with identical ordering in rdr95 and rdr98

```
rdr98:
line   293-294   [jumbo-hold] ml997 granted 8960MB at 0x7b9e00000  (holdback)
line  8465-8469  a SECOND 8960MB reserve fails -- NOT fatal, game continues
line 76318       last [rpc-census]  60 flushes/553ms, 0.19ms mean  -- HEALTHY
line 76489       *** NSAllocateMemoryPages(2621440) failed
                 NSInvalidArgumentException, uncaught
line 76496       NtTerminateProcess exit_code=0x80000101 (pseudo-process 0020)
                 [srv-kill] kill_thread ... violent=1
line 76496+      ZERO rpc-census in the remaining 11,268 lines
host             [rmetald] client gone; releasing 25897 session handles
```

A **2.5 MB** Foundation page allocation fails; the uncaught ObjC exception kills
one pseudo-process; its teardown takes down the PROCESS-WIDE remote-Metal
socket; the render thread then spins in `_MTLCommandBuffer_commit` (`swtch_pri`
is the hottest sample in the whole run) -- FPS 0, hung, which is what the user
sees after the green flash.

The host had rendered **7800 frames / 19441 command buffers, gpu-errors=0,
blank=9 (0.12%)** up to that moment. The rendering path is basically working.

### 49.5 The open question — DO NOT guess at this, measure it

`[holes<64G] free=594 MB largest=349 MB`, and the now-trustworthy ml1027 scan
says `after 67474 regions, walk_stopped_at=0xfc0000000, tail=0 MB, gaps_seen=0`.
So regions span essentially 0 -> 63 GiB with only ~594 MB of gaps: **~62 GB is
mapped, and I can only account for ~21 GB of it**:

- 8960 MB granted guest reservation (PROT_NONE, at 0x7b9e00000)
- ~7389 MB PROT_NONE region seen in the window inventories
- 4096 MB FEX arena -- ml995 measured this at **0 MB resident**
- 1024 MB JIT pool (512 RX + 512 RW)
- guest images/DLLs/heaps, CoreAnimation spans

**What holds the other ~40 GB is UNKNOWN and is the next thing to establish.**
`[phys-map]`'s top-12 is ranked by DIRTY pages, so it structurally cannot see
large PROT_NONE reservations -- which is exactly what we are looking for.

Recommended next step: a by-SIZE VA census (one `mach_vm_region_recurse` walk,
accumulate bytes by `info.user_tag` and by protection, print the top N regions
by SIZE plus per-tag totals). This is a probe, not a fix, and it is the cheapest
way to stop guessing. `[cage] holdback reserve FAILED (errno 12)` in rdr98 is a
second symptom of the same exhaustion and a second reason to want this census.

### 49.6 Two real defects this exposed, independent of the VA work

Both are worth fixing on merit -- they turn a contained error into a whole-app
hang -- but neither is yet proven to be THE fix for reaching the menu:

1. **A pseudo-process teardown must not close fds it doesn't own.** Same class
   as the Explorer COM wedge (`project_explorer_com_wedge_start_menu.md`): the
   final fix there was fd ownership, not ordering. One dying helper should never
   take down the shared Metal transport. NOTE: the evidence here is ordering
   correlation (healthy RPC at 76318, death at 76496, zero RPC after) plus the
   host's "client gone" -- I did NOT find a logged close of the socket fd, and
   136 `[fdtrace]` lines exist. Confirm before building.

2. **The remote client must not spin on a dead socket.** A failed commit RPC
   should fail loudly or reconnect, not burn ~87% of a core forever in
   `_MTLCommandBuffer_commit`. Astra's rdr90 review already asked for a properly
   propagated failed-backend state rather than silently continuing.

### 49.7 Deployment state

`madeira-totalphys.txt` = 2048 (inert). ml1025 + ml1026 + ml1027 all built,
deployed to the VM bundle and content-verified on device. Nothing is committed;
the user requires a rundown and approval first.

Still open from earlier sections and untouched by this work: the
`Fragment input(s) user(texcoord4),user(texcoord7) mismatching vertex shader
output` pipeline warning, `rasterSampleCount (8) is not supported` (we correctly
report `NumQualityLevels=0`; the game asks anyway), the green-flash/black frame
itself, and one leaked encoder per run on the guest side.

Also worth noting for whoever touches the deploy script: the prefix's
`system32/d3d12.dll` symlink resolves to `Madeira.app/aarch64-windows/d3d12.dll`
(458752 bytes) while `deploy-vm.sh` pushes `arm64ec-windows/d3d12.dll` (302080).
`madeira_d3d12.dll` DOES resolve to the arm64ec copy we push, so the native
D3D12 runtime is current -- but the `d3d12.dll` entry in FILES is not reaching
the DLL that actually loads.

### 49.8 CORRECTION (Astra, 2026-09-19) + ml1028, the actual fix for the blocker

**§49.4/§49.5 above conflated two different allocations and drew a wrong
conclusion. Read this instead.**

The "NO GAP FITS ... the map really is full for this size" verdict belongs to an
**8960 MB** request (`size=0x230010000`), which the game SURVIVED -- it happens at
log line ~8465, sixty-eight thousand lines before the hang. It says nothing about
the failure that actually kills us. Near that failure the census reports
**free=609 MB with a 366 MB largest hole**. A **2.5 MB** allocation was failing
with 366 MB available, so the map was NOT exhausted, and my "~62 GB mapped, only
21 GB accounted for" line was built on that same conflation. The by-size VA census
in §49.5 is still worth having, but it is NOT the blocker and should not be
prioritised over this.

**The real defect, verified independently:**

```
[surf-flush] #48 surface=0x659da1ec0 hwnd=0x80054 rect={0,0,1024,640} dirty={0,0,968,572}
*** Terminating app due to uncaught exception 'NSInvalidArgumentException',
    reason: '*** NSAllocateMemoryPages(2621440) failed'
```

The `[surf-flush]` of the **1024x640** window surface is the IMMEDIATELY
PRECEDING line, and `1024 * 640 * 4 = 2,621,440` exactly. Same adjacency in
rdr95 and rdr98.

`Winios.m:959` (`winios_surface_present`) copied the window's DIB with
`[NSData dataWithBytes:bits length:stride * sh]`. That allocator is
`NSAllocateMemoryPages`, which **throws and cannot return nil**. The uncaught
ObjC exception killed the pseudo-process; its teardown took down the
process-wide remote-Metal socket; the render thread then spun in
`_MTLCommandBuffer_commit` forever. A 2.5 MB compositor copy became a whole-app
hang.

**ml1028** (`Winios.m` + `build/win32u-unix/driver_ios.c`):

- `winios_surface_present` is now `int`, not `void`.
- The snapshot is `malloc` + `memcpy`, ownership handed to
  `[NSData dataWithBytesNoCopy:length:freeWhenDone:YES]`. `free()` matches
  `malloc`, and `CGDataProviderCreateWithCFData` retains the CFData, so the
  bytes live as long as any CGImage built from them.
- On allocation failure it logs `[surf-snap] ml1028 #N ALLOC FAILED` (rate
  limited) and returns 0.
- `winios_surface_flush` propagates that as FALSE.

Why FALSE is the correct failure signal -- verified in `wine/dlls/win32u/dce.c:637`:

```c
if (surface->funcs->flush( ... ))
    reset_bounds( &surface->bounds );
```

The bounds are only reset on TRUE, so returning FALSE **preserves the dirty
region** and the frame is repainted on a later flush. No lost damage, nothing
dies.

⚠️ **Do NOT "optimise" this into a no-copy wrapper around Wine's `bits`.** The
presentation happens in a `dispatch_async` block on the main queue, i.e. AFTER
`winios_surface_present` has returned, and `bits` is only valid for the duration
of the call. The snapshot must own its bytes. (The block is `dispatch_block_t`
and must stay void -- its early-out is `return;`, while the function's own
result is 1, because the copy already succeeded and only presentation is
skipped.)

Astra tested this ownership shape offline under ASan/UBSan: allocation-failure
handling, source mutation, image lifetime and 100 alloc/release cycles passed.
Device placement was still unverified at the time of writing.

Status: built, deployed to the VM bundle, content-verified on device
(`ml1028` + `surf-snap` present in `Madeira.debug.dylib`). Not committed.

Acceptance for the next run: no `NSAllocateMemoryPages` / `NSInvalidArgumentException`
/ `exit_code=0x80000101`; `[rpc-census]` continuing to the end of the log
(transport alive); and if `[surf-snap] ml1028 ALLOC FAILED` appears, that is the
graceful path working -- the run should continue rather than hang.

## §50 — rdr100/rdr101: RDR2 reaches its FIRST-BOOT UI and is interactive. The black screen is the G-BUFFER, and it is down to 4 pipelines

rdr100 = first interactive run (an "enter" prompt rendered bottom-right, user
pressed it). rdr101 = same with ml1030. Logs in the scratchpad.

### 50.1 Where we are

The game boots, loads, and runs its first-boot flow at 50-60 FPS. Host, rdr101:

```
[rmetald] frames=46200 blank=20950 (45.35%) draws/frame=20 | cmdbufs completed=103113 gpu-errors=0
```

46,200 frames, 103,113 command buffers, **zero GPU errors**. `blank` is FROZEN at
20950 while frames climb, i.e. the early frames were blank and recent ones carry
content -- the visible "enter" icon. Guest-side: **15,589 root signatures parsed**
and thousands of VS/PS/CS converted at runtime. Memory is comfortable: 2009 MB
steady, **peak 2542 MB** against the 4096 MB jetsam ceiling, flat for 1,200+
cycles.

What got us here, all four device-confirmed: ml1026 (one RemoteIO endpoint),
ml1027 (explicit mmap placement), ml1028 (checked surface snapshot, Astra),
ml1029 (holdback carving), ml1030 (sample-count clamp).

### 50.2 ml1029 — the "variance" was OURS, and it was ordering

The `exited unexpectedly` dialog was never random. The guest makes TWO large
requests during startup -- one **8960 MB** reserve and one **~692 MB** commit --
and `[jumbo-hold]` pre-reserves a single hole for the big one:

- **rdr98**: big ask arrived FIRST -> `ml996 releasing the holdback ... for a
  8960 MB request` -> holdback consumed -> the 692 MB ask then found space ->
  reached the loading screen.
- **rdr99**: the 692 MB ask arrived FIRST -> **no releasing line in the whole
  log** -> our own 9216 MB holdback was still held, which is exactly why
  `ml1027` reported `gaps_seen=0` -> dead.

We were starving a request happening NOW to protect one that might never come.
Both fit: 8960 + 692 = 9652 MB inside the 9907 MB hole the holdback is cut from.
`take()` hands out the BOTTOM (it returns `base`), so ml1029 carves off the TOP
and they cannot collide. Keep floor is config (`madeira-jumbo-keep-mb.txt`) and
defaults to keep-everything, so nothing changes for Thumper/ULTRAKILL/Stray
unless enabled. VM is set to `jumbo-mb=9856`, `jumbo-keep-mb=8960`.

### 50.3 ml1030 — we contradicted ourselves about MSAA, and it cost 28 pipelines

`device_CheckFeatureSupport` correctly reported `NumQualityLevels=0` for 8x
(Apple GPUs top out at 4x) and then handed the application's `SampleDesc.Count`
of 8 straight to `MTLRenderPipelineDescriptor.rasterSampleCount` anyway:

```
[rmetald] newRenderPipelineState: rasterSampleCount (8) is not supported by device.   x28
```

A rejected pipeline is a NULL pipeline and every draw through it silently
disappears. D3D12 permits failing pipeline creation, but a translation layer that
cannot do 8x should DEGRADE the AA, not drop the geometry. `mad_clamp_sample_count()`
now drives the capability report, the pipeline descriptor AND texture creation, so
they can never disagree again (a 4x pipeline against an 8x render target is its
own validation failure).

Measured effect: rasterSampleCount rejections **28 -> 0**, and `draws/frame`
**1 -> 20 sustained**.

### 50.4 THE REMAINING BLOCKER — 4 G-buffer pipelines, one defect class

```
[madeira-d3d12] newRenderPipelineState failed (5 targets, depth 20, 9 input elements)
[madeira-d3d12] newRenderPipelineState failed (5 targets, depth 20, 4 input elements)  x3
```

**5 render targets + depth = RDR2's deferred G-buffer.** These are the MAIN SCENE
pipelines, not UI/text -- which is why the screen is black while the "enter" icon
(1 target, no depth) draws fine. The host names the cause:

```
newRenderPipelineState: Fragment input(s) `user(texcoord4),user(texcoord7)`
  mismatching vertex shader output type(s) or not written by vertex shader      x3
newRenderPipelineState: Fragment input(s) `user(color1),user(color2),
  user(texcoord7),user(texcoord14)` mismatching ...                             x1
```

The PS reads interpolants the VS never writes. **D3D allows this** (the values are
undefined); **Metal requires an exact stage_out/stage_in match.** So this is our
converter's problem, not the game's.

**It is OUR converter.** Confirmed: the entry names are `mdc_<fnv1a>`, emitted by
`mad_air_entry_name()` inside `mad_airconv_convert()` -- so RDR2's SM5.1 shaders
go through the in-tree **airconv**, not Apple's DXIL-only MSC. We generate the IR,
so we can fix this.

**Design (not implemented -- no confident fix this turn):**

1. Extract the VS **output** signature register/component mask. airconv already
   parses both signatures (`dxbc_converter.cpp:1042-1048`,
   `CSignatureParser inputParser` / `CSignatureParser5 outputParser`), and
   `madeira_sm5_ia.cpp` already demonstrates driving DXBCParser from our side for
   the ISGN, so the machinery exists in both places.
2. Plumb a "provided interpolant mask" from the PSO (which has both shaders) to
   the PS conversion: a new field in `madeira_ir_abi.h` plus either a new
   `SM50_SHADER_COMPILATION_ARGUMENT_TYPE` or an extra field on the existing
   `SM50_SHADER_PSO_PIXEL_SHADER` data (ml1023 already chains that struct).
3. In airconv's PS input declaration, for every declared input the VS does not
   provide: do NOT emit it in stage_in, and initialise that register to zero
   instead. Zero-filling in the PS is preferable to synthesising VS outputs --
   the VS is shared across PSOs and would otherwise need a variant per PS.
4. The shader cache key must include the mask, or a PS compiled against one VS
   will be reused against another. `mad_sc_hash_add` already covers the resolved
   IA elements and the four ps_* values; add the mask there.

⚠️ Do NOT "fix" this by relaxing anything host-side: the host is Metal and the
rejection is correct. The mismatch must be resolved at conversion time.

### 50.5 SECOND remaining gap — tessellation pipelines dropped

```
[madeira-d3d12] pipeline carries HS DS (N/N/N bytes) which this runtime DROPS;   x12
```

12 pipelines with hull/domain shaders are discarded outright. `SM50_SHADER_PSO_TESSELLATOR`
exists in airconv's argument enum, so the converter side may already have support
worth wiring up. Separate, larger piece of work than 50.4; likely terrain/water.

### 50.6 Build-chain trap that cost two misleading deploys

`build/madeira-d3d12/build-pe.sh` writes `madeira_d3d12.dll` to
`build/madeira-d3d12/out-pe/` and does **NOT** copy into
`app/Madeira/arm64ec-windows/`. `scripts/deploy-vm.sh` takes its FILES from the
**Xcode DerivedData** bundle. So after editing `madeira_d3d12.c` the sequence is:

    build-pe.sh  ->  cp out-pe/{madeira_d3d12,d3d12}.dll app/Madeira/arm64ec-windows/  ->  xcodebuild  ->  deploy-vm.sh

Skipping the middle steps makes deploy-vm.sh print `same ... (already deployed)`,
which is TRUE of the stale file and completely misleading. Worth making
build-pe.sh stage into `app/` itself.

Also still open from §49: the prefix's `system32/d3d12.dll` resolves to
`aarch64-windows/d3d12.dll` (458752 bytes) while we push the arm64ec copy
(302080). `madeira_d3d12.dll` DOES resolve to what we push, so the runtime is
current, but that FILES entry is not reaching the DLL that loads.

### 50.7 Status

ml1026/1027/1028/1029/1030 all deployed to the VM bundle and content-verified on
device. **Nothing is committed** -- six changes now stack up awaiting a rundown
and approval. Config on the VM: `totalphys=2048` (inert), `jumbo-mb=9856`,
`jumbo-keep-mb=8960`.

## §51 — iPhone 18 Pro (2026-09-20/21): menu + benchmark reached on LOCAL Metal; the open wall is a write-permission fault on ordinary guest data

**Development target is now the iPhone 18 Pro** (A20 Pro, 12GB, iOS 27, local Metal,
bundle `com.willfaust.madeora`, UDID redacted). The same build
that showed one icon through rmetald renders RDR2's splash video, full menu and
settings on the phone => **rmetald is the inaccurate layer**, not the D3D12 runtime.
Logs: scratchpad `ph-rdr01..15.txt`. Game Mode is on (Info.plist
`LSApplicationCategoryType=public.app-category.games`, `GCSupportsGameMode`); the
process limit reads 8192MB via `os_proc_available_memory()+phys_footprint`.

### 51.1 Fixed on the phone, each confirmed by a later log

| ml | defect | evidence it held |
|---|---|---|
| 1034 | exe window `[0x140000000,+)` reserved AFTER the RX pool, so the pool swallowed it; RDR2 has BASERELOC size 0 and was displaced with stale absolute pointers (TLS callbacks `0x1432BA978`) | boots at `0x140000000` every run since |
| 1035 | `IosMigrateLock` non-recursive spinlock held across `StartLargerCodeBuffer()`; a fault inside re-entered via `IsAddressInCodeBuffer` => thread waits for itself, 100% CPU, JIT write lock held, `[fexlock] STUCK` x17 | 0 STUCK in every run since |
| 1036/1037/1040 | the phone's low gap (~1.4GB) must hold BOTH the window and the debugger-placed RX pool; our own runtime allocations fragment it per launch. Hole census + adaptive pool size; window 256->128MB; RW alias hint moved high; a **constructor** claims the window and the run above it at image load | pool 624MB at `0x148000000`, deterministic |
| 1038 | we advertised `UMA=CacheCoherentUMA=TRUE`; RAGE then Map()s TEXTURES in CUSTOM heaps (all 625 heaps were type 4). Runtime textures are GPU-private, Map refused x39, HRESULT ignored => white images, then `memset(NULL,0,0x80000)` | UMA=FALSE: Map REFUSED 39->0, loading-screen photos render |
| 1039 | JIT pool tail refused while >100MB of RECLAIMED head sat free (launcher generation copies the 117MB image then exits; freed ranges only fed the image allocator) | not needed at 624MB, untested |
| 1041 | `[bigres]` probe read 8KB up the syscall stack with no bound -> AV inside NtAllocateVirtualMemory -> "exited unexpectedly", only when the next page was unmapped | bounded by `mach_vm_region_recurse` |
| 1042 | DXGI video budget = Metal's `recommendedMaxWorkingSetSize` (~all of RAM); RAGE budgets RAM and VRAM separately => 8186MB, jetsam. Budget now `limit - 4096 - 2560`, floor 1GB (`madeira-vram-mb.txt` overrides) | 6.5-6.9GB flat |
| **1046** | **x18 is transiently ZERO on iOS** (kernel clobbers it, repaired lazily on the next TEB-relative LOAD; `x18_fixes`~2500/run). rpmalloc used the raw register as thread id: `heap_lock_acquire` did `CAS(lock,0->0)` = no lock; owner stamped 0; `owner==id` was `0==0` for any thread. Now `TEB->Self` via `ldr [x18,#0x30]` (volatile) | `RPMALLOC-REPAIR` 45 -> 0, no FEX-heap crashes since. Candidate root cause of the ml606/ml610 container-corruption family |
| 1047 | SEGV dump budget was "first 20"; 16 went to ONE routine pc. Now 2 per distinct pc | — |

Traps recorded: never call `getenv`/CRT from an allocator entry point (ml1044
recursed ucrtbase off the stack at early thread init, boot hang); `build-pe.sh`
does not stage into `app/` and `deploy`/install take files from Xcode DerivedData;
never `pkill CoreDeviceService` mid-session; NEVER uninstall the app (the data
container holds the 117GB game).

### 51.2 THE OPEN WALL (deterministic, 2 runs running): ph-rdr14, ph-rdr15

```
D 94 Exception: Code: C0000005 Address: 14DC6AD84 AV WRITE of 17E3E908C
D 94 pc: 14DC6AD84 rip: 14063B7D6
guest:  14063b7d6  movq %rdi, 0x68(%rax)      rax=0x17e3e9024
        rax = game bump allocator 0x142ba100c(size 0x84): lock xadd 32-bit offset, addq [base]
-> crack VEH 0x13037C00 "recovers" to stub 0x13037CE0 -> RIP=0 storm -> ml465 terminates
```

Facts, all measured:
- The target is **ordinary anonymous guest DATA**, not the image: the log holds 45
  distinct successful store targets `0x17ac4332c..0x17e435d7c` in that arena
  (`UNALIGNED-BACKPATCH ... kind=STLR`, `[unaligned-guest] emulated`), i.e. the
  kernel placed a Wine allocation in the low hole above the pool. Neighbouring
  regions there are `tag=100` (IOAccelerator), so a mapped D3D12 buffer is also
  possible -- NOT established.
- It reaches FEX as an **AV WRITE with no "unreadable target" line**, so the page
  was READABLE: `bus_handler` classified it by ESR (`(esr&0x3f)!=0x21` => data
  permission fault, `[bus-data-av]`). `virtual_handle_fault` was tried first
  (ml420) and declined. FEX's SMC tracker did not claim it ("Passing through").
- Every diagnostic budget on that path (`[bus-data-av]` 16, `[wr-strip]`,
  `[wr-strip-declined]` 12) is exhausted within seconds by the crack's
  self-modifying writes into `[0x140000000,0x148000000)`, so NOTHING was ever
  logged about the arena page. That is why two runs produced no attribution.
- The stores are 8-byte `movq` to 4-byte-aligned addresses (rax ends ...4/...C).

**ml1048 (installed, unrun):** for permission faults whose target is OUTSIDE the
image range, log ESR (fsc, WnR), the kernel region (prot, max_prot, share_mode,
user_tag, resident/dirtied/ref/shadow/pager), Wine's expected unix prot, and
`ios_page_vprot_explain()`. Reading it:
- `prot` lacks W, `max` has W  => someone mprotect()ed it (FEX SMC tracking of a
  4K code subpage sharing a 16K HOST page with data is the prime suspect: FEX
  tracks 4K intervals, the host protects 16K, and a store to a sibling subpage is
  "not my interval").
- `max` lacks W => mapped that way (view/placement bug).
- Wine expects W and host lacks it => extend the existing `[wr-strip]` heal to it.

Hypotheses NOT yet supported: 64->32 truncation of the arena base (the address
decomposes as low32(0x73ffff0000)+off, but off~2GB is implausible for this scratch
arena and every writer of the base table is a clean 64-bit store -- likely
coincidence).

### 51.3 Still open behind it
- 12 pipelines carrying HS/DS are DROPPED (tessellation); the 4 G-buffer PSO
  failures seen on the VM are those -- the PS expects DS outputs. ml1031/1033
  zero-filled 0 shaders on the phone. Needs real tessellation, not zero-fill.
- Benchmark frames so far: ~7500 draws/600 lists, largest list 17 draws, scene
  black at ~2 FPS before the memory fix; not re-observed since.
- JIT pool holds RDR2's .text more than once (240MB of 406MB image head).
- The allocator lock (ml1044/45) is still in; remove once ml1046 is proven.

### 51.4 Remaining raw-x18 identity reads in FEX (audited, NOT changed)
`CPUBackend.cpp:362` (`IosMigrateLockGuard::Me`), `CPUBackend.cpp:418` (log only),
`JIT.cpp:1233` (CodeBufferWriteMutex self-nesting test). All three read the register
directly, so under the transient zero they see 0. Unlike rpmalloc they fail toward
a MISSED re-entrancy grant (self-park), not toward a false ownership match: `Me()`
returns 1 and is explicitly barred from claiming re-entry, and JIT.cpp skips the
test when the value is 0. No `[fexlock] STUCK` since ml1035, so left alone under the
one-variable rule; the correct form is the ml1046 one (`ldr [x18,#0x30]`).

## §52 — ml1049 (2026-09-21): SM5 binding correctness + command-buffer status + autorelease scope

ph-rdr17 reached the BENCHMARK again (black, ran long, jetsam at 8184MB). The
§51.2 arena fault did NOT recur and ml1048 logged nothing -- still unattributed.
Astra's review (`RDR2-PHONE-BENCHMARK-REVIEW-2026-09-21.md`) verified against
source; all three binding defects were real. PE runtime only (`madeira_d3d12.c`),
no shader-cache invalidation.

- **Typed-buffer views**: `mad_air_texbuf` walked `srv_res`/`uav_res`, which hold
  only TEXTURES (the buffer branch of both view creators returns before the
  insert) => every typed-buffer binding failed and its draw/dispatch was skipped.
  Now a device-wide id->(count,first) map (`vmap`, open addressing, `view_lock`),
  filled at view creation, emptied at resource release. ASan/UBSan stress-tested
  offline (1.2M put/del/get, 0 errors). Texture views register too (array length).
- **View eviction**: `tview[16]` / `xview[8]` rings released Metal views that
  descriptors still named. Now growable, never evicted; outgrown arrays are kept
  until the resource dies so an unlocked reader cannot touch freed memory. A
  census line fires at 64/256/1024/... views per resource (unbounded growth
  would be its own defect -- measure, don't guess).
- **Shader visibility**: `mad_air_resolve` took the FIRST param matching
  type/register/space. Now filtered by stage (fragment: ALL|PIXEL; vertex:
  ALL|VERTEX|GEOMETRY; compute: all). If the filter alone loses a binding it
  retries unfiltered and COUNTS it (`visibility fallbacks`).
- **Null CBV**: a null descriptor in a bound table skipped the whole draw. D3D12
  only requires validity if the shader reads it; now binds the 64KB zero buffer
  (same rule the DXIL path already applied at :1229), counted. CreateConstantBufferView
  with a null desc writes that descriptor directly.
- **Command-buffer status** was never read: `mad_cb_retire` now checks
  `MTLCommandBufferStatusError` and logs the NSError description (24 max) plus a
  cumulative count. A failed buffer discards a whole 48-list batch.
- **Skip census**: every `skipped++` site is counted by source line, every bind
  failure by reason; printed every 3000 lists (`ml1049 skips by site`, `ml1049
  bind failure xN`).
- **Autorelease**: pool around `queue_ExecuteCommandLists` (encoders all end in
  `exec_end`; the open command buffer holds its own retain). NSError out-params
  are BORROWED from the bridge: all 7 `NSObject_release(err)` removed, text logged.
  ⚠️ NOT done: `swap_Present` -- it releases a +0 drawable, which is only correct
  because no pool drains; adding a pool there REQUIRES removing that release.
  Residency-set removal and placed-resource backing are untouched.

## §53 — ph-rdr18 (ml1049 result) and ml1050

**ml1049 result, measured:** benchmark no longer black. `skips by site: none`, zero
bind failures, `65000 command buffers retired, 0 ENDED IN ERROR`, null-CBV 0,
visibility fallbacks 0 (so the b22 "address zero" failures were the stage bug, not
null views). ~26k draws / 600 lists, largest list 548. Screen = light-grey field of
huge overlapping triangles. Jetsam again at 8184MB.

**Reading of the image (hypothesis, ml1050 tests it):** the benchmark opens on snow
terrain, which RAGE tessellates (topology 35 logged). A tessellation pipeline's VS
emits control points in object/world space; the DS projects. We drop HS/DS and
rasterise VS output as clip space => screen-filling garbage triangles over
everything. The "12 pipelines" figure is a log cap, not a census.

**Memory, measured across the benchmark (cycle 475 -> 1255):** tag100 (GPU) 982 ->
1742 MB and still climbing vs a 1536 MB advertised budget; FEX band 316 -> 804 MB
(JIT metadata, separate problem); guest band 2677 -> 3679. The ExecuteCommandLists
autorelease pool alone did not stop it.

**ml1050:**
- `MTLResidencySet_removeAllocation` added to the winemetal bridge (unix call 134,
  appended; PE thunk + unix + wow64 stub + api names). `mad_unresident()` on every
  buffer, texture, typed view and texture view at resource release; lands at the
  next commit. The set retains members and nothing ever left it.
- Tessellation pipelines flagged (`has_tess`); their draws begin the pass (clears
  stay real) and are DROPPED, counted. This is an isolation test and strictly
  better than garbage; real fix = DXMT's airconv tessellation path (Astra §4).
- Report line: `ml1050 residency set: A added, R removed (M members);
  tessellation: P pipelines, D draws dropped`.
- `build/dxmt-ios/build.sh` gained `MADEIRA_ONLY=<object>` so a bridge change does
  not recompile `madeira_ir_unix.mm` (its __DATE__ stamp keys the shader cache).
- Perf (user: ~1-5 FPS) deliberately deferred; known costs: 256 useResource per
  draw, O(n) view scans, per-pipeline root-signature reparse (15.5k log lines).

## §54 — ph-rdr19 (ml1050 result): FIRST REAL 3D SCENERY; residency fix confirmed; a hang; perf facts

**Result (user-observed + measured):** the benchmark rendered recognisable snow
scenery ("very recognizable", some glitches). 0 command-buffer errors of ~70k, 0
bind failures. Residency: 29,564 added / 10,435 removed; footprint PEAK 7287 MB
(HUD 7.6 GB) where the previous two runs hit 8184 and jetsam'd -> ml1050's
removeAllocation is confirmed as a real leak fix. 25 tessellation pipelines, 585
tessellated draws dropped (the first half of the run had 0 drops and already
rendered correctly, so tessellation does NOT explain the ml1049 "triangle mess";
that remains unexplained -- do not claim it).

**Hang (unattributed):** ~1 FPS, then 0. App alive, game dead for ~15 min until
relaunch. Final state: exactly ONE guest thread runnable, in the game's own yield
spin (`swtch_pri` <- `cthread_yield`, stack candidates RDR2.exe+0x25a51e4 /
+0x53a4058, ~40% CPU); 4 workers parked INFINITE on the same handle 0x183 since the
moment of the hang, 1 on handle 0x1; `[alert-ring]` frozen; no thread blocked in
Metal; no `[fexlock] STUCK`; last exception (thread 00a0) was the crack's routine
ud2 pass-through. Shape = main thread waiting on work that parked workers will
never do => lost wakeup / dead producer. User had just attached QuickTime screen
capture over USB -- coincidence not excluded. NOTE: a pull while the app is alive
is truncated to a multiple of 20,000,000 bytes; the full run was in
`madeira-log.prev.txt` (46 MB).

**Perf facts from this run (measured, not inferred):**
- FEX `[CB_SUMMARY] total=12.5M real_compiles=5.5M hit_rate=56%`. 392 code-buffer
  FREE / 388 REUSE, `exec alloc degraded 0x2000000 -> 0x1000000 (pool pressure)`,
  `[callret-gen] gen=389 resets=244`. The JIT is THRASHING: code buffers fill, are
  discarded, and the same code is retranslated. ~24% of RUNNING samples were inside
  FEX's compiler vs ~9% in translated game code. Root: a 624 MB pool whose head is
  ~406 MB of PE image copies (RDR2 .text several times), leaving little tail.
- ~12% of running samples = Swift string handling in our log pipeline; ~10% Mach
  exception handling; ~6% spinning in `fex_ios_rpm_lock` (ml1044 diagnostic lock;
  RPMALLOC-REPAIR=0 for 4 runs, removable).
- The D3D12 runtime did not appear in the samples at all. Per-draw overhead is NOT
  the current bottleneck (my earlier assumption was unverified and wrong).

**ml1051 (installed):** log diet only. `[iat-sync]`/`[x86-ptr]` (165k lines) first
64 then 1/2000; `ml1002` caller dumps (117k lines) first 24 then 1/2000; the
NtUserActivateKeyboardLayout FIXME (45k lines) once. No behaviour change, no shader
recompile.

## §55 — ml1052 (built, NOT yet installed): stop the JIT code-cache thrash

User direction 2026-09-21: graphics are "very clearly rendered" (texture glitches,
some invisible snow ground); PERFORMANCE FIRST, target an order of magnitude, so
in-game testing is possible. Glitches and tessellation are parked behind it.

Mechanism, read from source + ph-rdr19: FEX keeps ONE shared code buffer per
process (`CodeBufferManager::Latest`); when it fills, a new one is allocated and
every thread's translations are discarded. iOS capped it at 32 MB
(`MAX_CODE_SIZE`), and ntdll's tail allocator capped grants at 16 MB once the tail
passed 160 MB (ml480) and at pool/2 overall. RDR2's hot code does not fit: 390
rotations, 5.5M compiles for 12.5M lookups. Separately the launcher generation's
117 MB fixed-base image was freed 3 s-grace too late for the real process to reuse,
so the head bumped to ~357 MB of a 592 MB pool and squeezed the tail to ~235 MB.

Changes (one theme):
- `virtual_ios.c ios_pool_alloc_range_ex`: a >=32 MB request that an in-grace freed
  range could serve WAITS out the grace (<=4 s, logged `ml1052 waited`) instead of
  bumping. Grace guarantee unchanged.
- tail cap: budget-aware, up to 128 MB while 48 MB stays free between head and
  tail; a free carve of the asked size always bypasses the cap; tail limit
  pool/2 -> 3/4 (collision check still authoritative).
- FEX: `MAX_CODE_SIZE` 32 -> 128 MB on iOS; growth goes 16 MB -> MAX directly
  (carves never coalesce; a doubling ladder strands 112 MB). Refusal still halves.
Acceptance: `[CB_SUMMARY] hit_rate` well above 56 %, rotations (`tail FREE`) far
below 390, `exec alloc degraded` rare, FPS. Risk: a 128 MB generation pinned by a
parked thread (ml460 sweeper exists for this); watch `TAIL REFUSED`.

## §56 — ph-rdr20 (ml1051): the FPS variance is the JIT cap cliff, not logging

User: four benchmark scenes rendered mostly correctly; 3-5 FPS in snow (was ~1),
12-13 FPS in the last scene; jetsam at 8190 MB. No hang (no QuickTime this time --
one data point, not proof).

| | ph-rdr19 | ph-rdr20 |
|---|---|---|
| FEX hit rate | 56 % | 94 % |
| real compiles | 5.5 M | 0.62 M |
| code-buffer rotations | 392 | 15 |
| `exec alloc degraded` | 390 | 0 |
| FEX band dirty | ~830 MB | ~320 MB |

Same build for JIT purposes, same pool layout (560-592 MB, identical image
offsets), both granted 32 MB buffers at first. The runs diverge at footprint cycle
~330: rdr19's `tail_resv` crossed ml480's 160 MB watermark, the cap fell to 16 MB
and NEVER recovers (`tail_resv` only grows) => permanent thrash; rdr20 stayed at
144 MB and kept 32 MB buffers. The system is BISTABLE on a watermark. Logging
reduction is NOT the explanation (the compile counts differ 9x). ml1052 removes
the cliff (budget-aware cap, 128 MB) -- installed after this run.

With the thrash gone the profile (172 RUN samples, last third) is: mach_msg 16 %,
wineserver `read` 13 %, madeira_d3d12.dll 10.5 %, game yield spin 10.5 %, Swift log
pipeline ~11 %, guest code 7 %. Inside the D3D12 DLL 34 of 36 samples are
`mad_texture_of_view`: base textures were never put in ml1049's view map, so
every sm5 texture binding fell back to the O(resources x views) walk.
**ml1053 (built, not installed):** base textures registered/removed in the map;
root-signature parse line (15.6k/run) capped at 24; ml992 clamp line (4k) at 3.

Memory at the kill: tag100 (GPU) 2.45 GB, tag0 4.46 GB, compressed 3.07 GB.
QueryVideoMemoryInfo already reports Budget 1536 MB (ml1042 sits under
recommendedMaxWorkingSetSize) and a truthful CurrentUsage of ~2.4 GB -- the game
runs over its budget anyway, i.e. this scene set needs more than we advertise.
Residency: 60,461 added / 27,730 removed. 17,748 tessellated draws dropped across
27 pipelines (the missing snow ground). 0 of 97,579 command buffers in error.

## §57 — ph-rdr22 (ml1052+1053+1054): ~10 FPS; the next wall is 18,000 Mach exceptions/s

User: ~10 FPS through the first four benchmark scenes, ~6 in the fifth (gameplay),
geometry popping in/out there; jetsam at the end (8180 MB; tag100 2.49 GB, tag0
4.45 GB). Direction: PERFORMANCE FIRST, target 60 FPS as the aggressive goal;
rendering bugs after. The ph-rdr21 early native crash did not recur (1 run); ml1054
native backtrace is armed for when it does.

Confirmed by measurement:
- ml1052: hit rate **98 %**, 270k compiles for 16.6M lookups, **1** rotation, 0
  degraded, 0 refused; one 128 MB generation, 5 threads migrated, old 16 MB freed.
  Launcher image range reused (`waited 2100 ms`), head 296 MB (was 357).
- ml1053: `madeira_d3d12.dll` is no longer in the running-sample profile at all.

Profile now (274 RUN samples, second half of run): mach_msg 19.7 %, wineserver
`read` 15.7 % (32 of those are the SERVER thread in read_request), game yield spin
11.7 %, translated guest code ~15 %, Swift log pipeline ~11 %.

**The exception wall:** `[subfloor] ml956 serviced 12,029,953 access(es) via=mach`
in an 11-minute run = ~18,000 Mach exceptions per second, every one a full
kernel->handler-thread round trip that stalls the faulting guest thread. 2934 of
2938 sampled targets are the crack DLL's window (guest 0x13017xxx / 0x13105xxx),
byte and word LDAPR loads, six instruction encodings = two thirds of samples: a
bytecode interpreter reading its own image through sub-4GB absolute addresses
that iOS cannot map. Same class as the ULTRAKILL SWPAL storm (40k/s -> fixed ->
60 FPS). Candidate fixes, to be chosen from data:
  (a) if the ACCESSOR is code that itself lives in the window, FEX knows that at
      compile time (Frontend already calls IosSubfloorToReal): emit
      `addr - pref_base < size ? addr + delta : addr` on memory operands of those
      blocks only -- zero cost to the game's own code;
  (b) otherwise per-site backpatch to a thunk (flag-free range check + delta),
      like UNALIGNED-BACKPATCH.
**ml1055 (installed)** = measurement + log diet, no behaviour change:
- `[subfloor] ml1055 accessor guest-RIP pages` histogram every ~1M hits;
- `[srv-req] ml1055 last 200000 requests in N s: reqK=count...` (numbers index
  wine/server/request_trace.h req_names[]) to size an in-process wait fast path;
- FEX: AV-write "Exception:" line deferred -- sampled 1/1000 when the fault is
  handled SMC (21.6k per run), printed in FULL for every other outcome;
  `BUS at pc` sampled likewise. (`[rtcs] pre` lives in the EC PE ntdll; left.)
Not yet done: ml1044 allocator lock removal; pool-warmer RSS; tessellation.

## §58 — ml1056: the early "after window creation" crash = over-released drawable

ph-rdr21 and ph-rdr23 (and plausibly the session's earlier intermittent early
crashes): both native, both immediately after `[madeira-d3d12] Present #1`.
- rdr21: MAIN thread, `objc_retain_x8`, isa-derived class pointer NULL.
- rdr23 (ml1054 backtrace): dispatch worker, `AGXMetalG19P` block ->
  libdispatch with x0 = NULL (fault addr 0x50), under `_pthread_wqthread`.
Different victims, same culprit class: a Metal/CA object freed while still in use.
`swap_Present` called `NSObject_release(drawable)` on the +0 autoreleased result
of `-[CAMetalLayer nextDrawable]`. With no pool on the thread this stole the
pool's reference, so the drawable died as soon as the present block dropped its
own -- racing the driver's completion blocks and CoreAnimation. Timing-dependent
(came and went with unrelated builds; more frequent once the JIT got faster).
Fix: `swap_Present` = scoped NSAutoreleasePool around `swap_Present_inner`, no
manual release (also stops leaking one present command buffer per frame). The
unused `MadeiraD3D12Presenter` path held a +0 drawable across calls: now retains
at acquire. CONFIDENCE: mechanism is source-proven and fits both crashes; that it
is the ONLY cause is not proven -- ml1054's backtrace stays armed.

## §59 — ph-rdr24 (ml1056) data and ml1057: inline sub-floor translation

ml1056 booted and reached the benchmark (drawable-ownership fix: 1 clean boot,
not yet proof). Jetsam at 8189 MB again; user: texture quality already LOW, wants
another 0.5-1 GB found.

- `[srv-req]`: ~18-19k wineserver requests/s, ~78 % req29 and ~16 % req31 (names:
  see below) -- waits/selects dominate, i.e. the in-process sync fast path IS the
  next lever after exceptions.
- ml1055's accessor histogram was badly designed (48 first-come slots filled with
  cold pages; only one page, 0x71f5535000, crossed the print threshold). What it
  does establish: that RIP is inside the crack image's REAL mapping
  (0x71f53a0000+0x20f000) and only ~9k of 11.5M hits had no FEX frame. Combined
  with the target/insn profile (byte/word LDAPR at scattered 0x130xxxxx addresses)
  the accessor is the image's own interpreter. Not proven exhaustively.
- **ml1057 (installed):** FEX `OpDispatchBuilder::BeginFunction` asks
  `IosSubfloorWindowForCode(RIP)`; for blocks that live in a sub-floor image (real
  or low address) every `_LoadMemAutoTSO/_StoreMemAutoTSO` (Ref and AddressMode
  forms; the latter materialises the EA) gets
  `EA += (((EA-Low)-Size) & ~(EA-Low)) >>s 63 & (Real-Low)` -- five flag-free IR
  ops, arithmetic verified offline incl. edge cases. Other images untouched;
  uncovered paths (string ops, atomics) still take the old exception emulation.
  Logs `[iOS-subfloor-xlate] ml1057` (first 4). Acceptance: `[subfloor] ml956
  serviced` far below 12M, mach_msg share down, FPS.
  Risk: stores into the image's own code pages now fault at the REAL address and go
  through the ordinary SMC path rather than the sub-floor emulator.
- Also in ml1057: `[madeira-d3d12] ml1057 live GPU backing (estimated)` by kind
  (tex-RT/DS/UAV/sampled/MSAA, buf-private/shared), live + peak, every 3000 lists.
Memory facts at the kill: tag100 2.59 GB; game arena = nine fully-dirty 128 MB
regions at 0x708af30000.. (1.15 GB) + two more; pool 560 MB of which head 312 +
tail 176 touched (the 128 MB code buffer is NOP-PREFILLED, dirtying all of it up
front -- a candidate ~100 MB); `pa` band (native malloc in the jumbo reserve)
592 MB dirty -- unexplained, next thing to attribute.

## §60 — ph-rdr25 (ml1057) results; ml1058 = userspace ntsync (in-process sync)

**ml1057 measured:** `[iOS-subfloor-xlate]` fired; `[subfloor] serviced` 49,153 for
the whole run vs 12,029,953 before (~250x fewer Mach exceptions). FPS unchanged
(~10-11). CORRECTION to §57: the ~16-20 % of RUN samples in `mach_msg2_trap` is
the ml876 thread sampler observing itself (thread_get_state is a MIG call), not
exception service -- it is still 15.8 % with the exceptions gone. Exceptions were
real waste but never on the frame's critical path.

**What bounds the frame:** no thread saturated (top 78 %, then 53 %, 37 %); the
wineserver answers ~17,500 requests/s: `select` 66 %, `event_op` 26 %,
`release_semaphore` 5 %, `set_queue_mask` 2 %. ~1,750 synchronisation round trips
per frame at 10 FPS, each a pipe write + server wake + reply + client wake.

**GPU census (ml1057, estimated live bytes at the kill):** total 1,286 MB --
tex-RT 281 (435), tex-DS 88 (27), tex-UAV 53 (136), tex-sampled 270 (14,032
textures!), buf-shared 590 (644), buf-private 0. But tag100 dirty was 2,480 MB:
~1.2 GB is NOT accounted by resource payloads. Prime suspect: 14k individually
allocated small textures (each a page-granular IOAccelerator allocation + driver
metadata) -- i.e. placed resources that should sub-allocate from an MTLHeap;
then 1,676 pipeline states, argument rings, descriptor heaps. Unmeasured split.

**ml1058 (installed): `build/madsync/`** -- a userspace implementation of Linux
ntsync semantics, so Wine 11's existing in-process sync (server/inproc_sync.c,
ntdll/unix/sync.c) runs with NO server round trip for wait / release / set.
- `linux/ntsync.h` shim (both builds get `-I build/madsync -DHAVE_LINUX_NTSYNC_H`):
  structs, private request codes, `ioctl`/`close` redirected for the two including
  files only (non-ours fall through to libc).
- `madsync.c`: one global mutex; semaphores, recursive owner-tracked mutexes with
  one-shot EOWNERDEAD, manual/auto events, pulse, wait-any / wait-all (atomic under
  the one lock), alert reported as index==count, MONOTONIC/REALTIME absolute
  timeouts. Descriptors are PSEUDO fds (0x70000000|index): no kernel fds, identity
  survives hand-off. Termination-safe: waiter is per-thread HEAP state in pthread
  TSD with a destructor, threads sleep on a per-thread Mach semaphore with NO lock
  held, all signals masked while the lock is held (a Wine thread is killed via
  pthread_exit from a signal handler). Offline: ASan+UBSan+TSan clean, semantics
  tests pass, 200x kill-while-waiting clean, ~1.2M ops/s on 8 threads.
- Hand-off replaces SCM_RIGHTS: server `madsync_post(pid, handle, fd)`, client
  `madsync_take(GetCurrentProcessId(), handle)` (sync fd + alert fd); device fd is
  the constant MADSYNC_DEVICE_FD, nothing sent.
- ⚠️ iOS-specific hazard handled: ntdll's inproc cache is indexed by HANDLE VALUE
  and all pseudo-processes share one ntdll-unix => made per-PEB
  (`ios_get_inproc_cache`), dropped in `ios_fd_cache_release`.
- ⚠️ Build trap hit twice: wineserver's PATCHED_FILES only compiles; REPLACEMENTS
  is what enters the archive (inproc_sync.c was previously the prebuilt all-stubs
  object). Verify with `nm -A app/Madeira/libwineserver.a | grep madsync`.
- Kill switch: `Documents/madeira-inproc-sync.txt` containing `0`.
- Logs: `[madsync] ml1058 in-process synchronisation ENABLED`, periodic
  `[madsync] ml1058 N waits (B blocked), W wakes`. Acceptance: `[srv-req]` rate
  collapses (select/event_op/release_semaphore mostly gone), FPS up.

## §61 — ph-rdr26 (ml1058): madsync works (13-15 FPS); the exception thread is the next wall

User: 13-15 FPS in the snow scene (was 10-11). Run ended in jetsam (8186-8190 MB)
-- it coincided with an interrupted devicectl pull, which was NOT the cause. NOTE:
devicectl copy fails ("socket closed") while the benchmark is running hot.

madsync, measured: ENABLED; wineserver requests fell from ~17,500/s to ~700-3,000/s
and `select` / `event_op` / `release_semaphore` no longer appear in the top ten
(what is left: req87, req21, req92, set_queue_mask, open_mapping). 30.9M waits in
~10 min (~51k/s), of which 30.5M were EMPTY POLLS (deadline already past) and only
581k real wakes: the game's spin-yield loop polls handles at zero timeout. ml1059
returns those before linking a waiter. `__pthread_sigmask` is ~9 % of CPU-weighted
samples -- the price of masking signals around the lock at 50k ops/s; acceptable
for now, revisit after the exception fix.

**Next wall, found from thread samples:** thread "wine-x18-exc" (the Mach exception
handler) sits at 32-54 % CPU, and the hottest sampled guest pc is kernelbase
+0x23d98 reached via `lr = std::chrono::system_clock::now()+0x2c` inside FEX.
`LookupCache::UpdateDynamicL1Stats` calls `system_clock::now()` on EVERY L1 miss
that hits L2/L3 (12M lookups a run). libc++ reaches the Win32 clock through a
GetProcAddress'd pointer; on iOS that pointer names the image's NON-EXECUTABLE
backing, so every call is exec fault -> Mach exception -> redirect to the pool copy
(`[x18-redir2]` alone counted 1,384,448 at that single pc, and that counter only
sees the x18==0 subset). ml1059: FEX reads the clock once per 256 hits.
GENERAL DEFECT (not fixed): any native function pointer obtained at runtime
(GetProcAddress, vtables, callbacks) costs one Mach exception per CALL. ml1059 adds
`[exec-redir] ml1059 N executions through non-executable backing; hottest: pc=n`
every 100k to enumerate the rest. The proper fix is translation at the source
(LdrGetProcedureAddress et al. for native-code exports), but FEX's EC-bitmap
classification keys on backing addresses -- needs design, do not rush it.

## §62 — ph-rdr27 (ml1059): 15-25 FPS; profile is finally dominated by guest code

User: 15-20 FPS snow scene, 20-25+ after; jetsam (8175 MB) before the heavy fifth
scene -- the run now reaches the limit at footprint cycle 755 (was ~1570), so
MEMORY is the hard stop.

Measured: exec-through-backing redirects collapsed to 100,000 for the whole run
(ml1059 clock decimation); hottest remaining backing pcs 0x71ffd76250 (1100/100k),
all minor. madsync 20.7M waits: 19.3M empty polls, 860k sleeps, 748k wakes.
Exception thread 4-12 % CPU (was 32-54 %). Hit rate 98 %.

CPU-weighted profile, second half: guest JIT code 23 % + other JIT 8.5 % (top RUN
category for the first time); semaphore_wait in do_wait 26 % (threads legitimately
asleep); mach_msg 10 %; __pthread_sigmask 6 % (madsync's lock discipline);
cthread_yield 5 %; FEX 5 %; MTLResourceListAddResource 2 %.
The mach_msg share is OUR MONITOR: `ios_pool_warmer_thread` drops to a 250 ms
cadence above 2400 MB (ml668, tuned for a 4 GB phone) but every block in it is
gated on `cycle`, so the 120k-region phys-map walk ran every 1.25 s and every pool
page was re-touched 4x/s: one thread at ~30 % of a core for the whole benchmark.

**ml1060 (installed):**
- monitor: fast cadence runs ONLY a cheap footprint line (printed when it climbs
  16 MB); the full body is back on its designed ~2 s period.
- D3D12: per-draw/per-dispatch useResource loops skipped when a residency set
  exists AND nsrv > 256 (the list saturated on an arbitrary first 256 SRVs and the
  UAV loop never got a slot, so it was pure cost: 256 commands x 26k draws/600 lists).
- census: `ml1060 shader libraries created: N, M MB of metallib` -- every pipeline
  stage gets its OWN MTLLibrary even when the bytecode is shared; candidate for the
  unexplained ~670 MB of native malloc in the `pa` band.
Memory candidates, unmeasured: library dedup; 15k individually allocated small
textures (tag100 2.35 GB vs 1.39 GB of payload); 128 MB code buffer NOP-prefill
(no saving unless the pool warmer stops touching unused tail pages).

## §63 — ph-rdr28 (ml1060): 20-30 FPS, then a FREEZE with a named cause; ml1061 = async fences

User: snow scene often >20 FPS (thermal "serious"), scene 2 touched 30, scene 3
nearly locked 30, then froze ~100 MB short of jetsam. (The Metal HUD's 8.6 "GB" is
the same number as our 8192 MiB: 8192 MiB = 8.59 GB decimal.)
Shader-library census: 4,231 libraries but only 45 MB of metallib -- NOT the
unexplained native memory; drop that theory.

**The freeze, fully attributed from the log:**
- 00f0 holds the queue's `submit_lock` (entered at madeira_d3d12 `queue_Signal+0x40`)
  and sits forever in `_MTLCommandBuffer_waitUntilCompleted` (thread-stacks bt).
- 00f8 holds game CS 0x1456C97B8 and waits for `submit_lock` ("blocked by 00f0").
- 00e8 waits for 0x1456C97B8 ("blocked by 00f8"). Everyone else parks behind them.
WHY that command buffer never completed is unknown (no GPU error was ever logged);
ph-rdr19's identical-looking freeze predates madsync, so it is not the sync work.

**The perf defect found on the way:** `queue_Signal` = commit + BLOCK until every
pending command buffer completes, under the submission lock. The render thread
stood still for the whole GPU frame at every fence; CPU and GPU never overlapped.

**ml1061 (installed), PE runtime only:**
- GPU timeline: one MTLSharedEvent per device; serial assignment + encodeSignalEvent
  + commit are one step under `fence_lock`, so serials are monotonic across queues
  and threads.
- `queue_Signal` returns immediately: a FIFO job {serial, fence, value, the batch's
  command buffers} goes to `mad_fence_worker`, which waits on the event, retires the
  buffers (status still checked) and advances the fence. Synchronous path kept when
  no event exists or a capture (`ncap`) is pending. Lock order everywhere:
  submit_lock -> fence_lock.
- `queue_Wait` is a no-op once the fence value has been SUBMITTED (`fence.submitted`):
  all D3D queues feed one Metal queue in commit order, so the dependency already
  holds; only a not-yet-submitted signal still takes the CPU wait.
- ⚠️ Prerequisite bug fixed: per-list argument rings were rewound at LIST Reset
  ("the allocator's reset rule says the GPU is done" -- wrong, that rule is about
  ALLOCATORS). Rings are written at replay and read at GPU time, so re-recording a
  list overwrote arguments the GPU had not consumed; the synchronous Signal hid it.
  Chunks now retire with their batch's serial (per-queue batch->serial history,
  forcing a flush if the batch is still open) into a device pool and are reused
  only after the GPU passes that serial. Possibly related to the reported
  geometry popping -- unproven.
- `[madeira-d3d12] ml1061 GPU STALL: serial N not reached after M ms ... status`
  at 5 s then every 60 s: evidence for the next hang instead of silence.

## §64 — ph-rdr29 (ml1061): freeze = a command buffer that ENDED IN ERROR; ml1062 = TSO A/B

User: snow 20, night forest 25, night landscape 30, night camp 17, Arthur scene
13-18 FPS; footprint stayed under 8 GB most of the run (peak 7718 MB logged);
dropped to 1 FPS for seconds around a SCREENSHOT, recovered, then froze after
another screenshot. Async fences worked (`fences are delivered asynchronously`).

The ml1061 watchdog named the freeze: `GPU STALL: serial 188372 not reached ...
(event at 188370, committed 188374); command-buffer status of this batch: 5 5` --
the batch's command buffers ENDED IN ERROR. A failed buffer never runs its
encodeSignalEvent, so its serial is never reached and the fence worker waited
forever. (Under the old synchronous Signal the same failure presumably presented
as the ph-rdr28 lock pile-up.) The Metal error TEXT is still unknown: retire, which
logs it, never ran. The screenshots are a plausible trigger (a screenshot makes iOS
allocate/copy GPU surfaces at ~100 MB from the process limit) -- unproven.
**ml1062:** the worker checks buffer status on every 50 ms tick; a failed batch
logs, records `gpu_serial_failed` (so fences with no buffers of their own queued
behind it also advance) and advances its fence; retire then prints the NSError.

**The 2x lever (user asked for multipliers, not trims):** FEX emulates x86 total
store ordering for EVERY guest memory access (`FEX: TSO config tso=true
halfbar=true`): loads as LDAPR, stores as STLR -- each store an implicit barrier.
Real Windows-on-ARM avoids this with compiler-emitted "volatile metadata" telling
the emulator where ordering matters; FEX supports it, but RDR2.exe was linked with
MSVC 14.14 (VS2017, 2020-09-19), which predates it, so the whole 117 MB image runs
with TSO on. iOS offers no hardware TSO. ml1062 adds `Documents/madeira-env.txt`
(KEY=VALUE per line, exported before Wine starts, echoed to the log as
`[madeira-env] ml1062 K=V`) so ANY FEX option is a file edit; pushed with
`FEX_TSOENABLED=0` for this test. Risk: code relying on x86 ordering without LOCK
or explicit atomics can misbehave (rare, timing-dependent); LOCK-prefixed and
xchg atomics are unaffected. Compiled default is unchanged (TSO on). Expect the
UNALIGNED-BACKPATCH exceptions to vanish too (they exist only for TSO atomics).
Other FEX defaults checked: Multiblock on, MaxInst 5000, SMC mtrack, L2 cache
disabled/dynamic L1 on, AVX host features off (SSE paths), x87 reduced precision off.

### §64.1 — TSO-off result (ph-rdr30): benchmark LOAD livelocked on the first attempt
`tso=false` confirmed in the log. The load never finished: one guest thread at 99 %
CPU in the game's poll loop (madsync counted 288M empty polls, stack candidates
RDR2.exe+0x25a5120 / +0x2b0e10f), everything else idle, footprint flat at 5.8 GB, no
GPU stall, no CS deadlock. One run is not proof, but this point has never hung in
~10 TSO-on runs, and a producer/consumer handshake that loses an update is exactly
what removing store ordering produces. Reverted to TSO on. Kept: per-MODULE TSO off
via `FEX_EXTENDEDVOLATILEMETADATA=oo2core_5_win64.dll:bink2w64.dll` (self-contained
decompression / video decode). RDR2.exe's hot code cannot be range-selected
offline: the hottest sampled region (+0x2e1c6d9, ~20 % of guest samples) is
ENCRYPTED on disk, so choosing TSO-free ranges needs a runtime profile + care.

### §64.2 — TSO cost MEASURED (correction) and ml1063 (built, not installed)
Micro-benchmark on an M4 Max (same ISA family as the phone; scratchpad
tso_bench.c): mixed str+ldr vs stlr+ldapr = 0.61 ns vs 0.61 ns per iteration
(1.0x); a pure 4-store stream = 0.78 vs 1.54 ns (2.0x). Apple cores make LDAPR
free and STLR cost only store-issue bandwidth, so FEX's TSO emulation is NOT the
2x lever I called it in §64 -- for ordinary code it is a few percent, at most tens
of percent in store-dominated loops. The TSO-off hang (§64.1) therefore bought
nothing worth its risk; it stays on. The per-module exceptions for oo2core/bink
stay (harmless).

What the samples say the frame actually is: 1-2 game threads pegged at 100 % with
total process CPU ~200-430 % of 600 %. One pegged thread is the game's own
spin-poll worker (WaitForSingleObject(h,0) + SwitchToThread in a loop; on iOS
sched_yield with nothing runnable returns immediately, so it burns a performance
core and heat). The other is the real critical-path thread: FPS is bound by
single-thread translated-code throughput on that one thread. That is why removing
system overhead now gives diminishing returns; the remaining big levers are (a)
give that thread an uncontended, cool performance core, (b) JIT code quality.

ml1063: (a) `NtYieldExecution` adaptive backoff on iOS -- after 256 back-to-back
yields (<2 ms apart) the thread sleeps 100 us instead of yielding; streak resets
after a 2 ms gap. (b) madsync lock-free poll: a wait whose deadline has passed and
whose objects are all unsignaled returns ETIMEDOUT with no lock and no sigmask
syscalls (object table now grows by publish-then-cap, old tables leaked, so
unlocked readers are safe). ASan/UBSan/TSan clean, kill test clean.

## §65 — ph-rdr31 (ml1062, TSO on): BENCHMARK COMPLETED, 21 FPS average; ml1064 installed
User: snow 23, forest 27, night landscape 30, camp 18, Arthur 15-20 (22-25 on the
still horse at the end); the game's own benchmark summary said **21 FPS average**;
back in the menu it crashed. Footprint peak 7813 MB. madsync: 202M waits, of
which 197M EMPTY POLLS (ml1063 makes those lock-free and sleeps the spinner).
Menu crash: the game RE-CREATED its D3D12 device on the way back ("device
created" ... "destroyed Device"), then a JIT write to the image (0x14011b2c7 from
pc 0x1669cf63c) was redelivered 2000x and ml465 terminated. Not analysed further
(user: low priority). But it exposed a real ml1061 defect: the fence worker held a
raw pointer to the DESTROYED device. ml1064: `device_Release` sets `fence_quit`,
wakes and joins the worker, releases the timeline event, frees the pools.
ml1064 = ml1063 (adaptive yield + lock-free polls) + that fix. Installed.

## §66 — ph-rdr32 (ml1064): same FPS; menu crash is DETERMINISTIC and now explained; ml1065

User: 20/27/30/17/15-20 -- no change from ml1064's spin-loop work (the spinner's
core share fell from 100 % to ~60 %, empty polls doubled to 408M because each is
cheaper; nothing on the critical thread changed). 1-FPS episodes recur (compressed
memory 3.7 GB at the time: compressor thrash is the likely cause). Arthur "stops
on his horse" while the world keeps running -- game logic, unexplained. Persistent
white-noise audio since audio first worked.

**Menu crash (2 for 2, same site):** on return to the menu the game recreates its
D3D12 device, then a crack byte store (`stlrb`) into the image at 0x14011b2c7
faults; ml1018's byte-store path DECLINED the backpatch and retried the same
instruction expecting the page to have become writable -- on iOS it never does
(Wine's Mach emulator completes STR/STRB but not the release forms), so 2000
identical redeliveries -> ml465 termination. Exactly Astra's §2 warning ("merely
returning from ml657 without a store can recreate the old infinite retry").
**ml1065:** rewrite the instruction IN PLACE to the plain byte store with the same
registers/offset (stlrb->strb, stlurb->sturb), flush icache, retry: PC[-1] stays
intact, the retry is an ordinary store fault that Wine completes. Release ordering
on a byte written into a code page is the only thing given up.
Also ml1065: `[ios_audio] ml1065 slot=N source: samples, mean|x|, peak, clipped`
every 10 s per stream -- tells whether the noise is in the data the game hands us.

Profiling for selective TSO: the existing `[rip-profile] ml981` reads the FEX
frame's State.rip, which is only updated at block boundaries and produced
implausible values this run (0x3cec000, 0x7a68000); it cannot be trusted for
attribution. A usable profile needs host-pc sampling of the pegged thread mapped
to guest blocks via FEX's block tails. Not built yet.

## §67 — STATE OF PLAY (2026-09-21, end of day) — for external review

Self-contained summary of where the RDR2-on-iPhone effort stands, what the
measurements say limits performance, and the candidate unlocks. Every number
below is from a device log unless marked "estimate". Reviewers: please attack
the reasoning; the goal is 60 FPS and the current honest ceiling estimate is well
below it.

### 67.1 The stack
- iPhone 18 Pro (iPhone19,2, iOS 27, 12 GB, jetsam limit for this app 8192 MiB
  with Game Mode + increased-memory entitlement), sideloaded (free Apple ID, no
  jailbreak, no paid entitlements). JIT via StikDebug debugger attach, dual-mapped
  RX/RW pool of ~560-624 MB placed in the ~1.4 GB VA gap below the malloc zones.
- Wine 11.4 (ARM64EC PE DLLs) + FEX (x86-64 -> ARM64 JIT, `xtajit64.dll`) +
  our own D3D12-on-Metal runtime (`madeira_d3d12.dll`, SM5.1 DXBC shaders via
  DXMT's airconv; DXIL via Apple's converter) + a userspace ntsync ("madsync")
  for in-process synchronisation. wineserver is a thread of the same process.
- Game: RDR2 x86-64 (117 MB exe, no relocations, fixed base 0x140000000, MSVC
  14.14/2020), DRM-cracked (crack DLL `EMP.dll` at a sub-4 GB fixed address that
  iOS cannot map -> emulated window; it also rewrites the main image at runtime
  (~16-20k self-modifying writes per run) and runs a bytecode VM).
- Settings: 960x540, texture quality LOW, MSAA off.

### 67.2 Performance history (in-game benchmark, per scene, user-reported FPS)
| build | snow | forest | night landscape | night camp | Arthur riding | note |
|---|---|---|---|---|---|---|
| ml1049 (first non-black) | ~1 | - | - | - | - | JIT thrash 56 % hit |
| ml1051 | 3-5 | - | - | 12-13 | - | thrash happened not to trigger |
| ml1052-54 | ~10 | ~10 | ~10 | 6 | - | code cache fixed (98 % hit) |
| ml1058 (madsync) | 13-15 | - | - | - | - | server round trips gone |
| ml1059 | 15-20 | 20-25 | 25+ | - | - | clock-call exception storm gone |
| ml1060/61 | 20-23 | 25-27 | 30 | 17-18 | 13-20 | async fences, monitor quieted |
| ml1062/64 | 20-23 | 27 | 30 | 17-18 | 15-20 (22-25 idle) | benchmark avg **21 FPS** |
Loading: ~5 min to the benchmark. Thermal state reported "serious" during runs.

### 67.3 What the profiles say NOW (ph-rdr29/32, thread samples)
- Process CPU ~200-430 % of 600 % available (6 cores; A20 Pro core split not
  verified by us). In every burst ONE game thread is pegged at 100 %: that is the
  critical path (render/main), and FPS tracks its single-thread throughput of
  translated code. A second pegged thread was the game's own spin-poll worker;
  ml1063/64 (adaptive yield) took it to ~60 %.
- Running-sample breakdown (second half of a benchmark, ml1059+): translated guest
  code ~23 % + JIT helpers ~8 %; threads asleep in waits ~26 %; our monitor thread
  ~10 % (fixed ml1060); madsync's signal masking ~6 %; game yield spin ~5 %;
  FEX runtime ~5 %; Metal per-draw bookkeeping ~2 % (fixed ml1060).
- Overheads that used to dominate and are now gone (measured before/after):
  JIT code-buffer rotations 392 -> 1 per run; wineserver requests 17,500/s -> ~600/s;
  Mach exceptions from the crack window 12M -> 49k per run; exec-through-backing
  redirects >1.38M -> 100k per run; GPU-sync stalls at every fence -> async.
- GPU: at 960x540 GPU time was ~5 ms/frame when measured (HUD); not the limiter.
  (Metal HUD file logging is being enabled for the next runs.)
- Memory: runs end at the 8192 MiB jetsam limit (or 1-FPS episodes with 3.7 GB in
  the compressor). Composition at the kill: game heap ~4.4-4.6 GB (eleven fully
  dirty 128 MB arenas = 1.4 GB of it), GPU (tag 100) 2.3-2.6 GB of which only
  ~1.3-1.4 GB is resource payload (15k individually allocated small textures +
  driver metadata suspected for the rest), JIT pool ~490 MB resident, FEX ~300 MB,
  native malloc in the high band ~600-670 MB (unattributed; shader libraries are
  only 45 MB of it).

### 67.4 Where the remaining time goes (best current model)
FPS = f(single critical thread). On that thread the cost is: x86 -> ARM64
translation quality (instructions emitted per x86 instruction, register
allocation, flag computation), TSO memory-ordering instructions, the crack's VM
and hooks executing inside the game's frame, and residual exception traffic.
We have NO per-instruction profile of that thread yet (the existing rip-profile
tool is unreliable, §66) -- building one is the single most valuable next step
because it decides which of the unlocks below is real.

### 67.5 Candidate unlocks (ranked by my estimate; please challenge)
1. **A real profiler for the critical thread** (1 kHz host-pc sampling of the
   pegged thread, mapped to guest blocks via FEX block tails; per-block cost).
   Not a speedup itself; it is the instrument everything below needs.
2. **Selective TSO** via `FEX_EXTENDEDVOLATILEMETADATA` ranges in RDR2.exe.
   Measured instruction cost on Apple cores: mixed load/store code 1.0x, pure
   store streams 2.0x (M4 Max microbenchmark, §64.2). Global TSO-off livelocked
   the load (§64.1). Estimate for RDR2: +5-15 %, needs the profiler + careful
   range choice (hot code is encrypted on disk). Per-game configuration.
3. **JIT quality** (FEX itself): multiblock is on, AVX off (SSE paths), x87
   reduced precision off (irrelevant for this game). Unknown headroom without
   the profile; FEX upstream improvements land here.
4. **Crack overhead**: the DRM crack's bytecode VM and SMC hooks run inside the
   frame (16-20k self-modifying writes per run, each a Mach exception + SEH
   round trip; a VM interpreter reading through the sub-floor window, now
   translated inline). A legitimate, un-cracked build (or Steam's own) would
   remove this entirely -- unmeasured share, possibly several percent.
5. **Memory** (not FPS directly, but it ends runs and causes 1-FPS episodes):
   (a) MTLHeap-backed placed resources so 15k small textures stop being
   individual allocations (suspected ~1 GB); (b) attribute the ~650 MB native
   malloc; (c) 128 MB code buffer NOP prefill (~100 MB, only if the pool warmer
   stops touching it); (d) the game's own arenas are sized from reported RAM
   (4096 MB clamp) and VRAM budget (1536 MB) -- both already minimal.
6. **madsync signal masking** (~6 %): replace pthread_sigmask around the lock
   with a deferred-kill protocol. Bounded gain.
7. **Thermals**: sustained runs throttle ("serious"). Every watt we remove is
   FPS later in a session; the spinner fix and monitor quieting are this kind.
8. **Rendering correctness that will COST**: tessellation (27 pipelines, ~28k
   draws/run currently dropped = missing snow ground/geometry) adds GPU and CPU
   work when implemented.

Explicitly ruled out / measured: global TSO off (livelock; small gain anyway);
Vulkan/MoltenVK instead of our D3D12 runtime (does not touch the FEX-bound
thread, loses generality); shrinking the JIT pool (its RSS is exempt/needed);
`totalphys` clamp as a memory lever (no effect on occupancy).

### 67.6 Open defects (not performance)
- Persistent white-noise audio (probe ml1065 will say whether the source data
  is noisy). - Menu-return crash (byte-store SMC retry loop; fixed ml1065,
  unverified). - Occasional GPU command-buffer errors near the memory limit
  (now survived, error text will be logged). - Arthur stops riding while the
  world continues (unexplained). - Geometry popping / surface glitches (parked).
- Tessellation not implemented. - The ph-rdr14/15 arena write-permission fault
  (not seen since; logging armed).

### 67.7 How to reproduce a measurement
Build chain and IPAs in `build/ipa/`; logs pulled with devicectl from
`Documents/madeira-log.txt` (previous run in `madeira-log.prev.txt`; pulls fail
while the benchmark runs). Key log lines: `[CB_SUMMARY]` (JIT hit rate),
`[srv-req]` (server mix), `[madsync]`, `[subfloor] ml956`, `[exec-redir]`,
`[thread-sample]` bursts (per-thread cpu + pc), `[footprint]`, `[phys-map]`
bands, `ml1057 live GPU backing`, `ml1049/ml1050` D3D12 counters,
`ml1061 GPU STALL`. Runtime knobs: `Documents/madeira-env.txt` (FEX_* vars),
`madeira-inproc-sync.txt`=0 (disable madsync), `madeira-vram-mb.txt`.

## §68 — ph-rdr33 (ml1065): 22 FPS benchmark average; audio noise LOCATED; menu crash mechanism refined

User: benchmark summary **22 FPS average** (best yet); stopped when the horse halted
on the Saint Denis bridge -- same spot as the first full run, so that is probably
the benchmark's natural end. Footprint peak 7670 MB, under 8 GB all run (two runs
in a row now; async fences (ml1061) remain the plausible cause -- upload memory is
recycled sooner -- unproven). Menu crash again after a while on the menu.

**Audio (ml1065 probe):** slot 0 (48 kHz, 2 ch, 16-bit) carries FULL-SCALE UNIFORM
NOISE from its first buffer: mean|x| = 0.500, peak = 1.000, ~4,800 clipped samples
per 10 s, at a steady 48,000 frames/s. mean|x| of 0.50 is exactly a uniform random
16-bit signal -- i.e. uninitialised or misinterpreted memory, not music/effects.
Slot 1 (float32) carries real, quiet audio (mean 0.006, peak 0.15) and long
silences. So the white noise IS in the data the 16-bit client writes (or our
reading of its scratch buffer), not in the mixer. ml1066 logs, per stream, the
client name mmdevapi passes plus flags/duration/period/format, to identify that
client (candidates: Bink video audio, the launcher/social-club, dsound via
Wine's mmdevapi with a stale mixing buffer).

**Menu crash (ml1065 did not fix it; mechanism now clear):** the in-place
stlrb->strb rewrite DID fire (`Unhandled JIT SIGBUS: PC ... Instruction:
0x39000028` = the rewritten STRB), but the retried plain byte store faults again
and re-enters FEX's SMC branch, where `HandleUnalignedAccess` does not know STRB,
logs "Unhandled JIT SIGBUS" and the branch returns without progress -> same 2000-
redelivery death (pc 0x16bf41a1c, addr 0x14011b2c7). The target pages show
`entryprot=1 nowprot=1` in `[fault_rip]` (read-only), i.e. unlike the ~21k wider
SMC stores per run that succeed because HandleRWXAccessViolation's unprotect makes
their page RW, THESE pages cannot be made writable, so no retry can ever land.
Correct fix = PERFORM the byte store from the handler through a writable route
(Wine's `ios_emulate_store` already knows STRB, but that path is only reached for
pool-RX targets today; an image-page write needs Wine's wr-strip/backing route).
Not done; the crash is on the menu after the benchmark and does not affect
measurements. 43 `TEXT LOST EXEC` events this run.

## §69 — ph-rdr34: FIRST STORY CUTSCENE (9-10 FPS), then a GPU fault froze rendering; ml1067

User entered Story mode: the opening cutscene rendered at 9-10 FPS, then froze.
The log has the whole chain, thanks to ml1062's error logging:
  #1 `Caused GPU Address Fault Error (kIOGPUCommandBufferCallbackErrorPageFault)`
  #2 `Caused GPU Hang Error`  #3 another page fault
  #4 `Ignored (for causing prior/excessive GPU errors) (SubmissionsIgnored)`
After #4 the driver drops every submission on that queue: the game keeps running
(fences advance via ml1062), but nothing renders -> "frozen". The address of the
fault is not in the error. Best candidate for the fault: a resource created or
viewed while a batch was OPEN was added to the residency set, but the set was only
committed when the NEXT command buffer opened, so the batch that first used it ran
with it non-resident; the per-draw useResource lists that used to paper over this
were removed in ml1060. Cutscenes create many resources mid-batch.
**ml1067 (installed):** (a) commit a dirty residency set immediately before each
batch commits; (b) when the driver reports SubmissionsIgnored, replace the Metal
command queue (residency set re-attached, old queue leaked) so rendering resumes;
(c) `mad_cb_retire` now takes the device.

**Audio:** all four streams are opened by 'Red Dead Redemption 2' itself. The noisy
one is the 16-bit, polling (flags=0), 30 ms-buffer client; the three float ones are
event-driven (0x800c0000 = AUTOCONVERTPCM|SRC|EVENTCALLBACK), 80 ms. So the noise
is a second render client of the game -- most likely Bink (video) or a
voice/telemetry path -- writing garbage, or our GetBuffer serving it a pointer it
does not write (render_scratch is REALLOC'd on a larger request, so a cached
pointer would go stale -- unverified). ml1067 dumps the first 12 words of its
buffer every 10 s to tell float-bit-patterns / stale / zero apart.
Menu-crash byte-store loop (§68) unchanged.

## §70 — ph-rdr35: the white noise is OUR mix format; ml1068

ml1067's byte dump settled it. The noisy stream (RDR2's own 16-bit, polling, 30 ms
client) holds float32 samples: `5660 b8d6 aabc 39c9 ae70 b905 ...` = 0xb8d65660
(-1.0e-4), 0x39c9aabc (3.9e-4), 0xb905ae70 (-1.3e-4) -- quiet audio in float,
which read as int16 is full-scale uniform noise (mean|x| 0.500). Root cause:
`ios_get_mix_format` advertised a 16-bit PCM shared-mode mix format
(IOS_AUDIO_BITS 16, SubFormat PCM). On every Windows since Vista the shared-mode
mix format is 32-bit float; the game's engine Initialize()s with GetMixFormat's
answer but renders float regardless (the assumption is universal). Our buffer was
sized for 4-byte frames while the client wrote 8-byte frames -- also a heap
overrun of render_scratch. ml1068: mix format = float32 (32 bits, 8-byte frames,
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT). General fix, not a game fix. Untested.
Also this run: 0 GPU errors (ml1067's residency commit at flush, one run);
footprint peak 7662 MB; benchmark ran; menu crash (byte-store loop) as before.

## §71 — ph-rdr36 (ml1068): audio FIXED (confirmed by user: music and voices); story cutscene to ~17 FPS; jetsam on skip

- Audio: user confirms clean music and dialogue. §70's cause stands.
- Story: the first cutscene now runs (peak ~17 FPS, HUD frame times mostly 33-70
  ms). Holding Space to skip triggered the next area's streaming and the footprint
  went 7945 -> 8187 MB in ~30 s: JETSAM. Memory is the story-mode wall.
- Before that, 12 command buffers ended in error: `GPU Hang` x7, then
  `SubmissionsIgnored`; ml1067's queue replacement fired 3 times and rendering
  resumed each time (it works). Right before the first hang: "topology 35
  (adjacency or patches) drawn as triangles" and HUD frame times of 200-950 ms.
  A patch-list draw on a pipeline whose HS/DS we did not see (has_tess=0 --
  probably created through a PSO STREAM description, which our parser does not
  scan for HS/DS) was rasterised as triangles: screen-sized garbage with a full
  pixel shader each -> GPU watchdog. **ml1069 (installed):** any draw with a
  patch-list topology is dropped and counted, whatever the PSO looks like.
- Byte-store SMC loops (§68): 10,452 `Unhandled JIT SIGBUS` retries at 117 pcs this
  run, in bursts of 300-450 that DO eventually progress (the page becomes
  writable later) -- so they cost stalls (each retry is a Mach exception + SEH
  round trip, ~30-45 ms per burst) but are not fatal in-game; fatal only on the
  menu return. Still owed: perform the byte store in the handler.

## §72 — 2026-09-22: the GPU IS a limiter in heavy scenes (HUD data); plan; ml1070

Metal HUD file logging (user enabled it) gives per-frame (interval, GPU time)
pairs. Parsed (scratchpad): in the benchmark's ~20 FPS windows GPU time is
**median 27 ms, p90 40 ms**; the first cutscene **48-76 ms**; menu 7 ms. At 960x540
that is far too much for the pixel work, which points at pass boundaries on a
tile-based GPU: every render encoder we end and restart loads and stores every
attachment (5 G-buffer targets + depth). We end encoders on: any clear (even of an
unrelated target), every compute dispatch, every blit, every command-list end
(~30 per frame), every attachment-less boundary, and every target change;
clear-only passes get a whole pass of their own; stores are always Store and
loads always Load. Astra's two reviews (TSO caution; MetalFX frame interpolation
as the 60 Hz route; a low-workload A/B; frame-latency object always signalled)
are consistent with this; the frame-latency point is fixed below.

**ml1070 (built, install pending the user's current run):**
- `[madeira-d3d12] ml1070 render passes in those lists: N begun (R reused), ended
  by: targets/clear/dispatch/blit/list-end/attachless; clear-only passes; attachment
  load+store ~MB` every 600 lists -- the census that decides which merge to build.
- Bounded frames in flight in Present: frame N waits (event, 1 s cap) until the
  GPU has passed frame N-2's last committed serial (SetMaximumFrameLatency
  honoured 1..3). Back-pressure when GPU-bound, less live upload memory.
Plan after the census: (1) do not end a pass for a clear of a target that is not
bound, and fold clears into the next pass's load action; (2) keep the render
encoder open across command-list boundaries when the next list continues the
same targets (defer exec_end to the batch); (3) DontCare/Memoryless for targets
never read back (transient depth, G-buffer when consumed in-pass); (4) then
tessellation (real terrain) and the memory heap work.

## §73 — ph-rdr37/38 (ml1069/ml1070): cutscene now dies ONLY on memory; ml1071/ml1072 memory work

ph-rdr37 (ml1069): cutscene, 0 GPU errors, 0 queue replacements (the patch-list
drop removed the hangs), jetsam at 8178 MB. ph-rdr38 (ml1070): same, 8168 MB.
Pass census (ml1070): ~30-34 render passes per frame, ended by: target change 225,
clear 83, dispatch 105, blit 49, list-end 125 per 600 lists; clear-only passes
75; attachment load+store ~150 MB/frame. Modest -- consistent with the user's HUD
reading (GPU <= 15 ms, interval 50 ms): the GPU is NOT the benchmark limiter; my
§72 HUD-column read was wrong (column 2 is not GPU time). Pass merging is parked.
Bounded frames in flight: only 3 present waits in a whole run (CPU never runs
ahead), harmless, kept.
Memory at the kill (ph-rdr38): guest heap 4298 MB dirty, GPU (tag100) 1941 MB vs
1383 MB resource payload, native malloc band 784 MB, pool 576, hostlow 728, FEX
325. The game's OWN heaps: 1,320 texture heaps totalling 1,985 MB for ~280 MB of
sampled textures -> backing them 1:1 with MTLHeaps would ADD memory; rejected.
**ml1071:** `[malloc-zones] ml1071` per-zone census every ~60 s (who owns the
784 MB native band: LLVM from 4k shader compiles / Wine / runtime).
**ml1072 (installed):** runtime-owned 64 MB Metal PLACEMENT heaps for sampled
textures whose Metal size <= 2 MB (RT/DS/UAV/MSAA excluded): first-fit with
coalescing free lists, blocks returned only after the GPU passes the releasing
serial, residency follows the heap. Bridge gained heapTextureSizeAndAlign /
newPlacementHeap / newTextureAtOffset (unix calls 135-137; remote mode returns 0 ->
standalone fallback). Report line `ml1072 texture heaps: N (reserved, live),
placed, fallbacks, blocks awaiting GPU`. Expected saving: page-granularity +
per-allocation overhead on ~12.7k small textures (estimate 150-250 MB); the
census will say.

## §74 — ph-rdr39/40: ml1071 crash (mine), ml1072 WORKS (-180 MB GPU), still jetsam; VRAM knob A/B

- ph-rdr39: crash during desktop boot = ml1071's census vm_deallocate'd libmalloc's
  OWN zone table (malloc_get_all_zones returns it, not a copy); next malloc
  faulted (ml1054 backtrace named it). Fixed ml1073.
- ph-rdr40 (ml1073): the small-texture heaps work: 5 heaps (320 MB reserved,
  221 MB live), 27,849 textures placed, 0 fallbacks, 0 GPU errors; tag100 fell
  1941 -> 1762 MB (-180 MB). Jetsam anyway at the same cutscene point (8187 MB):
  guest 4058, pa 772 (of which libmalloc default zone alloc=287 MB, used 178), GPU
  1762, pool 544, hostlow 724, FEX 322. The 8960 MB guest reservation starts at
  0x73ffff0000, so much of "pa" is the GAME's heap too, not native code.
- Pushed `Documents/madeira-vram-mb.txt` = 1024 (was 1536) for the next run: RAGE
  sizes its streaming pool from the advertised VRAM budget; a smaller budget should
  lower both GPU residency and the game's staging memory. One-variable A/B on
  configuration; delete the file to restore 1536.
- ml1074 (installed): census prints the four dirtiest regions of the hostlow and
  pa bands with tag/prot, so those 1.5 GB can finally be attributed.
Next memory candidates by size: 128 MB code-buffer NOP prefill (~80 MB), libmalloc
287 MB (attribute by zone/owner), hostlow composition (RDR2 image backing dirtied
by the crack = up to 117 MB; TEB/stack region; Metal driver).

## §75 — ph-rdr41/42: VRAM budget A/B; the story-mode memory wall, stated plainly; ml1075

- 1024 MB advertised VRAM: the GAME refused ("out of memory, please reboot" is
  RDR2's own dialog) at 4.6 GB footprint -- below its floor for these settings.
- 1280 MB: accepted; cutscene ran longer; footprint 4.7 -> 7.9 GB then jetsam.
  Band curve across the cutscene: guest heap 2330 -> 4048 MB (the growth), GPU
  765 -> 1728 MB (upload buffers 813 MB of it), pool 592, pa 576 -> 929, FEX 335,
  compressed 1.4 GB. ml1074 attribution: hostlow's big pieces are Metal driver
  regions (128 + 83 + 16 MB, tag 100) and 44 MB of dirtied RDR2 image pages; pa is
  many small regions (libmalloc default zone alloc 235 MB, rest guest).
- The game queried the budget exactly ONCE (our value was cached), so a static
  smaller budget cannot make it trim mid-scene.
**ml1075 (installed, cap back at 1536):** the advertised budget becomes dynamic:
once the footprint passes (limit - 1.5 GB) it shrinks 1:1 with the excess, floor
768 MB, recomputed <= 4x/s; `[wmt] ml1075 video budget now ... (N queries so far)`.
DECISIVE READ: if N stays at 1-2, RAGE does not poll QueryVideoMemoryInfo and
the next lever is the budget-change NOTIFICATION EVENT (currently refused with
DXGI_ERROR_UNSUPPORTED because an event that never fires hung the game; firing it
on real changes is the correct implementation, `madeira-dxgi-budget.txt`=1 arms
the registration path in dxgi_adapter.cpp). If N is large and the heap still
grows, the game's own streaming (RAM-sized) is what grows -- then try
`madeira-totalphys.txt` = 2048 (memory notes say 4096 vs 1024 made no difference
on the VM loading screen; story streaming was never tested).
Honest state: our controllable overhead is ~1.2-1.5 GB (pool 592, FEX 335, malloc
235, driver ~230); the rest is the game (5+ GB incl. compression) on an 8.19 GB
limit. Story mode likely needs the game to be told a smaller world budget, which
is what ml1075 / the notification event / totalphys are for.

## §76 — ph-rdr43 (ml1075): the game POLLS the budget (930k queries) and does NOT trim

Decisive: QueryVideoMemoryInfo was called 931,000+ times in the run (~every frame,
often more), and the advertised budget stepped 1536 -> 1423 -> ... -> 1036 MB as
the footprint passed 6.7 GB, yet neither the game's heap (guest band 3958 MB) nor
its GPU usage (1462 MB, of which upload buffers 748 MB) fell. Jetsam at 8142 MB
at the same cutscene point. RAGE reads the budget but does not evict against it
mid-scene (its settings screen shows a fixed "1516 MB / 1516 MB" texture budget
computed at start). So the dynamic budget is NOT a lever; the notification event
is unlikely to be either (same information, different delivery). Untested: the
reported RAM (`madeira-totalphys.txt`, currently 4096) for story streaming, and
lower in-game streaming/LOD settings.

Where memory stands for a reviewer (all measured at the last jetsam, MB dirty):
game heap ~3960 (+ most of the 782 in the 0x74 band), GPU 1462 payload (upload
buffers 748, RTs 281, sampled 279 in runtime heaps, depth 88) inside ~1.7 GB of
tag-100, JIT pool 624, hostlow 754 (Metal driver ~230, image pages 44, TEBs/stacks,
app), FEX 331, libmalloc ~235; compressed 1.87 GB counted within. Limit 8192.
Our own reducible share is ~1.2-1.5 GB and the cutscene overshoots by more than
that unless the game itself holds less.

HANDED TO ASTRA at this point (user's decision, 2026-09-22 ~11:15). Open threads,
in priority order as I see them:
 1. Story-mode memory: test totalphys 2048 (one run); if no effect, the game's
    streaming settings are the remaining lever; then our fixed overhead (pool
    NOP prefill ~80 MB, FEX 331, malloc 235).
 2. Critical-thread profiler (host-pc sampling mapped through FEX block tails);
    prerequisite for selective TSO / native routines / crack-overhead decisions.
 3. Byte-store SMC loop (§68/§69): perform the store in the handler through Wine's
    writable route; fixes the deterministic menu-return crash and ~10k stalls/run.
 4. Tessellation (real terrain; patch draws currently dropped, ml1069).
 5. Pass merging (GPU is not the limiter now; thermal win only).
 6. MetalFX frame interpolation once there is GPU headroom.
Build state: everything since ml1034 is uncommitted; IPAs in build/ipa/; latest
installed ml1075. Device config: madeira-vram-mb.txt=1536, madeira-env.txt
(TSO on, oo2core/bink exempt), madeira-pool.txt=640, madeira-totalphys.txt=4096.

## §77 — ml1076 canary CONFIRMED on the phone; ml1077 = file-backed guest data tier

Canary (in-app, before Wine, ph-rdr44): anonymous 64 MB -> footprint +63 MB;
file-private (COW) 64 MB -> +64 MB; **file-shared 64 MB -> +0 MB** (external +64,
resident +64); **file-shared 512 MB -> +2 MB**; every byte verified after
MADV_DONTNEED; F_PUNCHHOLE on the first half -> word0 reads 0, resident -32 MB;
`os_proc_available_memory()` unchanged by the shared mappings. So on iOS, dirty
pages of a MAP_SHARED mapping of an unlinked temp file are NOT charged to
phys_footprint. Astra's mechanism holds on the device. Not yet shown: eviction
under pressure and its latency (the kernel's job; first game run will tell).

**ml1077 (installed):** opt-in via `Documents/madeira-swap-mb.txt` (= cap, pushed
3072). App creates `tmp/madeira-swap.bin` with NSFileProtectionNone and passes
MADEIRA_SWAP_FILE/_MB. In `virtual_ios.c`: a commit that is (a) writable,
non-exec, not guard/watch/copy-on-write, (b) in a valloc view (no SEC_FILE/IMAGE/
RESERVE/SYSTEM), (c) in the guest band [0x70..0x7c), (d) >= 8 MB, (e) entirely
FRESH (no page already committed) gets its 16 KB-aligned interior mmap'd
MAP_FIXED|MAP_SHARED from a first-fit extent of the sparse file. Hooks: the
reserve+commit path and the commit-on-existing path of allocate_virtual_memory;
decommit (mmap-over branch) and delete_view release extents (F_PUNCHHOLE => zero
on recommit, space reclaimed); set_protection with EXEC/WRITECOPY/GUARD copies a
backed range back to anonymous memory first. Sub-page edges stay anonymous.
Logs: `[swap] ml1077 ... ON`, `backed`/`released` (first 16 then 1/64), and a
`stats` line each monitor cycle. Kill switch: delete/zero the cap file.
Risks: frame-time stalls from flash paging when the hot set is file-backed;
write volume; a code path that executes from a backed range (handled by copy
back, unverified); MEM_RESET ignored. Canary gate now requires file content "1".

## §78 — ph-rdr45 (ml1077 first run): tier engages (886 MB backed at boot); boot stalled in OUR probe

The tier backed the game's first arenas (692 MB at 0x7038010000, then 886 MB in 5
extents) and the footprint sat at 2.5 GB with 371 MB "external". The desktop
then stalled at the end of shader-cache loading: thread 0x140eb in
`ios_verify_commit_zero` (the ml293 zero-on-commit PROBE) under virtual_mutex,
run_state waiting, walking all 692 MB -> ~44k sparse-file page-ins at ~2 ms each
(~90 s), while every other thread (incl. a fexlock holder in NtFreeVirtualMemory)
waited for the mutex. Not a deadlock -- a probe that became quadratic-slow on a
file mapping. ml1078: the probe skips ranges overlapping swap extents; the
"released" line prints only on real releases (the "copied back" lines in rdr45
were no-ops on image pages); first-touch cost of a sparse-file page is now
MEASURED at each of the first 8 backings (`[swap] ml1078 first-touch cost`).
That number decides whether the tier is usable: the game touches its arenas
lazily, and a multi-ms fault per 16 KB page would show as loading stalls.

## §79 — ml1079-1082: canaries clear the kernel; the tier's failures were ONE bug (prot=0 extents)

Canaries in-app on the phone: (ml1079) after dirtying 512 MB, first touches on a
fresh region of the SAME file cost 27-40 us/page and of another file 7-23 us --
no writeback stall. (ml1080) 1.5 GB dirtied through one shared mapping at
6-9 GB/s, then 2.6 GB/s; footprint FLAT at 672 MB while external rose to
1.58 GB; re-read 297 us. No throttle. The mechanism scales.

The tier's three failures had one cause: `get_unix_prot(vprot)` returns PROT_NONE
when VPROT_COMMITTED is absent, and the commit-on-existing hook passed the bare
flags from get_vprot_flags(). So the reserve+commit extents were RW and fine
(5 of them, 854 MB) and the FIRST commit-on-existing extent was mapped prot=0
(ph-rdr49: `region=0x760fff0000+0x2000000 prot=0 max=3 extpager=1`):
- rdr45/46: my first-touch loop read it while holding virtual_mutex -> Mach
  exception -> handler needs virtual_mutex -> wedge (also removed, ml1081);
- rdr49: the game touched it -> bus_handler healed pages one at a time
  (`[bus-reheal] re-applied prot=3`) until a JIT block's access was misdelivered
  to the guest as an AV -> crack VEH -> RIP 0x170 -> ml465 termination.
ml1082: extents mapped with `get_unix_prot(vprot | VPROT_COMMITTED)`. Tier ON
(3072), canary off. Unverified on device as of this note.

## §80 — ph-rdr50 (ml1082): STORY MODE PLAYABLE. The file-backed tier works.

User: first cutscene under 7 GB on the HUD, 30-40 FPS in the close-ups; then
free play; memory rose to ~7.6 GB and PLATEAUED; ~20 FPS riding at night in
snow; "performance does not seem to be affected by the swap". Crashed eventually
(low priority).
Measured: tier held 2191 MB file-backed (peak 2311) in 14 extents, 191 backings /
177 releases (recycling works, 0 refused, 0 exec unbacks); footprint plateau
7.2-7.4 GB with compressed 1.5-1.7 GB and external 340-650 MB; guest band dirty
3645 MB (was 4000-4600 at the old kills). ~9 minutes of story before the crash.
Crash: `GPU Hang` (one command buffer) then a NULL READ from JIT pc 0x161f5d2ec
redelivered 2000x (guest exception record: c0000005, 2 params, addr 0) -> ml465
termination: a game-side null dereference that the crack's VEH "recovers" in
place forever. Possibly downstream of the hang (a failed resource -> NULL);
unproven. Left open.
Tier knobs: `madeira-swap-mb.txt` (3072). Next for memory: raise the cap and lower
the 8 MB eligibility floor if the plateau creeps; watch `refused`.

## §81 — ml1083/84/85: TESSELLATION runs (hull+domain as an object/mesh pipeline); draw-params slot bug; cold-cache double compile

**Why.** The three rendering defects the user reported after §80 (cross-hatched
textures, objects popping in and out, the horse and much of the ground never
visible) share one measured cause: every PATCH-LIST draw was dropped (ml1050/
ml1069). ph-rdr50 counted **538,933 dropped tessellation draws** in one story
run across 26 pipelines. The pipelines the log names are exactly the ground and
character material set (depth-only VS 564 B + PS 0 B for the terrain shadow/
depth pass, VS 4 KB + PS 44 KB for the full material). The cross-hatch is the
LOD cross-fade dither with its tessellated partner draw missing; the popping is
the same partner draw missing per LOD switch.

**What ml1083 does.** Ports DXMT's D3D11 tessellation path into the D3D12
runtime, using the in-tree DXBC compiler's own mesh-shader emulation
(`SM50CompileTessellationPipelineHull/Domain`):
- `madeira_ir_abi.h`: `tess_stage` (1 = object VS+HS, 2 = mesh DS),
  `tess_index_format`, hs/ds bytecode, a second range set for the hull, and the
  tessellator facts (`ret_threads_per_patch`, `ret_tess_out_prim`,
  `ret_max_potential_factor`).
- `madeira_ir_unix.mm`: `mad_airconv_convert_tess`. The object function binds
  the VS's tables at 27/28 (re-homed from the reflection's 29/30), the hull's at
  29/30, VB table at 16, draw args at 21, index buffer at 20; the mesh function
  is the DS with its tables at 29/30. Both functions are compiled for the SAME
  max potential factor (from the DS reflection). Point/line tessellators are
  refused (DXMT does the same). Disk cache format bumped to v2 (carries the
  second range set + tessellator facts).
- `madeira_d3d12.c`: `struct mad_tess` on the PSO; `mad_tess_build` compiles
  the mesh function once and the object function per index format (0/u16/u32,
  eagerly, the object function is specialised on it exactly as DXMT's variants
  are), then `MTLDevice_newMeshRenderPipelineState` with DXMT's immutable
  masks. `exec_draw`: a patch-list draw on a built pipeline builds FIVE tables
  (VS, HS on object; DS on mesh; PS on fragment; VB table), writes the D3D draw
  arguments verbatim to object index 21 (start index in ELEMENTS, IB bound at
  its view base), and dispatches `drawMeshThreadgroups(grid = (ceil(patches /
  patches_per_group), instances, 1), object tg = (threads_per_patch,
  patches_per_group, 1), mesh tg = (32,1,1))`. Indirect tessellation draws stay
  dropped (need the dispatch kernel; counted). Census line now reads
  `tessellation: N pipelines (M built as mesh pipelines), D draws dropped, R
  drawn (X non-indexed)`, plus one `ml1083 tessellation pipeline built:` line
  per pipeline (first 12) and a `NOT built` / `FAILED` line per failure.

**ml1084 (bug found by reading, fixed in the same build).** exec_draw wrote
the 20-byte draw parameters at byte 512 of "the last ring slot taken"
(`ring_used - 1`). That is the argument slot only on the DXIL path; on the DXBC
path the table builders take up to five more slots after it, so the params
landed in the LAST table built: the VB table when the VS fetches vertices
(harmless, 256 B), otherwise the PIXEL ARGUMENT TABLE, whose qwords 64.. then
held {count, instances, start, ...} instead of texture handles. Any fullscreen
pass (no vertex buffers) with more than 64 qwords of PS arguments read garbage
handles. Fixed: the slot index is recorded right after `exec_arg_slot`.

**ml1085.** The shader disk cache stored an entry only on the FILL call, and
the PE's protocol is size-then-fill, so every cache miss compiled the shader
twice. Now stores from the compiler's bytes on the sizing call. This build
invalidates the cache anyway (new `__DATE__` stamp + v2 format), so the first
boot recompiles everything -- once, not twice.

**How to read the next run.** Look for `ml1083 tessellation pipeline built`
(count vs `NOT built`/`FAILED` with the Metal error), the census `drawn`
number, and `[madeira-d3d12] ml1008 '...': cannot bind` lines naming a hull or
domain range (a visibility or space-0 refusal on a stage we never bound
before). A GPU hang or "ENDED IN ERROR" right after the first `drawn` > 0 means
the emulation itself; check `threads/patch` and `max factor` on the built line.
Not yet verified on the phone. Installed as ml1085
(`build/ipa/Madeira-20260922-1324-ml1085.ipa`).

## §82 — ph-rdr51 (ml1085, benchmark): TESSELLATION CONFIRMED RUNNING; three more defects found and fixed (ml1086/87/88)

**ph-rdr51 (ml1085, benchmark run).** User: "fixed many textures/shaders", but
objects (Arthur included) still pop in and out, lots of invisibility, the horse
never appears, and daytime "bokeh balls" around lamps. Log:
- `tessellation: 28 pipelines (26 built as mesh pipelines), 0 draws dropped,
  64432 drawn (0 non-indexed)`; 0 command buffers in error. The port works.
- The two unbuilt pipelines FAILED CREATION entirely: `newRenderPipelineState
  failed (5 targets, depth 20, 9/4 input elements)` with Metal's "Fragment
  input(s) user(color1),user(color2),user(texcoord7),user(texcoord14)
  mismatching vertex shader output type(s) or not written by vertex shader".
  Both are hull/domain G-buffer materials (VS 4 KB, PS 18/32 KB): their PS is
  fed by the DOMAIN shader, so the plain VS->PS pairing can never match, and we
  returned E_FAIL for the whole PSO -> the game has no pipeline for those
  materials -> never drawn. Prime suspect for the permanently invisible horse.
- `ml1008 'mdc_09c4661714c29b33' wants a 140-qword argument table; the slot
  holds 136` -> 796 draws skipped (site L2590).
- Occlusion: 5 heaps of 8192 BINARY + 6 heaps of 2048 counting queries, all
  answered by the constant "1 sample, visible". A lens flare / light sprite's
  brightness is the occlusion fraction the game reads back, hence full-strength
  sprites through walls and in daylight.

**ml1086** — a hull/domain pipeline gets NO plain vertex pipeline; the PSO
succeeds when its mesh pipelines built (`!p->rps && !p->tess` is the only
failure now). exec_draw accepts a PSO with tess but no rps.

**ml1087** — `exec_ring_take_n`: an argument table larger than one 1088-byte
slot takes N consecutive slots (skipping to the next chunk when the tail is
short). A whole 64 KB chunk is the new ceiling.

**ml1088** — REAL OCCLUSION QUERIES, mirrored from DXMT's D3D11 design
(dxmt_occlusion_query.hpp / VisibilityResultOffsetBumpState):
- Every render pass carries a visibility result buffer (a 64 KB chunk from the
  arg-ring pool, owned by the BATCH = command buffer, 8192 slots) once a query
  heap exists, so a Begin mid-pass never forces a restart; crossing a chunk
  boundary does (counted: `pass restarts`).
- A draw with active queries counts into the current slot
  (`WMTRenderCommandSetVisibilityMode` counting, emitted only when the slot
  changes, placed FIRST in the draw's command chain); Begin/End inside an
  encoder that already counted into the slot start a new one; an encoder end
  bumps the slot. A query's value = CPU sum of slots [begin, end).
- At flush the batch's slots/queries/resolves move to the device's FIFO; in
  `mad_cb_retire` (which runs BEFORE the fence signal on the worker) all
  batches up to that command buffer are summed, values stored on the heap
  (`results[]`, per-heap lock), and every ResolveQueryData of the batch is
  written into the readback buffer's CPU mapping (binary -> 0/1). A resolve
  into a GPU-only buffer is logged and not delivered (4 lines max).
- Non-occlusion queries (timestamp, pipeline stats) still resolve to zero.
- Census line: `ml1088 occlusion queries: N begun, M results delivered, K
  queries counted samples > 0, R pass restarts`. If K stays 0 while N grows,
  the counting is not landing (mode/offset/buffer) and the scene will vanish
  the way ml889 described -- that is the failure signature to look for.

Installed as ml1088 (`build/ipa/Madeira-20260922-1422-ml1088.ipa`).
Not yet verified. Indirect tessellation draws and GraphicsCommandList1
(depth bounds / sample positions) remain unimplemented.

## §83 — ph-rdr52 (ml1088, benchmark): queries and tessellation confirmed; flicker traced to the min-LOD clamp; the freeze is the fault-loop breaker

**ph-rdr52 facts.** `ml1088 occlusion queries: 63940 begun, 253802 results
delivered, 29956 counted samples > 0, 0 pass restarts` -- counting lands.
`tessellation: 28 pipelines (28 built), 0 dropped, 28418 drawn`; `skips: none`;
0 command buffers in error. The user still sees: objects (Arthur too) flashing
invisible for a split second several times a second; small areas flashing
extremely bright white at night; oversized blocky "bokeh" light sprites; then
a freeze with memory climbing to 7.4 GB.

**Flicker / white flashes / blocky sprites -> ResourceMinLODClamp (ml1089).**
The descriptor carried the clamp in the metadata word exactly as DXMT lays it
out (`array_length << 32 | float bits`), but the DXBC compiler never READS
that word when sampling (`dxbc_converter_basicblock.cpp` has no use of
`metadata`; DXMT's D3D11 layer always passes 0). So a streamed texture whose
top mips are not resident yet was sampled from those mips: uninitialised heap
memory in placed textures (stale white/garbage, "bright cutouts"), zeros in
committed ones (alpha 0 -> the object cuts out), flipping as the camera moved
the sampled mip. RDR2 streams mips this way (vkd3d-proton grew
VK_EXT_image_view_min_lod for the same reason). ml1089 folds the clamp into
the Metal texture VIEW (first mip = ceil(clamp)); implicit-LOD sampling then
gives exactly the clamped result. Explicit mip indices (SampleLevel/Load,
GetDimensions) see a shifted numbering -- the compiler-side `min_lod_clamp`
sample argument is the complete fix, later. Census on the ml1088 line: `SRVs
with a min-LOD clamp folded into the view: N (largest clamp X)`. If N is 0
the theory is dead and the flicker is something else.

**The freeze.** `[fault-stuck] BREAKING LOOP ... diverting thread to
abort_thread` fired 22 times: pc 0x71f88713e8 inside the CHILD's private
ntdll (`[ec-child-ntdll] mapped at 0x71f8810000`, pool alias 0x155e18000)
executed through its non-executable backing and NOT translatable any more
("not a known PE-image translation"), although the same pc redirected fine
3873 times in ph-rdr51 and 87 times earlier in this run. Threads 00ac/00b0
had already died ("SEGV LOOP FATAL pc=0x168ff4660 addr=0x0"), i.e. the child
pseudo-process's translation state was torn down while threads still ran its
code. Same family as the menu-return crash. NOT fixed. The 426,000 region-map
dumps (3 M log lines) were the memory climb; ml1090 caps the dump at 6 per run.
Also visible: `[exec-redir] 0x71f7f43d98=71553` per 100k window -- one hot
native call through a non-exec backing, a Mach exception per call; fix at the
source (which pointer is it?).

**Not done, candidates if ml1089 is not it:** placed-texture block release
uses `d->gpu_serial` (last COMMITTED serial); a texture last used by the still
OPEN batch could be reclaimed one batch early (`mad_hp_release`, use
gpu_serial+1 or the open batch's future serial). GraphicsCommandList1 QI is
refused 23x (depth bounds / sample positions). Indirect tessellation draws.

Installed as ml1090 (`build/ipa/Madeira-20260922-1444-ml1090.ipa`, ml1089
runtime + ml1090 ntdll). Unverified.

## §84 — ph-rdr53 (ml1090, benchmark): pulled, NOT analysed (handed to Astra)

Log: `research/logs-rdr2/ph-rdr53.txt` (10 MB, 96,840 lines). User report:
flicker / pop-in-out still happening, though it seemed fixed in the first snow
scene; NO white cutouts this time; some geometry possibly better. One fact read
off the census line only: `ml1089 SRVs with a min-LOD clamp folded into the
view: 0 (largest clamp 0.0)` -- RDR2 never sets ResourceMinLODClamp, so the
§83 min-LOD theory is DEAD for the flicker; ml1089 is inert here. The white
cutouts disappearing needs its own explanation (run-to-run variance is
possible). Queries: 93,816 begun, 45,426 nonzero. Everything else in this log
is unread.

## §85 — ph-rdr53 analysed + Astra's review acted on: ml1091 fence chain, ml1092 ordered queries, ml1093 sampler axes, ml1094 zero-instance draws

**ph-rdr53 (ml1090, benchmark).** Clean run: 28/28 tessellation pipelines,
30,944 tess draws, 0 skips, 196,500 command buffers retired with 0 errors,
93,816 queries / 45,426 nonzero, footprint peak 5.9 GB, no fault storm (the
ml1090 cap held: 2 region dumps). `ml1089 SRVs with a min-LOD clamp: 0` --
RDR2 never sets ResourceMinLODClamp; §83's flicker theory is refuted (ml1089
stays, it is correct and inert). User: flicker/pop-in still present, though
absent in the first snow scene; NO white cutouts this time; geometry possibly
better. ClearState was called once (unimplemented stub); one 8-sample PSO ran
at 4 samples (ml1030).

**Astra's review (RDR2-RENDERING-REVIEW-ph-rdr53-2026-09-22.md), verified
against the source, all three confirmed:**
1. Synchronization: `list_ResourceBarrier` discarded every barrier and
   ml1060 dropped the per-draw useResource lists once nsrv > 256. Metal tracks
   hazards only for resources an encoder declares DIRECTLY; a residency set
   makes memory accessible, not finished. Every texture, UAV and DXBC vertex
   buffer is reached through argument tables, i.e. undeclared: a compute skin
   or an upload could still be running when its consumer started.
2. Queries: `mad_vis_complete` applied every query value before any resolve,
   so "End Q7 (37) / Resolve / reuse Q7 End (0) / Resolve" delivered 0 twice.
   Also two retirers could publish batches out of order.
3. Sampler axes: U was on r and W on t (Metal s/t/r = D3D U/V/W).

**Shipped (ml1094 build):**
- ml1091 ENCODER FENCE CHAIN: one MTLFence per device; every render/blit/
  compute encoder waits on it at creation (render: before Vertex|Object) and
  updates it at its end (render: after Fragment). Clear-only passes are in the
  chain. ResourceBarrier is now RECORDED (MC_BARRIER, consecutive calls fold);
  at replay it closes an open compute or blit encoder (render passes close on
  target changes) so the fence orders what follows. Conservative: every
  encoder boundary is a full barrier; overlap between passes is lost. Watch
  the HUD GPU time. Census: `ml1091 encoder fence chain: N waits, M updates;
  K ResourceBarrier calls recorded`.
- ml1092: End and Resolve events carry a program-order sequence; a query's
  value is published at its End, in order with the resolves; a `vis_pub_lock`
  serialises retirers so batches publish in commit order. NOT done: a GPU
  consumer of a resolved buffer (a later copy in the same stream) can still
  run before the CPU write; RDR2 reads occlusion on the CPU.
- ml1093: sampler s/t/r <- U/V/W.
- ml1094: InstanceCount 0 draws nothing (was promoted to 1). Census:
  `zero-instance draws elided N`.
Not done from the review: explicit-zero SampleMask preservation, ClearState
and the initial-PSO argument of Reset/CreateCommandList, real 8x MSAA, the
placed-texture release serial (+1), indirect tessellation draws.

Installed as `build/ipa/Madeira-20260922-1516-ml1094.ipa`. Unverified. If flicker persists WITH the fence chain, the
producer/consumer story is not it either; next is Astra's item 5 -- capture
the horse's actual draw (post-transform output, depth) and the light-sprite
pass, not another census.

## §86 — ph-rdr54 (ml1094, benchmark): pulled, NOT analysed (handed to Astra)

Log: `research/logs-rdr2/ph-rdr54.txt`. User report: flicker FIXED in the
forest scene. In the Saint Denis Arthur-running scene: a huge amount of
geometry completely invisible including the ground much of the time, lots of
flickering, slow pop-in of building geometry, Arthur invisible much of the
time, horses invisible all the time; bokeh balls unchanged. Nothing in this
log has been read.

## §87 — ml1095: ONE config file, Documents/madeira.cfg

All 33 runtime switches that each lived in their own Documents/madeira-<key>.txt
now come from ONE file, `Documents/madeira.cfg` (`key = value`, `#` comments,
last line wins). Keys are the old file names without `madeira-`/`.txt`.
Environment exports are `env.NAME = value` (was madeira-env.txt); DXMT options
are one line, `dxmt = a=b;c=d` (was madeira-dxmt.txt). Example with the
current phone values: `research/madeira.cfg.example`.

Reader: `build/madeira_cfg.h` (header-only C, used by ntdll unix, wineserver,
madsync, the winemetal bridge, the shader converter and the ObjC bridge) and
`app/Madeira/MadeiraConfig.swift` (same rules). Semantics: when madeira.cfg is
PRESENT the legacy files are ignored entirely; when ABSENT the legacy files
still apply (nothing regresses for a device without the new file), and the app
writes madeira.cfg from them once at launch (`MadeiraConfig.migrateLegacy`,
logged) so they can then be deleted. The launch log prints the whole effective
config: `madeira.cfg: key=value ...`.

Untouched, not config: madeira-log.txt / .prev.txt (logs), madeira-input.json
(input map), madeira-retire-trace.txt (trace OUTPUT), madeira-d3d12-canary.log.
Rebuilding the converter object invalidated the shader disk cache again (the
__DATE__ stamp), so the first boot of ml1095 recompiles every shader once.
Installed as `build/ipa/Madeira-20260922-1619-ml1095.ipa`; madeira.cfg pushed
to the phone with the values that were in the separate files.

## §88 — ml1096/ml1097: legacy config files removed at launch; BAD POOL on every ml1095 launch

ml1096: once madeira.cfg exists the app deletes the legacy madeira-*.txt switch
files at launch (devicectl cannot delete container files). ml1095/96 then
failed EVERY launch with "BAD POOL placement 0x7000000000". Log (ph-cfg1):
the image-load constructor could not claim the executable window --
`ml1034: could NOT reserve the executable window (kr=3)`; the hole census
showed 0x140000000+88MB occupied and the free run starting at 0x145800000
(663 MB) merged with the released 608 MB placeholder; the ml1040 "plug lower
holes" rule then plugged THAT hole (its base is below the placeholder base),
leaving nothing >= 640 MB, so the debugger's first-fit fell into the guest
window. Two changes (ml1097): plug only holes that END below the placeholder,
and only when the window is held; and on a failed window claim log the
occupant (`ml1097: occupant of 0x140000000: region ... user_tag=... image=...`).
The occupant itself is NOT understood: the debug dylib grew by 29 KB
(MadeiraConfig.swift) and the frontier moved from 0x125400000 to 0x11f758000,
so something 88 MB that used to sit elsewhere now lands at 0x140000000 at
image load, 1 GB aligned, i.e. not first-fit. If the window stays lost, a
fixed-base main image (RDR2) will be displaced; read the ml1097 line first.
Installed `build/ipa/Madeira-20260922-1635-ml1097.ipa`.

## §89 — ml1098-1101: manual frame capture (CAP button), barrier-render toggle, copy offsets, stencil SRVs, query flush order

Astra's ph-rdr54 review, all four defects verified in the source and acted on:

- **ml1098 FRAME CAPTURE.** A cyan `CAP` pill in the FPS overlay (both
  layouts) calls `madeira_capture_request(1)` (winemetal_unix.c). New bridge
  slot 138 `MadeiraCtl` (op 0 poll, op 1 write Documents/capture/<name>,
  op 2 madeira.cfg get) is the PE runtime's one door into the sandbox. At the
  next Present the runtime arms the following frame: every render pass that
  encoded >= 1 draw is followed by a blit encoder (in the fence chain) that
  copies each colour attachment, the depth aspect and the stencil aspect
  (new `options` field on wmtcmd_blit_copy_from_texture_to_buffer,
  MTLBlitOption bits) into a shared buffer; MSAA, 3D and compressed targets
  are skipped; 384 MB budget per frame. At the Present after that the buffers
  are written as
  `f<frame>_e<enc>_<rt|depth|stencil><idx>_<W>x<H>_pf<metal>_dx<dxgi>_<name>.raw`
  and the log says `[capture] frame N complete: K files`. The draw-dump is
  forced on for the captured frame (its `enc#` matches the file names) with
  the dump counter reset. Convert on the Mac:
  `python3 build/tools/capture-to-png.py /tmp/capture` (pull the directory
  with devicectl first; index.txt lists images in pass order).
- **ml1098 barrier-render.** `barrier-render = 1` in madeira.cfg makes
  MC_BARRIER close an open RENDER pass too (Astra's conservative experiment);
  the census counts `closed a render pass`. Default off.
- **ml1099** `mad_vis_flush` moved under fence_lock before commit, so the
  query pending list is in serial order.
- **ml1100** CopyTextureRegion: buffer<->texture pitches come from the whole
  footprint; a source box moves the source address (front*imagePitch +
  top/block*rowPitch + left/block*bytes) and only shrinks the extent; DstX/Y/Z
  of a texture->buffer copy is an offset into the footprint.
- **ml1101** stencil SRVs: X24_TYPELESS_G8 / X32_TYPELESS_G8X24 on a
  depth-stencil texture become an X32_Stencil8 view with G <- Metal's R.
  Counted (`stencil SRVs N`).

Installed as `build/ipa/Madeira-20260922-1653-ml1101.ipa`. Unverified.

## §90 — ph-rdr55: SIX FRAME CAPTURES. The missing geometry is missing IN THE G-BUFFER, with identical draws; the survivors are screen-door dithered

Log `research/logs-rdr2/ph-rdr55.txt`; key images `research/captures/*.png`
(full set: 1,373 raw files / 1.5 GB in /tmp/capture on the Mac, PNGs in
/tmp/capture/png, index in `research/captures/ph-rdr55-index.txt`).
Frames: 5714 snow (good), 10257 night camp (bokeh), 13723 robbery start
(invisible assets), 13931 robbery (visible), 14319 invisible horse, 14996
black ground. Draw-dump enc# == capture file enc#.

**Findings (measured):**
1. In the "invisible" robbery frame the walls, floor, shelves and most props
   are ALREADY ABSENT from the G-buffer albedo (`albedo-invisible.png`,
   enc#909737) -- not lost in lighting. The props that are present are drawn
   with a SCREEN-DOOR DITHER (checker/cross-hatch): the game's fade-in/out.
   In the "visible" frame (`albedo-visible.png`) everything is solid.
2. The two frames submit the SAME DRAWS: G-buffer passes 212/54/61 vs
   213/56/60 draws, identical pipeline multiset (one extra pipeline in the
   invisible frame), identical index-count distribution. So the geometry is
   drawn every frame and its pixels vanish: this is DATA (per-instance fade /
   transforms / streamed asset state), not a dropped draw, not a lost pass,
   not lighting.
3. Horse frame (`albedo-horse.png`): the horse BODY is absent, the saddle,
   reins and tack are present; buildings on the right are dithered or gone.
4. Black-ground frame (`albedo-ground.png`): the road/ground is absent from
   the albedo while trees and houses are solid.
5. Night camp (`lights-bokeh.png`, the RGBA16F light pass enc#746811): the
   lamp sprites are large discs drawn with the SAME dither pattern, i.e. they
   too are mid-fade objects. Their size is the game's choice.
6. The forest/snow frame is solid everywhere.
7. Depth/stencil captures were all zero: the bridge's BC-pitch check skipped
   the aspect copies (fixed, ml1102). Not a rendering fact.

**Reading.** Everything in the city that pops is in a FADE state, and the
game fades an entity in when its model/texture data STREAMS IN and out when it
is evicted. The game's own settings screen shows "1516 MB / 1516 MB" -- it is
at its streaming budget (we advertise vram-mb 1536; it queries the budget
ONCE, so ml1075's dynamic trim is inert). A dense city over budget = the
streamer loads and evicts continuously = objects fade in and out several
times a second, big models (horse body, buildings, road) never fully arrive.
The forest fits the budget. Our actual sampled-texture memory is ~300 MB, so
the game's pool is accounting-limited, not memory-limited.

**Experiment in flight (no code):** `vram-mb = 3072` pushed to the phone. If
the city pop-in and the horse improve, the fix is real budget headroom
(totalphys 4096 also shapes RDR2's CPU-side streaming pools; second lever).
If nothing changes, next discriminators: capture the same object's draw
constants (the fade value) across two frames, and Astra's binary-occlusion
"force visible" run.

Leftover: `madeira-dxgi-budget.txt` in dxgi_adapter.cpp is still a legacy
file read (PE side, effectively always off) -- fold into madeira.cfg via
MadeiraCtl op 2 when touched. Installed `build/ipa/Madeira-20260922-1724-ml1102.ipa`.

**§90 RESULT (user, same session): `vram-mb = 3072` DOUBLED the snow-scene
frame rate to ~40 FPS.** The streaming thrash was a large share of the CPU
frame (continuous Oodle decode + re-upload of evicted assets). City result and
footprint pending. Next levers: vram-mb 4096, totalphys 4096 -> 6144/8192 (the
game's CPU-side streaming pools), one at a time, watching the 8192 MB line.

## §91 — ph-rdr56 (vram-mb 3072): snow doubled to 40 FPS, city unchanged; the ml1075 trim took the budget back

Log `research/logs-rdr2/ph-rdr56.txt`. With `vram-mb = 3072`: snow scene ~40
FPS (was ~20). City: same pop-in, horse invisible, same frame rate. Footprint
peak 7288 MB. The game now polls the budget continuously (114k queries this
run, vs "1 query" at the first snow census -- that count was just early), and
ml1075's dynamic trim, which starts 1.5 GB below the kill line, cut the
advertised budget from 3072 to 2625 MB exactly as the footprint passed 6.7 GB
in the city: `video budget now 3004 (foot 6723) ... 2625 (foot 7102)`. So in
the city the game was told to shrink its pool and went back to evicting.
Whether the city also needs MORE than 3 GB is still open: RDR2's pool demand
follows its Texture Quality setting; ask what the game is set to.

ml1103 (installed, `build/ipa/Madeira-20260922-1746-ml1103.ipa`): two
madeira.cfg switches, defaults unchanged:
- `vram-trim-mb = N`: the trim starts N MB below the kill line (default 1536;
  0 = never). Pushed to the phone as 512 for the next run.
- `occlusion-visible = 1`: every occlusion query answers fully visible
  (binary 1, counting 2^20) -- Astra's discriminator for "queries are
  fading the objects". Off by default.
One capture taken on the invisible horse this run (frame 12234, 211 files,
pulled to /tmp/capture56). Not yet examined.

## §92 — ph-rdr57: the "trim 512" run never happened (config landed late); the horse is absent from DEPTH too

ph-rdr57 ran with the DEFAULT trim (the config push was queued behind a
capture pull): `ml1103 video budget trim starts 1536 MB`. The budget then
OSCILLATED every few hundred ms between 2877 and 3072 MB as the footprint
hovered around 6.7-6.9 GB (10 budget changes logged, 76k polls), and the user
saw 1-3 FPS with hitching in the city, footprint peak 7512 MB, compressor
1.1 GB. A budget that moves every poll makes the streamer evict and reload
in a loop -- worse than a stable smaller budget. Next config pushed and read
back: `vram-mb = 2304`, `vram-trim-mb = 0` (stable budget, ~770 MB below the
run that hitched), plus `occlusion-visible = 1` (the query discriminator).

Horse capture from ph-rdr56 (frame 12234; `research/captures/albedo-horse2.png`,
`depth-horse2.png`, depth readback now works after ml1102): the horse BODY
is absent from the albedo AND from the depth buffer; the tack, Arthur and
the horseshoes write depth. So the body's draw rasterises nothing (no
coverage) or discards every fragment -- not a lighting or texture issue.
The road is present in this frame (it was absent in 14996): intermittent.

ml1104 (installed, `build/ipa/Madeira-20260922-1804-ml1104.ipa`):
draw-dump lines now carry `topo=N` and `TESS` (ran through the mesh
pipeline) / `tess-pso` (a hull/domain PSO drawn with a non-patch topology),
so the next capture can say whether the missing bodies/ground are the
tessellated draws. Suspicion: the horse body and the terrain are tessellated
(fur / displacement), and the emulated tessellation output is empty for some
patches (factor 0? wrong control-point count?), while the snow terrain works.

## §93 — ph-rdr58: occlusion and budget RULED OUT for the invisibility; the 1 FPS collapse is the JIT pool tail capped at 16 MB. HANDED TO ASTRA.

Log `research/logs-rdr2/ph-rdr58.txt`; capture frame 12781 (224 files, pulled
to /tmp/capture58, key images `research/captures/*horse3.png`).

**Config that applied (verified in the log):** `vram-mb 2304`, `vram-trim-mb 0`
(budget stayed 2304 all run, the game's settings panel showed 2300),
`occlusion-visible = 1` (every occlusion query answered fully visible).
Result: invisibility/pop-in in the city UNCHANGED. So it is NOT the video
budget (stable 2304 and 3072 both fail; 1536 also failed) and NOT the
occlusion queries (forced visible, no change). occlusion-visible is back to 0
on the phone. Tessellation is not it either: the captured city frame has
ZERO patch-list draws (1816 trilist + 384 tristrip), while the horse body and
the ground are still absent from albedo AND depth (`albedo-horse3.png`); the
tack, Arthur and the horseshoes render. The horse body's draw therefore
rasterises nothing: wrong vertex data (skinning / vertex-fetch through the
DXBC vertex-buffer table) or full fragment discard. Identifying WHICH draw is
the horse in the dump is the next step (its VS is a skinning shader with a
large index count; compare against a frame where Arthur -- also skinned --
renders; look for a draw whose vertex buffers come from a UAV/compute output
or whose stride/format the ml1011 fetch table might mis-decode).

**The 1 FPS collapse (confident):** JIT code-buffer thrash. The pool was
placed at 560 MB (ml1036 shrink, placement luck; 480 and 592 in the two runs
before). Its HEAD holds 305 MB of PE image code copies (last entry
SOCIALCLUB.dll, `used=0x130c0000/0x23000000`). The tail already holds 176 MB
(`tail_resv=0xb00c000`) as ELEVEN 16 MB fragments on the ml438 free-list
(`tail REUSE free_left=10`). ml1052's cap = largest power of two with
2*cap <= pool - head - tail - 48 MB = 31 MB -> 16 MB, so every FEX request
for a 128 MB buffer is refused (`tail CAP: refusing 0x8000000 (cap
0x1000000)`) and halves down to 16 MB; FEX then rotates that buffer
continuously: 206 `tail FREE` / 166 `tail REUSE` in the city segment,
3.06 M real compiles (a good run has ~0.8 M), hit rate falling 98 -> 91 %,
HUD frame time 971-1000 ms from present ~13170 on. ph-rdr57 (4.08 M
compiles, cap 16 MB) was the same collapse. The snow scene is early, before
the head and the fragments fill the pool.
Fix direction (not done): (1) coalesce adjacent free tail ranges so a 64-128
MB buffer can be served from the 11 fragments; (2) let FEX keep ONE large
buffer rather than rotate 16 MB ones; (3) shrink the head -- 305 MB of image
copies is .text sharing territory (memory index: "LESSON: .text-SHARING is
the real memory fix"); (4) make ml1036 place a full 640 MB pool reliably.
`pool = 896` in madeira.cfg will not help while placement shrinks it.

**Also seen:** `[exec-redir] 0x71f7f43d98 = 69k per 100k` -- one hot native
call through a non-executable backing (a Mach exception each); 926
UNALIGNED-BACKPATCH lines (STLR/LDAR unaligned emulation).

## §94 — ml1105: THE VERTEX-BUFFER TABLE WAS OVERWRITTEN BY D3D SLOT 10 (Astra, from the captures)

exec_draw bound every bound D3D vertex buffer at Metal index 6 + slot for
BOTH compilers. The DXBC backend fetches vertices through its table at index
16, so a buffer in D3D slot 10 (6 + 10 = 16) replaced the table -- and slot
10 is often stale IA state from an earlier draw that the current shader does
not read. Capture evidence (Astra's ph-rdr58 review): invisible robbery frame
194 of 212 G-buffer draws had slot 10 bound, the visible frame 0 of 213, the
horse frame 106 of 133. ml1105 restricts the raw 6+slot bindings to the MSC
(DXIL) backend and declares the table-pulled vertex/index buffers as used on
the DXBC path. No shader-cache impact. Installed as
`build/ipa/Madeira-20260922-2041-ml1105.ipa`; settings unchanged
(vram-mb 2304, trim 0, occlusion-visible 0) so this run isolates the fix.
Astra's other notes: the bokeh discs come from one indirect draw in encoder
746962, not the light pass 746811; `free_left` in the jit-pool log counts all
tracked ranges, not free ones -- the 128 MB buffer is outstanding and only
48 MB is free, so coalescing alone does not recover it.

## §95 — ph-rdr59 (ml1105): INVISIBILITY FIXED. Remaining: lamp halos (one indirect sprite draw), JIT rotation storm, pop-in, 20 FPS

User: "All invisibility is fixed now." Log `research/logs-rdr2/ph-rdr59.txt`,
two captures (14091, 15319), images `research/captures/halo-*.png`.

**Halos / muzzle-flash discs.** Located exactly (Astra's lead confirmed): ONE
indirect draw, `enc#1828899 vs='mdc_45d8d56225db606b' ps='mdc_1a16346dab3634c7'`
on the RGBA16F HDR target, `vb=[0: stride 20, 4 MB buffer] INDIRECT args-off=0`,
no depth test, blending. `halo-before.png` -> `halo-after.png`: it adds a flat
grey disc with a darker centre at EVERY lamp, in daylight, unlit lamps
included. On Windows these corona sprites are scaled by the light's intensity
and its occlusion-query fraction and the visible-light list is GPU-built. The
draw is INDIRECT, so its instance count comes from a GPU buffer; suspects, in
order: (1) the visible-light list / its count is produced by a compute pass
(RDR2 created NO UAV counter resources this run: the "counter resources are
ignored" line never printed, so it is a plain UAV write + indirect args);
(2) the per-sprite intensity constant (fade to 0
in daylight) is not what the shader reads; (3) the corona texture sampled
flat (the disc has no radial falloff). Next capture target: that draw's
indirect args (instance count), its vertex buffer contents, and the compute
dispatches that produced them, at the right timeline point.

**The hitch (present ~13900-14100, 400-900 ms frames):** JIT code-buffer
rotation storm again. Compiles went 258k -> 2.37M between log lines 88887 and
95060 (2.1 M recompiles in ~90 s), 127 `tail FREE` rotations, `tail CAP:
refusing 0x8000000 (cap 0x2000000, tail_resv=0xb00c000)`: pool placed at
592 MB, head 305 MB used (417 MB of image copies logged over the run, of
which 240 MB are UNNAMED "?" entries -- identify them: the exe, or anon JIT
regions copied as images?), tail 176 MB outstanding, so FEX gets 32 MB
buffers and rotates them. It STOPPED at line ~95060, one line before the
user's capture -- almost certainly the code working set settling, not the
capture, but it is noted. This is the city frame-rate ceiling: fix the tail
(let FEX keep its 128 MB buffer; free the outstanding one; shrink the head)
before any other perf work.

**Pop-in when moving fast:** streaming throughput -- Oodle decode on the
emulated CPU + our CPU load. Expected at this stage; the levers are CPU time
(the JIT storm above, the 69k/100k exec-redir hot call at 0x71f7f43d98) and
decode (native Oodle would be a large win: it is a self-contained DLL).

**Memory this run:** footprint peak 6916 MB with vram-mb 2304 / trim 0; the
swap tier still 2.2 GB of 3 GB. Story mode with 2304 is untested.

## §96 — ml1106: sweep retry for pinned code generations; halo input capture; redirect caller census

From Astra's ph-rdr59 next-fixes (all three items acted on):

1. **JIT rollover (FEX CPUBackend.cpp, `IosMaybeSweepCodeBuffers`).** The
   sweep ran once per generation and marked it swept even when it skipped
   threads (in simulation / signal-pinned / raced), so a thread that then ran
   only cached code kept the outgoing 128 MB buffer pinned (prev_use_count=63)
   while every successor was refused to 16-32 MB (the storm). Now: while the
   last pass skipped anyone, the same generation is re-swept every 16th
   trigger, at most 256 times. Threads pass through native side on every
   syscall, so retries catch them. Log: `[gen-sweep] ... retry=1 left=N
   rev=ml1106`. Acceptance per Astra: no extended small-buffer rotation, real
   generation sizes (`tail EC_CODE ... size=`), compiles/sec, frame-time
   percentiles, uncaptured run. MAX_CODE_SIZE left at 128 MB; the 64 MB
   policy is the fallback experiment if the retry alone is not enough.
   IosJitAlias.cpp now logs when IosAliasEntries (256) is FULL and refuses an
   image (that would make every call into it a fault-redirect).
2. **Precise-time redirect.** The x64 kernel32 export is a forwarder
   (`ldr x16,[__imp_]; br x16`, aarch64-windows/kernel32.dll RVA 0x3bea4) into
   kernelbase; the guest's kernelbase (0x71f7f20000) IS pool-copied (jit-pool
   image line 4399) but ExitFunctionEC's inline table walk did not translate
   it, so each call faults. Whether that is the alias table being FULL or the
   push callback missing the image is what the new log line decides. The
   exec-redir census now prints the CALLER: `pc(lr ...)=count` (ml1106,
   signal_arm64_ios.c). No translation change yet: Astra's "exact image
   ownership, canonical guest pointer identity" caveat stands.
3. **Halo capture.** madeira.cfg `dump-shaders = mdc_45d8d56225db606b,
   mdc_1a16346dab3634c7` writes the original containers to
   Documents/capture/shader_<name>.dxbc before the cache lookup (unix
   converter); `capture-ps = mdc_1a16346dab3634c7` makes the first four
   draws with that pixel shader in a CAP frame copy their inputs BEFORE
   running (all bound vertex buffers 64 KB, the index buffer, the indirect
   args, every texture the pixel stage samples at mip 0, kinds vb/ib/iargs/
   tex in the capture file names) and log their resolved argument tables
   range by range (`[capture-draw] t0 -> view ... ; b0 ... va=`). The
   draw-dump now carries `blend='rt0:en= src= dst= op= wm= ... mtl:...'`
   (D3D12 and Metal blend state). Both switches are set on the phone; one CAP
   tap in the night camp gives everything Astra's §2 list asks for except
   the offscreen replay.

Installed `build/ipa/Madeira-20260922-2223-ml1106.ipa` (xtajit64.dll,
ntdll unix, converter, runtime all rebuilt). Settings otherwise unchanged
(vram-mb 2304, trim 0, occlusion-visible 0).

## §97 — ph-rdr60 (ml1106): the lamp halo is the game's DEPTH OF FIELD given a zero depth at the flames

Run: benchmark, no hitch (FEX real_compiles 411,328 for the whole run vs
2.1 M in 90 s before; `[gen-sweep] retry=1` fired ~5 times per generation).
`[jit-alias] FULL` never printed (alias table is NOT the reason kernelbase's
GetSystemTimePreciseAsFileTime is untranslated). Redirect census with callers:
`0x71f7f43d98(lr 0x15611adf0)` = 70 % of 500 k faults; lr is JIT-pool code.

**Halo draw decoded** (`build/tools/dxbc-disasm.py`, new; containers in
`research/captures/halo-dof/`). VS `mdc_45d8d56225db606b`: one quad per tile
from a tile LIST in t0[42] (packed 16:16 tile coords, SV_InstanceID), i.e. the
DoF composite runs only on tiles the classifier flagged. PS
`mdc_1a16346dab3634c7` = depth-of-field composite:

    lin   = cb.z / (cb.w - depth(t32) + 1)          -- linear depth, metres
    d     = min(lin, t33)                           -- t33 = R16F 960x540 "transparent depth"
    coc   = |d - focus| / d * k ... clamp(0, 23)    -- focus from structured t5[40]
    alpha = smoothstep( lerp(coc, tileCoC(t4[38]), mask(t3[35])) / clamp(2*cb12.w, .5, 2) )
    out   = (halfres colour t2[34], alpha)          -- alpha blend over the HDR scene

Captured inputs (frame 10594, night camp): t33 is +inf everywhere except the
smoke plumes (8700-9984) and **exactly 0 at the flame sprites and the lit
windows** (202 pixels). d = 0 -> coc = |0 - focus| / 0 = inf -> alpha 1 -> the
tile classifier (20x12 buffer t38: G = tile min depth = 0, R = CoC = 13 in the
lamp tiles) and the half-res blur already contain a 26 px-radius disc per
lamp with the gather kernel's lattice visible (sample spacing >> source size).
The composite then paints that disc over the sharp lamp: flat grey disc, dark
centre, exactly what the user sees, day or night. Everything downstream of
t33 behaves per its own math; the defect is the value written INTO t33.

t33 is rt1 of the 5-MRT forward/transparent pass (enc#1070885: f10 HDR, f54
R16F, f61 R8, f29, f34; independent blend). Its writers with a non-zero rt1
write mask are the emissive sprite draws: vs `mdc_32c7316b3e054b41` with ps
`mdc_b256c32873e2309f` / `mdc_a5f25e6db51ccaf6` / `mdc_c47583f9895b9c11` /
`mdc_bf93a19241eae4ab` (flames, glass). Smoke (`mdc_628924096bfd3b1e`) leaves
rt1 masked (wm f0000f) yet the plumes hold ~9000 -- to explain as well.
Candidates, undecided until the shader is read: (a) the PS writes a depth
computed from something we bind wrong (cb value, SV_Position.w -- converter
DOES invert w, checked in dxbc_converter_basicblock.cpp init_input_reg);
(b) rt1's own blend (MIN with dst = inf is the usual idiom; if the PSO
declares something we mistranslate, e.g. src*0 + dst*0 on inf = NaN and a
NaN-to-zero somewhere); (c) the PS does not declare o1 and Metal writes 0 to
an attachment the D3D driver would leave alone (write mask f but no output).
NOT a per-game fix candidate: whichever it is, it is a general accuracy gap in
blend / output / depth handling.

**ml1107** (installed, `build/ipa/Madeira-20260922-2307-ml1107.ipa`): draw
dump prints EVERY render target's blend state (`indep= a2c= rt0:... rt1:...`);
`capture-ps` now also copies the draw's constant buffers and buffer SRVs (4 KB
each, kinds cb / buf) so the focus struct and the sprite's cb are readable;
madeira.cfg dumps the nine shaders above; capture-ps = the flame PS. One CAP
on a lamp scene gives the flame draw's DXBC, its cb contents, its rt1 blend.

**ml1108** (built, NOT installed: the user is mid-run;
`build/ipa/Madeira-20260922-2312-ml1108.ipa`): `[perf] ml1108 5.0s:
presents=.. fps cbs=.. gpu_busy=..ms (..% of wall) cpu_blocked_in_retire=..`
every 5 s (MadeiraCtl op 3 = GPUStartTime/GPUEndTime of each retired command
buffer, unioned). This is the GPU-bound vs CPU-bound discriminator the perf
plan needs; `perf-log = 0` disables it.

CPU picture from the ml876 sampler bursts (45 bursts): two guest threads at
42-50 % and 29 %, seven job threads at ~24 % each, 3.7 cores busy in total on a
6-core part; no thread pinned at 100 %. Read together with the GPU number
before choosing between the fence-chain (every encoder waits for the previous
one's fragment stage = zero vertex/fragment overlap on a TBDR) and the CPU
side.

## §98 — ph-rdr61 (ml1108): halo ROOT CAUSE = undefined output lanes folded to 0; perf is NOT uniformly GPU-bound

**Halo, closed on evidence.** capture-ps on the flame sprite PS
(`mdc_b256c32873e2309f`) split the forward pass around each flame draw, so the
R16F transparent-depth target is captured BEFORE and AFTER one draw: the draw
touched exactly one pixel (362,268) and wrote 0 there; the buffer was +inf
before. Disassembly: the PS declares `o1.x` ONLY and ends with
`mad o1.x, w, (spriteW - sceneLin), sceneLin` where sceneLin comes from the
opaque depth (t61 = the DepthTarget SRV, value 0.00324 -> 46 m with the
captured constants A=-0.15 B=-1.00002 from t2[2]@0x280). The PSO blends rt1
with SRC_ALPHA / INV_SRC_ALPHA (indep=1, rt0-rt3 identical). The shader never
writes o1's alpha. In airconv, `to_desired_type_from_int_vec4` shuffles the
unwritten lanes in as UNDEF, and LLVM folds `src*undef + dst*(1-undef)` to a
constant: 0. Reproduced on the Mac GPU (research/captures/halo-dof/
metal-blend-inf-test.m): a scalar `float [[color(1)]]` output with blending on
stores 0 for every alpha and every destination, blend-off stores the value;
with an explicit float4 alpha the blend is IEEE (alpha 1, dst inf -> NaN;
alpha 0.5 -> inf; dst 100 -> exact). The chain: t33 = 0 at the flame -> DoF
composite `|d - focus| / d` = inf -> max CoC -> disc.

**Fix (ml1109, airconv):** `pop_output_reg_fill` -- a pixel-shader
render-target output register's unwritten lanes are (0,0,0,1), the convention
of the hardware the game shipped against (unexported channels), instead of
undef. Applies to every SV_Target; D3D leaves them undefined so no correct
program can observe a difference. The blended result at the flame is now NaN
on Apple (0*inf); the composite's `min` is `air.fmin` (IEEE minNum, NaN
ignored, matches D3D) so d falls back to the opaque depth. Rebuilding
madeira_ir_unix bumped the cache stamp: first launch recompiles every shader.

**Perf timeline (ml1108 `[perf]` lines, seconds after launch; the user's
question "is it decisively GPU?" -- NO):**

    22-67 s     menu            60 fps   GPU  1 %
    66-157 s    early scenes    46-60    GPU 28-89 %   (snow etc.)
    156-211 s   forest          29-41    GPU 29-92 %
    226-251 s   camp at night   19-20    GPU 22-25 %   <- capture f10434 here
    280-339 s   store           19-21    GPU 26-59 %
    354-413 s   city            22       GPU 72-86 %
    472-971 s   ???             27.6     GPU 92 %      (8 min steady; user to name it)
    loading screens in between: 60 fps, GPU 1 %

The camp is neither GPU- (24 %) nor CPU-bound (sampler: top guest threads
46/38/36 %, nothing pinned) at 19 fps with ~13 command buffers per frame:
latency-bound on the sync path (ExecuteCommandLists -> commit -> GPU ->
fence worker -> event -> guest wake, x13 per frame). ml1109 adds the sync
census to the perf line: ECL / Signal / fence waits per frame, ms the game
sat on fences, GetCompletedValue polls, Queue::Wait sleeps, and our
GPU-done -> fence-advanced overhead. City = mostly GPU (72-86 %), so the
fence-chain / store-action work matters there; the 92 % segment is pure GPU.

Built `build/ipa/Madeira-20260923-0252-ml1109.ipa`, NOT installed (app was
running). Tool: `build/tools/dxbc-disasm.py` (opcode table generated from
libs/DXBCParser/d3d12tokenizedprogramformat.hpp).

## §99 — ph-rdr62 (ml1109): halo CONFIRMED FIXED by the user; sync census; everything committed; ml1110 = fence-chain A/B

Committed 2026-09-23: main 1b7f2af, FEX 89db11f58, DXMT 4c86e09, Wine
d88d55eee00 (tests/offline/sm51 ignored; wine/dlls/ntdll/arm64ec_x64_export_iat.c
is an unbuilt review candidate, left untracked). Not pushed.

**Sync census (ml1109) in the camp (19 fps, GPU 24 %):** fence waits 0.0 per
frame, game blocked 0.0 ms, 1 GetCompletedValue poll per frame, 0 Queue::Wait
sleeps, our GPU-done -> fence-advanced 0.03 ms, 11-15 ExecuteCommandLists per
frame. The game is NOT waiting on us there. The ml981 guest RIP profile at
t=268 s: 56 % of guest-thread samples sit at a native address (threads inside
waits), then RDR2.exe+0x25501c0 = 28 %, +0x2599480 = 9 %, +0x2550340 = 3 %:
ONE 64-byte bucket holds a quarter of all guest samples -- a hot loop, most
likely the job system's spin-wait or a copy loop. ml1110 prints 64 bytes of
guest code at the top 6 buckets (`[rip-profile] ml1110 code @...`); read them
with capstone (installed on the Mac). If it is a spin loop, the CPU side is
"threads spinning on the game's own semaphores under FEX", not our sync.
City: GPU 72-86 % with the same zero blocking -> GPU-led.

**ml1110** (built, `build/ipa/Madeira-20260923-0316-ml1110.ipa`, NOT installed
yet, app was running): `fence-chain = 0` in madeira.cfg disables the ml1091
per-encoder fence (mad_enc_fence returns 0; all three exec_fence_* become
no-ops). Pushed to the phone as 0 for the A/B: baseline = ph-rdr62 city
(22 fps, 72-86 % GPU, 35-40 ms GPU/frame). Expect the forest flicker to
return; only the GPU ms/frame and fps in the city matter for the experiment.
Multiblock is FEX's default (true), MaxInst 5000; TSO on, halfbar on.

## §100 — ph-rdr63 (ml1110, fence-chain = 0): GPU time HALVED, fps unchanged; the render thread's hot spot is OUR address resolver

City segment, same scene: fences on (ph-rdr62) 30-42 ms GPU/frame at 20-23
fps, 60-80 % busy; fences off (ph-rdr63) 18 ms GPU/frame at 25.9 fps, 47 %
busy. So the per-encoder chain costs ~15 ms of GPU per frame in the city, and
with it gone the frame is CPU-limited at ~38 ms. Forest flicker returned as
expected. Config restored to fence-chain = 1.

ml981 profile (all guest threads, running or parked) in the city:
0x15a0c8680 = 8-11 % and 0x15a0ca640 = 2 % are HOST addresses inside the
pool copy of madeira_d3d12.dll (image 0x15a0aa000): RVA 0x1e680 =
list_IASetVertexBuffers entry, RVA 0x1f640/0x1f678 = list_IASetIndexBuffer
entry (symbolised with llvm-objdump --syms; the DLL keeps COFF symbols). The
guest RIP of a thread inside an ARM64EC callee stays at the callee's entry,
so those buckets are time INSIDE those calls: both call mad_resolve_address,
a linear walk of d->live[] (every live resource, tens of thousands) under
live_lock, per vertex-buffer slot, per index buffer, per root descriptor,
per draw. The x86 buckets 0x1425a2cd0 (QueryPerformanceCounter-style timer
stop through the IAT, 2-3 %) and 0x1425a36b6 / 0x142595049 (IAT calls in a
loop) are the game calling into Wine DLLs. 0x1425501c0 (13-24 %) is
unreadable through vm_read (page not readable at the image address) and may
be parked threads: the profile had no run-state filter.

**ml1111** (installed, `build/ipa/Madeira-20260923-0341-ml1111.ipa`):
- mad_resolve_address: sorted address index over live buffers (lazy rebuild
  on track/untrack, binary search, backward scan bounded by the largest
  size for aliasing placed resources, live-order first match kept; a miss
  falls back to the old walk and re-syncs). Pure optimisation.
- fence-chain = 2: an encoder waits only if a ResourceBarrier was replayed
  since the last wait, or at the start of a list; updates unchanged. This
  run has fence-chain = 1 (accurate) to isolate the index; next run = 2.
- rip-profile: RUNNING-only histograms (guest RIP, and host pc for threads
  inside native callees) next to the old all-threads one.

## §101 — ph-rdr64 (ml1111, address index, fence-chain = 1): benchmark 32.6 fps avg (user), city now GPU-bound

User: ~60 snow, 35 forest, 39 night landscape, 23 camp, ~23 store/city with
30+ in static city shots; benchmark average 32.6 (was ~20-25). Perf lines:
city t=380-415 s 28-33 fps at 91-92 % GPU, 32 ms GPU/frame -> the accurate
fence chain is the city wall now; store 21 fps at 72 %, 34 ms; camp 24 fps
at 30-73 %. Sync census: camp/store still block ~0-5 ms on fences; city
blocks 6-11 ms/frame (18-31 %) = waiting for the GPU. Next lever there is
fence-chain = 2 (pushed to the phone, config only).

RUNNING-only profiles (ml1111): the top guest buckets are IAT call sites
(0x1425a3680 5-32 %, 0x1425a0080, 0x1425a5100, 0x1425a2cc0 = QPC-style timer
stop, 0x142595040 = loop calling an import per item); native callees: a
shared-cache function at 0x244b60c80 (90-97 % of native samples in two
profiles; the Wine watchdog saw it at PC=0x244b60c2c with LR in
Madeira.debug.dylib) -- ml1112 names it with dladdr; libarm64ecfex.dll
RVA 0xa4c0/0xa500 (transition thunks); madeira_d3d12 mad_resolve_address+0x164
(the per-change qsort of the ml1111 index) -> ml1113 keeps the index sorted
incrementally (insert/remove, seq counter); ntdll memmove+0x140.

Redirect target 0x71f7f43d98 explained: the game process loads the ARM64EC
kernelbase from C:\windows\sysx64 (mixed-arch fallback, 0x2f0000), pool copy
0x15656c000; the native-session kernelbase at 0x71ffb00000 is the ARM64 one.
So the target IS EC code with a pool alias, yet every call still exits to the
PE address and faults: the exit path is not translating it through
IosAliasEntries (pushback callback exists for late loads; verify the entry is
present, and which exit path ExitFunctionEC vs the ios_ffs bypass takes).
~250 k faults/run, ~70 % this one export. Astra-grade FEX question.

Builds: ml1112 = profiler symbolisation (dladdr) + IAT-slot resolution for
the hot guest buckets (`build/ipa/Madeira-20260923-0355-ml1112.ipa`);
ml1113 = incremental index on top (`...-0356-ml1113.ipa`). Neither installed
yet: the app was running (fence-chain = 2 run).

## §102 — ph-rdr65: fence-chain = 2 NEVER RAN (parse clamp); the game's hot code is critical sections

`g_fence_chain = mad_cfg_int_pe(...) ? 1 : 0` (ml1110) clamped 2 to 1, so
ph-rdr65 was another mode-1 run (log: "fence-chain = 1"); fixed in ml1114
(`build/ipa/...-0414-ml1114.ipa`). Mode 2 is still untested.

ml1112's import-slot resolution names the hot guest call sites (RUNNING
threads only):
    0x1425a3680 (38/14/12 %)  -> ntdll    RtlLeaveCriticalSection
    0x1425a0080 (17/10/4 %)   -> ntdll    RtlEnterCriticalSection
    0x1425a5100 (12 %)        -> kernel32 WaitForSingleObject
    0x1425a2cc0 + 3 (5-11 %)  -> kernel32 QueryPerformanceCounter
    0x142595040 (loop, 4-5 %) -> kernel32 SetEvent
(export tables parsed from app/Madeira/arm64ec-windows/{ntdll,kernel32}.dll;
IAT slots 0x145f65378..570.) Native callee samples: 67-89 % at a
libsystem_kernel trap stub that dladdr names mach_generate_activity_id+0x8
(nearest export; probably a neighbouring ulock/semaphore trap), then
libarm64ecfex.dll RVA 0xa4c0/0xa500 (the EC transition), ntdll memmove.
Reading: the game's frame is dominated by lock traffic; each Enter/Leave is
an x64->EC transition (ExitFunctionEC walks IosAliasEntries linearly per
call) and contended ones are ulock syscalls. ml1115 adds: x16 (trap number)
and lr for native samples, and `[sync-census] ml1115 thread alerts: N
wakes/s, N waits/s` from the unix sync path (NtAlert/NtWaitForAlertByThreadId
counters). Candidate levers after that: cheaper EC transitions (hash the alias
table instead of the linear walk; per-call cost), spin counts, and the
per-frame QueryPerformanceCounter redirect faults.

## §103 — ph-rdr66 (ml1115, fence-chain = 2 REALLY ran): no gain; lock traffic confirmed; ml1116

Mode 2 is live in this log ("fence-chain = 2 (ml1111 ...)"), no flicker seen,
but the city still spends 33 ms GPU/frame at 85 % (25.7 fps) -- the same as
mode 1. So "wait after any barrier / list start" waits nearly as often as
"wait at every encoder": RDR2 issues barriers between most passes. ml1116
logs `[perf] ml1116 encoders per frame: fence waits, fence updates, barriers`
to show it directly.

Sync census: 30-53 k thread alerts per second in gameplay (wakes == waits),
~4 k/s in the menu. Every one is a futex wake plus a futex wait, i.e. two
syscalls and a context switch, behind the Enter/LeaveCriticalSection hot spots
of §102. Windows critical sections spin before sleeping; this is the next
CPU lever to examine (spin count in RtlpWaitForCriticalSection / RtlWaitOnAddress).

Native-callee samples of ml1111-1115 were MISATTRIBUTED: the "hot trap" was
threads with no TEB (Mach exception thread, UIKit run loop: x16 = -47
mach_msg2, lr mach_msg2_internal) that passed the x28-frame test. ml1116 only
profiles threads that have a TEB.

**ml1116** (built, `build/ipa/Madeira-20260923-0438-ml1116.ipa`, not installed:
app running):
- FEX exit thunk (Module.S ExitFunctionEC + the ios_ffs bypass): check the
  last-hit alias entry (`IosAliasLast`, pointer, set on a walk hit) before the
  walk, and walk NEWEST-FIRST (the game's EC ntdll/kernel32/kernelbase and the
  D3D12 runtime are registered late; oldest-first was ~150 compares per
  x64->EC call at tens of thousands of calls a second).
- fence-chain = 3 (pushed to the phone): ResourceBarrier records up to 8
  transitioned/UAV resources; the replay tracks resources written since the
  last wait (render targets and depth at pass begin, fill/copy destinations;
  dispatches and texture copies mark "everything"); a barrier arms the wait
  only if it names a written resource. Pixel-shader UAV writes from draws are
  NOT tracked yet -- if anything flickers in mode 3, that is the first suspect.
- rip-profile: Wine threads only.

## §104 — ph-rdr67 (ml1116, fence-chain = 3): BROKEN rendering (glowing ground), reverted to 1; city is CPU-capped ~40 ms

Mode 3 corrupted the forest ground and Saint Denis (untracked pixel-shader UAV
writes / texture-copy destinations are the likely misses). Config set back to
fence-chain = 1 on the phone. Keep modes 2/3 as experiments only.

Decisive result anyway: mode 3 cut the city's GPU time 31 -> 24 ms/frame and
busy 79 -> 61 %, yet fps stayed 25.3. With fences fully off (ph-rdr63) it was
18 ms and 26 fps. The city frame is CPU-bound at ~38-40 ms; GPU fence work
cannot raise its fps until the CPU side shrinks. Stop spending on fences.

Wine-thread-only profile, hot imports (export tables parsed):
RtlLeaveCriticalSection, RtlEnterCriticalSection, QueryPerformanceCounter,
WaitForSingleObject, SetEvent, SetThreadAffinityMask (0x1425a3f80, 10 %),
SetLastError. Thread alerts 11-51 k/s.

**ml1117** (built, `build/ipa/Madeira-20260923-0452-ml1117.ipa`, not installed:
app running). Pure performance, no behaviour change:
- ntdll (PE, sync.c): ios_cs_note (ml810 lock history) compiled out unless
  -DIOS_CS_HISTORY: it did an InterlockedIncrement on ONE global counter on
  every Enter and Leave from every thread (a cross-core cache-line ping-pong at
  the hottest call sites). ntdll SizeOfImage 0x140000 -> 0x120000 (the unused
  80 KB history table is gone); every other source marker matches the
  deployed DLL.
- ntdll (PE, time.c): RtlQueryPerformanceCounter reads CNTVCT_EL0 in user mode
  (offset to the syscall's mach_continuous_time counter measured, re-checked
  once per counter-second, moves forward only and only by > 1 ms).
- unix counters on the sync-census line: server requests/s, QPC syscalls/s,
  SetThreadAffinityMask/s (each is two wineserver round trips today: a
  ThreadBasicInformation query, then set_thread_info).
FEX exit-thunk last-hit cache (ml1116) kept: pure optimisation, no failure
mode seen; revert it if anything odd appears.

## §105 — ph-rdr68 (ml1117): best run so far; city GPU-bound again, camp bound by OUR replay

User: 60 snow, 39 forest, 44 night landscape, 24 camp, city 24-25 with 30+
standing still (thermals "fair"). Log capped at 10 MB (~400 s, ends mid-city).
ml1117 worked: thread alerts 30-53 k/s -> 11-18 k/s (the global atomic in
ios_cs_note made every lock hold longer), QPC syscalls 1/s, SetThreadAffinityMask
0/s (the §104 attribution of 0x1425a3f80 to it was wrong), server requests
~450/s (fine).

Perf lines: city 29 fps at 91 % GPU, 31.7 ms GPU/frame (GPU-bound with the
accurate chain; fences off measured 18 ms); store 21-24 fps at 72-76 %; camp
25 fps at 28 % GPU (CPU). Running-only profile: guest RIP bucket 0x15a064240
(14-42 %) = madeira_d3d12 RVA 0x1a240..0x1a27f = the ENTRY of
queue_ExecuteCommandLists (0x1a278): the game's submit thread inside OUR
replay; native samples in mad_air_resolve (+0x68/+0xe8/+0x128) and
mad_resolve_address. Our per-draw argument-table build runs serially on the
game's render thread at ExecuteCommandLists.

Next levers, ranked:
1. GPU (city/store): fence overlap. ml1118 = fence-chain 5 (installed, set):
   full chain kept (transitivity), render passes wait at the FRAGMENT stage
   unless a compute/blit encoder ran since the last vertex-stage wait. Risk:
   a vertex shader sampling the previous pass's render target. If 5 is clean
   but small, the real fix is per-writer fences: a pool of MTLFences, each
   encoder updates its own, resources remember their last writer's encoder,
   a barrier waits on exactly those writers, with a full wait every 32
   encoders to keep transitivity (mode 3's defect: a skipped wait broke the
   chain, a later wait only covers the immediately previous encoder).
2. CPU (camp, and the city once the GPU drops): an asynchronous submission
   thread per queue (ExecuteCommandLists/Signal/Wait/Present into a FIFO;
   hand the list's command array to the job, the list gets a fresh one so an
   immediate Reset is safe), plus caching argument tables across draws whose
   root arguments did not change.
3. Remaining lock traffic (11-18 k wakes/s), native Oodle (streaming),
   FEX TSO cost (fundamental).

## §106 — ph-rdr69 (mode 5) and ph-rdr70 (mode 0): ALL slow scenes are CPU-bound; fences closed as a lever

(ph-rdr70 is the .prev log; the run starts ~24 s later than ph-rdr68, so
align scenes by fps pattern, not by time.) Mode 5 saved ~2 ms GPU/frame, no
fps. Mode 0 (fences off) halves GPU ms/frame in store (23-38 -> 10-20) and city
(31 -> 9-18) and leaves fps unchanged (camp 23-24, store 19-25, city ~27);
snow was slower (42 vs 53-60, unexplained; flicker in that run anyway).
Conclusion: the "91 % GPU busy" in the city was fence-stall time inside
command buffers, not shader load. Camp, store and city are CPU-limited.
Config restored to fence-chain = 1.

ml1119 (installed, `build/ipa/Madeira-20260923-0535-ml1119.ipa`): `[perf]
ml1119 caller thread per frame: ExecuteCommandLists X ms, Present Y ms
(frame Z ms)` -- how much of the game's render thread is our replay. Decides
the asynchronous submission thread (list payload hand-off: cmds, cdata, used,
rings move to the job; Present drains the FIFO; queue_Wait drains the
signalling queue).

## §107 — ph-rdr71 (ml1119): the game's render thread spends 10-24 ms per frame inside OUR ExecuteCommandLists -> ml1120 async submission

Caller-thread time per frame (fence-chain 1): forest 10-13 ms of 24-31
(41-47 %), camp 13-14 of 42-45 (31 %), store 11-24 of 40-53 (25-50 %), city
10-20 of 30-41 (27-59 %). Present < 1 ms. Retire waits ~0. ~2900 draws and
dispatches per frame -> ~4.5 us of replay per call, all on the thread the
game waits for.

**ml1120** (built, `build/ipa/Madeira-20260923-1122-ml1120.ipa`; madeira.cfg
`async-submit = 1` pushed; NOT installed: app still running):
- Every queue owns a worker thread and a FIFO (CRITICAL_SECTION + two
  CONDITION_VARIABLEs). ExecuteCommandLists validates on the caller (closed,
  allocator generation), AddRefs each list and counts `inflight`, enqueues,
  returns. Signal does the `submitted` CAS on the caller and enqueues; Wait
  enqueues. The worker runs the old bodies (mad_ecl_run / mad_signal_run)
  in order, each job under its own autorelease pool.
- New `mad_fence.committed`: set once a Signal's batch is committed (and by
  CPU fence Signal). A Wait job blocks its worker until committed >= value
  (5 s cap + log) -- NOT "drain the other queue", which would deadlock the
  direct<->compute ping-pong.
- list_Reset waits for `inflight == 0` (spin, then Sleep(0), then Sleep(1));
  Present and ResizeBuffers drain their own queue; queue Release drains and
  stops the worker.
- `[perf] ml1120 async per frame: worker busy, jobs, Present drain, list Reset
  waits, queue-Wait jobs blocked`.
Hazards reviewed: descriptors/upload data/resource lifetime are all legal
only if unchanged until GPU completion, which now comes later but still after
our replay; no tile mappings implemented; a Wait on a signal the same thread
submits later behaves as before (5 s timeout either way).
Backup of the pre-change source: scratchpad madeira_d3d12.c.pre-ml1120.

## §108 — ph-rdr72 (ml1120): the wait MOVED into Present; ml1121 makes Present asynchronous

ExecuteCommandLists on the caller fell to 0.02 ms and nothing broke (no
Wait-job timeouts, no GPU errors, Reset waits 0), but Present drain = worker
busy time in every scene (forest 13/13 ms, camp 12-13, store 10-15, city
10-12): RDR2 submits nearly all of a frame's lists in one burst right before
Present, so the worker only starts when the game is already presenting. No
overlap, no fps change (user: snow 60, forest 36, landscape 45, camp 23,
city 20-30).

**ml1121** (installed, `build/ipa/Madeira-20260923-1138-ml1121.ipa`, active
with async-submit = 1): Present is a queue job. The caller advances
s->index (GetCurrentBackBufferIndex moves on at once), enqueues
MAD_SUB_PRESENT {swapchain, index} and returns after the throttle
(`async-present-ahead`, default 1: at most one present queued behind the
worker). mad_present_run (worker) = the old body: flush_all, capture
bookkeeping, the ml1070 GPU N-latency wait, nextDrawable, blit, present,
perf. Jobs hold NO swapchain reference; swap_Release drains the queue before
freeing (a final Release on the worker would release the queue and make the
worker wait for itself). ResizeBuffers drains. Expected: frame = max(game
CPU, worker encode, GPU) instead of game + worker; the "Present drain" perf
figure now means time the game waits in the throttle.

## §109 — ph-rdr73 (ml1121 async Present): the game thread never waits on us any more; the frame is the game's own threads

Caller time per frame: ExecuteCommandLists 0.02 ms, Present 0.00 ms, throttle
0, worker 10-14 ms in parallel. Frames unchanged (camp 37-43 ms at 11 ms GPU,
store 40-50, city 29-39). So the thread that submits is not the critical
path; removing 13 ms from it moved nothing. No guest thread is saturated
(busiest 35-55 % of a core; sum ~3.6 of 6 cores = latency-bound handoffs).

Sync traffic in gameplay (madsync counters, ph-rdr73): 10-16 k waits/s,
6-11 k sleep, 5-10 k wakes, and 340-610 k ZERO-TIMEOUT POLLS per second
(WaitForSingleObject(h, 0) spin loops; each = FEX x64->EC transition + Wine
syscall dispatcher + madsync lock-free check, est. 1-2 us -> ~0.5-1 core).
Thread alerts (CS / WaitOnAddress / SRW / CV) 11-18 k/s. madsync uses ONE
global pthread mutex + 2 pthread_sigmask syscalls per lock/unlock; a city
sample caught a game thread in __psynch_mutexwait on it.

Running-guest profile across ph-rdr68/72/73 (1924 samples): the hottest RIP
buckets are the game's import call sites: LeaveCriticalSection 17 %,
EnterCriticalSection 7 %, QueryPerformanceCounter 5 %, WaitForSingleObject
4 %, SetEvent loop 3 %. CAUTION: FEX only stores the guest RIP when leaving
compiled code, so a RIP at a call site can also cover x64 code that ran after
the call returned; this over-attributes to call sites. Hence ml1123.

**ml1122** (installed, then superseded): wake-latency histograms for madsync
(`[madsync] ml1122 wake latency ... lock contended ...`) and thread alerts
(`[sync-census] ml1122 alert wake latency ...`); experiments, all 0 = off:
`madsync-spin-us`, `alert-spin-us` (lock-free spin before sleeping, <= 200),
`cpu-count` (processors reported to Windows; iPhone = 2 P + 4 E).
**ml1123** (installed, `build/ipa/Madeira-20260923-1201-ml1123.ipa`): the
profiler runs every burst (20 s) and prints `[cpu-split] ml1123 gen=N
running samples=M: x64 JIT a%, ARM64EC images b%, native c%` by HOST pc, with
the top image PE addresses (/256; symbolise with the [jit-pool] image table:
ntdll/kernelbase unstripped in wine/build-arm64ec, madeira_d3d12 with
llvm-objdump --syms, libarm64ecfex.dll = FEX runtime/transitions/compiler)
and top native symbols.

Decision tree for the next lever:
- x64 JIT dominant -> FEX codegen/TSO (hard; memory-ordering cost is
  fundamental on iOS).
- ARM64EC images dominant, in libarm64ecfex -> x64<->EC transition cost: give
  the hottest imports (Enter/LeaveCriticalSection, QPC, TryEnter...) x64
  implementations the JIT can inline (a small x86-64 helper DLL + loader IAT
  rebinding for x64 modules; slow paths call the EC originals).
- native dominant in madsync/sync -> per-object locks instead of the global
  mutex, drop the sigmask syscalls, cheapen zero-timeout polls.
- wake latency high (tens of us) -> spin experiments via the ml1122 keys.

## §110 — ph-rdr74 (ml1123): where game-thread CPU goes, measured by HOST pc

~2000 running Wine-thread samples over the benchmark:
- **x64 JIT (the game's own code as compiled by FEX): ~68 %** (53-82 % per
  20 s window). The dominant block; still opaque -> ml1124 dumps the hottest
  compiled host code (`[cpu-split] ml1124 JIT code @...`) for offline
  disassembly.
- **ARM64EC images ~15 %, almost all FEX's own runtime (libarm64ecfex.dll)**:
  * ExitFunctionEC region (RVA 0xa500 bucket, 94+7+7 samples, ~5 %): the
    x64->EC call transition. It starts with `mrs FPCR / bic / msr FPCR`
    (clears AFP NEP/AH), and FillSpecialRegs sets them again on the way back:
    TWO FPCR writes per call into Wine; samples land right after the msr.
  * L1-miss path (~5.5 %): GuestToHostMap `do_find` (83), UpdateDynamicL1Stats,
    FindBlock, emplace. L2 is DISABLED on iOS (ml606, 50 MB/thread) and L1 is
    already at its iOS max (128 K entries), so every L1 miss takes the
    WritePriorityMutex read lock + a hash probe. ml1124 counts misses/s.
  * ntdll/kernelbase/madeira_d3d12 together < 2 %. So the earlier "24 % at
    Enter/LeaveCriticalSection call sites" was stale-RIP attribution (FEX's
    frame RIP is a block entry and goes stale across linked blocks).
- **native ~15 %**: swtch_pri (sched_yield from the game's spin-wait loops)
  ~7 %, mach traps, the syscall dispatcher.

Sync latency (ml1122 baseline, spin experiments off): madsync wake->run avg
16-18 us (50 % < 5 us, 10 % > 50 us); thread alerts avg 4-11 us. madsync
global lock: ~3.4 k contended acquisitions/s, avg ~20 us each (kernel
psynch sleep), ~70 ms/s blocked in total.

Freeze at the end of ph-rdr74 (not chased, per user): 1.77 M faults,
EXECUTE on ntdll's non-executable backing at 0x71f88d13e8 (hinsn d11343ff =
a function prologue, i.e. a call that landed on the PE copy instead of the
pool alias) alternating with PROTECTION faults writing 0x145728060 inside
RDR2.exe (entryprot 0), both from guest rip 0x1425a316e (the return address
of the game's WaitForSingleObject wrapper). The fault-loop family.

**ml1124** (installed, `build/ipa/Madeira-20260923-1219-ml1124.ipa`):
- madsync: spin on trylock (madsync-lock-spin, default 256) before a
  contended pthread_mutex_lock sleeps. Pure performance.
- NtYieldExecution's ml1063 throttle is configurable (yield-sleep-us default
  100, yield-streak 256). THIS RUN: yield-sleep-us = 0 (throttle off) --
  hypothesis: the game's workers wait by poll+yield, and the 100 us sleep
  after 256 yields delays job pickup on the critical path.
- profiler: JIT host-pc buckets + code dumps; FEX `[l1-miss] ml1124` rate.

Bigger levers, ranked (not built):
1. x64 implementations of the hottest imports (Enter/Leave/TryEnter
   CriticalSection first) in a small x86-64 helper DLL, bound into x64
   modules' IATs by the loader; slow paths call the EC originals. Removes
   the transition AND both FPCR writes per call. Needs a call-count probe
   first to size it.
2. FEX L1: 2-way lookup or a larger iOS L1 (memory!) or a cheaper miss path.
3. Only after the JIT code dump: codegen items specific to what is hot.

## §111 — ph-rdr75 (ml1124): the hottest "JIT" code is FEX switching FPCR; ml1125 = FEX without AFP

User: slightly worse than ph-rdr74 (snow fell to the 30s, forest 36, landscape
40, camp 23, city 20+). yield-sleep-us = 0 was the change: native share rose
to 28-79 % in windows (threads spinning in sched_yield), heat. REVERTED to 100.
madsync-lock-spin (256) did NOT reduce contended wait (still ~16 us avg): the
lock is not held long, it CONVOYS -- obj_wake calls semaphore_signal while
holding g_lock, the kernel hands the CPU to the woken thread, which then
blocks re-acquiring g_lock. Fix (not built): collect the semaphores in
obj_wake and signal them after ms_unlock (stale posts are already tolerated
by the do_wait loop).

FEX L1 misses: 80-190 k/s steady, spikes to 1.38 M/s.

JIT host-pc profile: the compiled game code is FLAT (64 distinct 64-byte
buckets, 1-3 samples each) -- no hot loop to optimise. The recurring hot
buckets are FEX's DISPATCHER: 0x16bff4140 (re-entry after an EC call:
`mrs fpcr; orr #6; msr fpcr` = FillSpecialRegs) and 0x16bff4400 (re-entry
after a C++ helper such as the L1-miss lookup: `bfxil DAZ; msr fpcr`). They
average 5.8 % of running game-thread samples, 12-18 % in heavy windows; add
ExitFunctionEC's own `msr fpcr` (~5 % in ph-rdr74) -> FPCR switching is
~10 % of game CPU, up to ~25 % in the worst windows. Every x64->Wine call and
every L1 miss pays two FPCR writes, and an FPCR write stalls on Apple cores.

**ml1125** (installed, `build/ipa/Madeira-20260923-1238-ml1125.ipa`):
- madeira.cfg `env.FEX_HOSTFEATURES = disableafp` (FEX's own supported
  switch; the M1-class path): SupportsAFP = false -> FillSpecialRegs /
  SpillStaticRegs never touch FPCR; scalar SSE ops get a few extra
  instructions (NEP/AH emulated in code), MXCSR.DAZ is no longer applied
  (FIZ needs AFP; denormal inputs are no longer flushed -- harmless for
  games, Apple FP handles denormals at full speed).
- Module.S ExitFunctionEC: `tst x17, #6; b.eq` -- the FPCR write only
  happens if NEP/AH are set (always safe; with AFP off it never happens).
Check in the log: `FEX: HostFeatures=<nonzero>`; the dispatcher buckets
0x16bff4140/0x16bff4400 should lose their weight in `[cpu-split] ml1124`.
Revert = delete the env line (no reinstall).

## §112 — ph-rdr76 (ml1125): the AFP experiment NEVER APPLIED; where the perf work stands, honestly

User: no change (snow ~60 then 30s, forest 36, landscape 40+, camp 24, city
20+ topping 30 late). User instruction going forward: if not really sure a
change gives a meaningful FPS gain, write to the handoff instead of building.

**Why ml1125 tested nothing.** FEX/Source/Windows/Common/CPUFeatures.cpp,
iOS branch of FetchHostFeatures: synthesises the feature set, HARDCODES
`HostFeatures.SupportsAFP = true` and returns WITHOUT calling
OverrideFeatures(), so FEX_HOSTFEATURES (disableafp) is read (log
"HostFeatures=32") but never applied. The dispatcher in ph-rdr76 still has
`mrs fpcr; orr #6; msr fpcr` (0x16cff4154) and `bfxil; msr fpcr`
(0x16cff4404). The ExitFunctionEC `tst/b.eq` guard is therefore inert too
(NEP/AH are set whenever JIT code runs). I verified the override against the
non-iOS branch of the same function -- the error. The env line in madeira.cfg
is harmless and inert.

**What is actually known (not guesses):**
1. The game's submit thread no longer waits on our D3D12 layer at all
   (ml1121: ECL 0.02 ms, Present 0.00). Removing 10-13 ms per frame from that
   thread changed nothing -> it is not the critical path.
2. Fences off halves GPU time in store/city and changes nothing -> not GPU.
3. Game-thread CPU by host pc: ~65 % FEX-compiled game code (flat, no hot
   loop), ~15 % ARM64EC images (almost all FEX runtime: ExitFunctionEC,
   L1-miss lookup), ~15 % native (sched_yield from the game's spin loops,
   traps). No thread saturated (35-55 % each, ~3.6 of 6 cores busy).
4. Sync: wake latency modest (madsync avg 16 us, alerts 4-11 us); madsync
   global lock convoys (~3.4 k contended/s, ~16 us each; semaphore_signal is
   called while holding the lock).
5. FEX L1 misses 80-190 k/s (spikes 1.4 M/s), each through the
   WritePriorityMutex read lock + hash probe.
6. Four "CPU overhead" changes produced no FPS change: async submit (13 ms
   off the submit thread), madsync lock spin, yield throttle off (worse),
   AFP (not applied). The consistent message: shaving CPU from arbitrary game
   threads does not move the frame. The frame is bounded by a dependency
   chain we have NOT identified.

**What would actually be confident next (measurement before any fix):**
- Critical-path trace: for the thread that calls Present and the threads it
  waits on, record per frame every blocking wait (object, duration, waker
  tid) -- madsync already knows the waker (obj_wake) and the waiter; add the
  waker tid to the waiter and accumulate per (waiter, waker) pair per frame.
  That names the chain: e.g. "render thread waits 18 ms/frame on an event
  set by thread X", then profile thread X specifically.
- Per-thread CPU with thread identities: name the game's threads (RDR2 names
  them via SetThreadDescription / the 0x406D1388 exception) and attribute the
  cpu-split per thread, so the chain's threads can be profiled in isolation.
- FPCR: a native microbenchmark (N x `msr fpcr` with alternating values) on
  the 18 Pro gives its real cost in ns; the sample bucket 0x16cff4140 also
  contains a TLS pointer chase + code-bitmap load, so sample skid alone does
  not prove the msr is the cost. Only if the benchmark shows it is expensive:
  fix FetchHostFeatures(iOS) to call OverrideFeatures() and rerun.
Levers already documented and NOT built (none has proven critical-path
impact): madsync deferred wake (signal after unlock), x64 fast paths for the
hottest imports, FEX L1 2-way / cheaper miss path.

## §113 — story-mode jetsam (ph-rdr77-story-jetsam.txt): vram-mb 2304 -> 1950; swap eligibility is the real headroom (not built)

Footprint plateaued at 8100-8185 MB from t=200 s to the kill at 8192 (peak
8185). Composition (last [phys-map], cycle 175): guest band 3939 MB dirty /
3737 resident; tag 0 3461 dirty (1231 compressed); tag 100 3060 (unidentified,
see the ml677 warning); pool RX/RW 592; hostlow 704 dirty / 1069 resident.
Our live GPU estimate is only ~1.4 GB (rt 281, tex 302, buf ~650) against the
2304 MB budget, yet budget size drives memory (user: 1536 -> 3072 added
~1.4 GB in the benchmark).

The file-backed swap tier had 750 MB spare (2325 of 3072 used, 0 refused):
the CAP is not the limit, ELIGIBILITY is. ios_swap_eligible() takes only
writable non-exec private valloc views in the guest band with a single commit
>= 8 MB; a heap reserved big and committed in small pieces never qualifies,
which is why ~3.9 GB of guest heap stays resident. Candidate (NOT built, perf
effect unknown): make the threshold configurable (e.g. 1-2 MB) and raise
swap-mb; watch the 16384-extent table, first-touch cost (2-40 us/page
measured in ml1076) and write-back hitches. That would free memory without
cutting the texture budget.

Applied (config only, user request): vram-mb = 1950 (-354 MB).
vram-mb restored to 2304 at the user's request (story perf lower at 1950).

## §114 — ph-rdr78 (story, 23 min riding): the end-of-run "freeze" is an IOGPU abort from a residency-set leak; ml1126

**Performance, riding:** 23-33 fps, GPU busy 61-91 % (27-34 ms GPU per frame,
so on this workload the GPU is near the frame budget), 0 command buffers in
error. Pause/map screens run at 58-60 fps.

**How the run ended (confirmed):** at 14:06:41 (cycle ~683) the phone's
system log (`log collect`, root, `scratchpad/rdr78-abort.logarchive`) has a
FAULT from IOGPU on the game's thread:
`Unable to add allocation to set, current resource count: 216836`.
Our census two seconds earlier: `residency set: 292888 added, 76120 removed
(216768 members)`. IOGPU called abort(). Our SIGABRT handler turned it into
EXCEPTION_WINE_ASSERTION (0x80000101) on thread 0164, which was unhandled, so the
game process was terminated. Its pool mappings were reclaimed after the 3 s
grace while some of its threads were still running. Those threads then
executed ntdll at 0x71f88d13e8 on reclaimed pages ("pc pool-copy pe=0x0
owner=-1"), which produced a fault storm of about 140k faults/s. The user
sees a freeze. ph-rdr74's benchmark "freeze" has the identical signature (same
code 80000101, same abort pc 0x244b6afd8), but it died at only 63,876 members,
and its device log is gone, so its IOGPU reason is unconfirmed.

**Why the set grew:** mad_typed_buffer_view() makes one Metal texture-buffer
view per distinct (format, byte offset, element count, uav). It adds each view
to the residency set and keeps it until the BUFFER is released. The game
sub-allocates typed-buffer SRVs out of long-lived pools, and the census
printed several of them:

- a 128 MB 'Resource' passed 16,384 views by cycle ~95;
- several 4 MB ones reached 4,096 to 16,384 views.

The census stops printing above 16,384. Membership rose linearly by about
170 per second (3,345 at cycle 19, 214,000 at cycle 673).

**The same leak explains most of the memory growth:**

- DefaultMallocZone grew about 615 blocks/s and about 13 MB/min, linearly,
  while the FEX compile rate varied 5x. Allocation rate is therefore not
  compile-driven, and it is near 0 on 60 fps pause screens, so it is not
  per-frame either.
- used +262 MB against about 200k views works out to about 1.2 KB per view.
- IOAccelerator (tag 100) grew +200 MB after load and is probably partly the
  same views (unconfirmed).

Other growth from cycle 127 to cycle 690:

- The guest band grew +960 MB dirty but is DECELERATING: +532 MB in the
  first 4 min, then about 130 MB per 4 min. This looks like the game filling
  its own heap, not a leak.
- Swap was flat at 2211 MB. The §113 eligibility point stands.

**ml1126 (installed, `build/ipa/Madeira-20260923-1430-ml1126.ipa`):**
typed-buffer views are no longer added to the residency set, and release no
longer removes them. A texture-buffer view aliases its buffer's storage, the
buffer is always a member (every r->buffer comes from mad_create_resource ->
mad_resident), and heap-placed textures already rely on the same rule (138k
placed textures render with only the heap resident, ml1072). Texture views of
textures (xviews, few) are unchanged. New log line `ml1126 views:` gives the
live and made counts.

- Expected: members stay around 20k instead of climbing.
- Risk: if the driver did need the view itself resident, typed-buffer reads
  would come back wrong. Look for garbage vertex/instance data or command
  buffers ENDED IN ERROR.
- ml1126 does NOT fix the view leak itself (malloc +13 MB/min continues).

**The real fix (NOT built, needs a design decision):** bound the views.

- Our DXBC backend reads (count, first) from the table we write at encode
  time (32-bit first). So ONE view per (buffer, format, uav) spanning the
  buffer could serve every offset if count and first come from the descriptor
  (metadata low 32 = byte length; bits 32+ = element offset) instead of the
  per-id vmap.
- BUT DXIL shaders (Metal Shader Converter) read the descriptor themselves,
  and the MSC runtime header (MSC 4.0b2, extracted to scratchpad/msc-x) has
  `kIRTexViewMask = 0xff`. An MSC shader sees only 8 bits of element offset,
  so its view must start within 255 elements of FirstElement.
- Options:
  - (a) granule views: base aligned to 256 x element size, width to buffer
    end, count from the descriptor. Bounded by size/granule, 131k worst case
    for the 128 MB pool, and it changes out-of-bounds behaviour unless both
    backends bounds-check with the descriptor length (check airconv and MSC).
  - (b) whole-buffer views when the device has no DXIL pipelines, with a
    one-time rewrite of existing typed-buffer descriptors when the first DXIL
    PSO appears.
  - (c) descriptor-slot refcounting so views can be freed. This is exact, but
    it adds work to CopyDescriptors on the render thread.
- Measure first: the distribution of offsets and counts per pool buffer.

**Second defect, not fixed:** after a game process dies, pool reclaim runs
while its threads are still executing, which turns a clean exit into a hang.
Reclaim should wait until every thread of that peb has left guest code, or
the thread kill must be synchronous.

**Astra's review (RDR2-ph-rdr76-performance-review.md), acknowledged:**
- The JIT host-pc table keeps only the first 64 buckets (`jb[64]`,
  server_ios.c), and the EC/native tables keep 48, so "no hot guest function"
  is NOT established. Rankings are first-seen-biased. Totals by class stand.
- `game blocked on fence` is registration-to-completion latency, not
  blocking time. Rename it; measure real blocking in the wait path.
- The GPU is not ruled out: city at ~31 ms GPU per frame and 84-86 % busy is
  near-GPU-bound, and this riding run is 27-34 ms GPU per frame.
- A madsync deferred wake needs a semaphore lifetime protocol
  (ms_thread_destroy frees the waiter). Its wake histogram also stops before
  lock reacquisition.
- The FPCR/AFP change never applied (iOS FetchHostFeatures). A benchmark
  measures switch cost, not frame-critical frequency.
- Oodle is not a priority until decode is shown on the critical chain.
- Next perf step per Astra: a bounded frame-dependency trace (per-thread
  wait/wake/poll episodes, queue and fence completion, present timeline) in
  one 2-3 s window. Not built yet; the crash took priority.

## §115 — ph-rdr79 (ml1126, story): residency fix confirmed; the "60 fps for a few seconds, then 30" drop happens in two steps

**ml1126 works:** residency membership held flat at ~4,100 (ph-rdr78: 216k and
climbing), 0 command buffers ended in error, no abort, and the user saw no
rendering problems. Typed-buffer views are still leaking as expected (58,197
live and 65,597 made after ~6 min). The log hit the 10 MB cap, so only the
first ~6 min are recorded.

**The user's 60 fps is real, but short.** The Metal HUD lines (one per second,
wall clock) show:
- 14:36:00-17: 54-60 fps, 3D scene (about 2,100 draws/frame), GPU 8-10 ms.
- 14:36:18-26: drops to 26-42 fps; GPU time rises 9.6 -> 25 ms while draws per
  frame FELL (~2,100 -> ~1,460).
- 14:36:27-14:37:00: a light screen (loading/menu/map: ~300 draws, ~90
  encoders/frame, GPU 4-7 ms) at 60 fps. This is what I first mistook for 40 s
  of in-game 60.
- 14:37:01-07: in-game 60 fps, ~1,450 draws/frame, GPU ~8 ms. The game waited
  ~2 ms/frame in Present, so it had headroom.
- 14:37:08: fps -> 25-42 with the SAME draw count and GPU still 8.5-10.6
  ms/frame. The game never waits in Present any more (0.00 ms) and its own frame
  is 26-32 ms, so this is a CPU-side step. Probably the player starting to move
  (streaming/simulation), but thread samples run only every 20 s, so the thread
  is unknown.
- 14:37:18 onward: GPU time per frame rises to 23-33 ms (80-92 % busy) while
  draws grow only ~1.5x and attachment traffic stays flat.

**Hypothesis (NOT proven):** once the frame rate falls for CPU reasons, the
iOS performance controller lowers the GPU clock to just meet the slower
cadence, so GPU time per frame stretches to fill ~80-90 % of the interval.
- Supporting: both episodes show the fps drop FIRST and the GPU-time rise
  after it (1-2 s, then ~10 s).
- Supporting: in episode 1 GPU time tripled while draws fell.
- Supporting: ml1110 (fence chain off) halved GPU time with no fps change.
- If true, "85-90 % GPU busy at 30 fps" (city, riding, Astra's point 3) does
  NOT mean GPU-bound; the limit is still the CPU chain.

**Test (no build needed):** Xcode -> Window -> Devices and Simulators -> the
phone -> Device Conditions -> GPU Performance State = Maximum, then play the
same scene.
- GPU ms/frame falls back to ~9-13 and fps is unchanged: clock scaling
  confirmed, CPU-bound.
- fps rises: the GPU was the limit at the lowered clock.
- Nothing changes: the extra GPU time is real work (heavier shading or
  streaming uploads).
Record the time the condition was set.

## §116 — ph-rdr80: the 60 -> 30 drop is THERMAL/POWER capping (device log proves it); supersedes the §115 hypothesis

Same shape as ph-rdr79: 60 fps at 10.5 ms GPU from 14:59:53 to 15:00:01. Then
at 15:00:02, 25-37 fps with the GPU unchanged; at 15:00:10 GPU time rises to
22-24 ms (fps actually rises to ~40). The device log (root `log collect`,
`scratchpad/rdr80-sys.logarchive`) lines up to the second:

- 14:57:20 thermalmonitord `Thermal pressure level 10` (already raised before
  gameplay).
- 15:00:01.371 kernel `ApplePPMPolicyCPMS::setDetailedThermalPowerBudget`
  STARTS budgeting clientIds 9 (1877 mW) and 10 (2495 mW), lowering them every
  second (to ~1.8 W / 2.35 W). clientId 11 (display) is cut and backboardd drops
  the brightness cap from 1600 to 1098 nits, then down to 545.
- 15:00:02: fps drops.
- 15:00:10.185 `Thermal pressure level 20`, and GPU time per frame jumps.
- 15:00:13: clientId 7 budget ~3.9 W (with a details word) appears and keeps
  being adjusted.

Which clientId is the CPU and which the GPU is not labelled; do not guess. The
"60 fps for a few seconds" is the phone at full power after a light loading
screen, until CPMS clamps it about 8 s into gameplay. Sustained fps in these
runs is set by the power budget. That is consistent with:
- "yield throttle off" being worse (more spinning = more watts),
- the best run having "fair" thermals,
- snow dropping from ~60 to the 30s mid-scene in two runs.

**Consequences for perf work:**
1. Every run needs the thermal state logged, or fps comparisons between builds
   are confounded by how hot the phone was.
2. Under a power cap, WASTED work costs fps even when it is off the critical
   path: zero-timeout polling (340-610k/s), yield/spin loops, FEX overhead,
   redundant attachment load/store (our estimate ~470 MB/frame, about 14 GB/s
   at 30 fps), and fence/encoder overhead. Energy per frame, not CPU %, is the
   metric to cut.
3. The Xcode GPU Performance State test (§115) is not decisive under thermal
   capping; skip it.

**Measurement to add (small, not built):** log ProcessInfo.thermalState
(nominal/fair/serious/critical) with every [perf] window, plus task energy
from `task_info(TASK_POWER_INFO_V2)` (task_energy, gpu_energy) as mW and mJ
per frame. Then A/B builds on energy per frame at a matched thermal state.
**User-side test:** an external phone cooler, a lower fixed brightness, and
not charging should hold 60 longer if this is right.

## §117 — ph-rdr81 (720p, vram 3072, ml1127): the 60 -> 30 drop is NOT thermal (corrects §116); jetsam from Game Mode dropping the memory limit

**§116 corrected.** ph-rdr81 repeats the drop with NO thermal change:
- in-game 60 fps from 15:13:46 to 15:13:55 at 720p, GPU ~13 ms/frame;
- 15:13:56: 22-34 fps with the GPU time UNCHANGED (~13 ms);
- no kernel CPMS budget events anywhere in 15:07-15:14 (the archive did
  capture them at 15:00-15:08, so logging works);
- thermal pressure stayed at level 10 (since 15:08:05) and fell to 0 at
  15:14:50;
- the user held the phone at a fan and saw "Fair".

The ph-rdr80 CPMS event one second before its drop was coincidence or a
secondary factor. The drop comes 7 s (ph-rdr79), 9 s (ph-rdr80) and 10 s
(ph-rdr81) after the full scene starts. At the drop, draws/frame, FEX
compiles (tens/s) and FEX lookups per frame are all unchanged; the game simply
stops waiting in Present and its CPU frame becomes 26-40 ms. The user says
the scenery was fully rendered at 60 and nothing visibly loaded at the drop.

**Open hypotheses; the logs cannot separate them** (thread samples fire every
20 s and none landed in a 60 fps window):
- (a) An OS performance cap on the CPU. gamepolicyd puts the app in Game Mode
  with `Sustained execution mode set to Auto`, `Set DPS to 2` (CLPC), so a
  burst-then-sustain clamp is plausible.
- (b) Loss of the 2 P-cores: more game/runtime threads become runnable about
  10 s after spawn (ambient simulation, streaming, decompression), and the
  critical threads land on E-cores.
- (c) The game really does more CPU work per frame after its spawn window.

**Measurement that separates them (NOT built):** once a second, log
`proc_pid_rusage(getpid(), RUSAGE_INFO_V6)` deltas (unix side or a Swift
timer):

| Field | What it separates |
|---|---|
| ri_cycles / (ri_user_time + ri_system_time) | effective clock; (a) shows up here |
| ri_user_ptime + ri_system_ptime share | P-core share; (b) |
| ri_runnable_time | starvation; (b) |
| ri_instructions per frame | (c) |
| ri_energy_nj, ri_penergy_nj | energy |

Also log ProcessInfo.thermalState. Candidate levers by outcome:
- (b): map Win32 thread priority to pthread QoS (TIME_CRITICAL/HIGHEST ->
  USER_INTERACTIVE) so the scheduler keeps the game's critical threads on
  P-cores, and cut our own busy/spinning threads.
- (a): cut energy per frame. Whether an app can leave SEM Auto without losing
  Game Mode's memory limit is unknown.
- (c): only faster translation helps.

**The crash (confirmed from the device log):** 15:14:08 SpringBoard
_UISystemGestureWindow received the user's bottom-edge trackpad drag (our log:
`[trackpad] CANCELLED` at y ~661-665). The scene was deactivated ('systemAnimation') and
gamepolicyd ended Game Mode: `Sustained execution mode set to Unsupported`,
`Increased memory limit disabled`. The kernel then logged `Madeira [5588]
exceeded mem limit: ActiveHard 6144 MB (fatal)` with a footprint of
6,697,724 KB. So the limit is 8192 MB only while Game Mode holds, and ANY
system gesture that ends it kills the app above 6 GB (720p + vram 3072 ran at
~6.5 GB). The same toggle happens when switching to StikDebug.

**Fix (NOT built, low risk):** defer system gestures on every window. Nothing
in the app does this today. Add `.defersSystemGestures(on: .all)` and
`.persistentSystemOverlays(.hidden)` to ContentView and to the hosting
controllers of PassthroughWindow and ControlsWindow (ControlsWindow does
become key sometimes). A first edge swipe then goes to the app and only a
second reaches iOS. Independently, keeping the footprint under 6 GB (lower
vram-mb) survives Game Mode flickers.

Also seen: IOGPU kernel faults `IOGPUCommandDescriptor::prepare ...
requiredBytes ~2.6-2.9 GB, preparedBytes != requiredBytes` on many command
buffers. Each submission wires ~2.6 GB of resident allocations, and the
per-submit cost is unmeasured.

## §118 — ml1128: 250 ms transition probe (measurement only), per Astra's ph-rdr81 spec

Installed as `build/ipa/Madeira-20260923-1550-ml1128.ipa`. Nothing in it
changes behaviour. The user asked for the root cause of the 60 -> 30 step and
said not to build a fix until the cause is confirmed.

- `server_ios.c ios_xprobe_main` (dispatch utility thread, armed with the thread
  sampler) runs every 250 ms and prints two lines:
  - `[xp] <wall> +<s> dt cpu (thr <sum of threads>) P E run pgw GHz P/E Minst IPC mJ pin rdKB fpMB | pres <enq>/<done> thr ecl n/ms sig wait prs (flush lat n/ms draw cmt) pool caller-ecl`.
    - This is proc_pid_rusage v6 plus the madeira_d3d12 counter block.
    - `cpu` is converted as Mach ticks. `thr` is the per-thread sum and
      validates the unit.
  - `[xp-t] <wall> <role><wintid>:<P ms>/<E ms>:<P GHz>/<E GHz>:<M instr> ...`
    - Role threads are always printed, then the busiest threads (>= 2 ms per
      interval, up to 14).
    - Counters come from PROC_PIDTHREADCOUNTS (34): perf level 0 = P, 1 = E.
      The armed line logs hw.perflevel names.
- Roles, registered via MadeiraCtl op 4 on the thread itself: W = submission
  worker, P = Present caller, E = ExecuteCommandLists caller.
- MadeiraCtl op 5 publishes `g_xp` (madeira_d3d12.c). The layout is mirrored
  in `struct ios_xp_pe`: append only.
- Wall clock in each line aligns with the metal-HUD NSLog lines.

**Reading it (Astra's table):**
- P-share of the E/P role threads falls -> P-core loss.
- Same placement but P GHz falls -> clock cap.
- Minst per frame of the game threads rises at the same GHz -> more game work.
- `run` (runnable) rises -> core starvation.
- `pgw` rises -> paging.
- Present stage (lat/draw/cmt) grows while enqueue holds -> pacing.

## §119 — ph-rdr82 (ml1128 probe): ROOT CAUSE of the 60 -> 30 step = the SoC clamps CPU clocks under a sustained power budget; work per frame is unchanged

**Transition:** in-game 60 fps 15:54:06-14 (GPU 12 ms). At 15:54:14.8 the
probe shows the clock cut:
- P-core effective clock 3.5-4.75 GHz -> 2.14 -> 1.54 -> **1.44 GHz, flat**
  (15:54:15-15:54:28);
- E-core 2.62 -> **1.72 GHz**;
- process CPU power 4-7 W -> ~0.9 W, then ~1.5 W;
- busy cores unchanged (3.3 -> 3.5-4.0).

fps halves at once while GPU time stays 12 ms. From ~15:54:30 the CPU clock
recovers to ~2.0-2.3 GHz while GPU time doubles (12 -> 24 ms): one power
budget shared between CPU and GPU. The phone was not in a hot state:
thermalmonitord showed no change and no CPMS events. The device log has
NOTHING at the clamp (it is the power controller acting silently).

**Same work before and after:** instructions per frame summed over all threads
were ~247 M at 60 fps and ~240 M capped.
- The submission worker W00f8 alone is ~52 M instr/frame, almost all on
  P-cores: the single biggest instruction consumer (native Metal encoding and
  replay).
- Game threads (00ac main/E, 00b0, 00b4, 00b8, 00bc, the 0154-0164 job
  threads) make up ~190 M/frame.
- CPU energy per frame: ~70 mJ at 60 fps, ~42 mJ capped.

So no emulator or game bug causes the step. Sustained fps ~= power budget /
energy per frame.

**Why the 60 lasts a few seconds:** an energy-bucket model fits.
- Loading ran at ~7 W for ~35 s without a clamp, the light screen let the
  budget recover, and gameplay at 4-6 W used the rest.
- Every observed run shows 60 for several seconds after a loading screen.
- The Game Mode policy in force is `Sustained execution mode = Auto`
  (gamepolicyd; Info.plist sets `GCSupportsGameMode = true` and the games
  category). Whether SEM or the ordinary power controller applies this clamp
  is NOT determined.

**Levers, by confidence (none built):**
1. **Energy per frame** (certain direction, work required). Every % cut raises
   sustained fps under a fixed budget.
   - Biggest single item: our D3D12 worker at ~52 M instr/frame (~21 %);
     profile mad_ecl_run and the Metal encode calls.
   - Then FEX-translated game work, the zero-timeout polling, spin/yield loops.
2. **Game Mode off** (`GCSupportsGameMode = false`) as an EXPERIMENT. It may
   change the policy (SEM Unsupported) or may not, and it very likely drops
   the memory limit from 8192 to 6144 (ph-rdr81). It needs 540p and a lower
   vram-mb to fit under 6 GB. Outcome unknown.
3. **User-side:** test unplugged (the device log shows the battery power
   negative, i.e. charging, and charging adds heat), and try an active (Peltier)
   phone cooler. If the controller models real temperature, both extend the
   burst and raise the sustained level.

## §120 — Game Mode / power research: no bypass; the clamp is Apple's default burst -> steady-state profile

Sources: Apple tech talk 111372, the sustained-execution entitlement doc,
LSSupportsGameMode doc, and the iOS 27 GamePolicyFoundation strings from
Xcode DeviceSupport (iPhone18,1 24A5424a).

**How the performance profile works:**
- Apple documents three stages: burst, consistent, then steady state "honoring
  device constraints". The 8-10 s at 60 fps followed by the clamp is that
  default profile. It is not a Game-Mode-only throttle.
- The `com.apple.developer.sustained-execution` entitlement holds an app at the
  steady state from launch (it limits burst). It is paid-program only and would
  not help.

**What Game Mode does for us:**
- gamepolicyd logs for us: GM:true DPS:true SEM:Auto MMA:true TXN:true MEM:true.
- DPS = Dynamic Power Splitter, the CPU/GPU split of one budget. After the clamp
  the P-cores ran below the E-cores (1.44 vs 1.72 GHz), which suggests the
  split favours the GPU.
- MEM = the increased memory limit is applied via Game Mode for games: 6144 MB
  without it (ph-rdr81 kill). Whether a NON-game with our entitlement gets more
  is unverified; log os_proc_available_memory() before relying on it.
- The CLPC controls (setPowerTarget:toTargetWatts:, setSustainableMode:,
  setWorkloadPowerBudget:, the SEM allowlist, enablement strategies) all need
  Apple-private entitlements. There is no free-account path.
- The "gaming energy mode" High/Low Power preference (macOS gamepolicyd)
  appears Mac-only; the iPhone logs never show it.

**User-side Game Mode off:** Control Center -> Game Overlay -> Controls. It
persists per game. The memory limit likely drops to 6144 MB, so test only at
540p with a low vram-mb.

**Levers inside the steady-state budget:**
1. Fewer concurrently busy cores. Power per core grows about cubically with
   clock, so the same watts buy much higher clocks for the critical threads.
   Test with `cpu-count` 4 and 3 (ml1122 switch, never measured with the
   probe).
2. Less GPU power moves DPS budget to the CPU. Test 540p vs 720p on the same
   scene.
3. Less work per frame: worker (§119; static review candidates below), FEX
   overhead, polling and spinning (Apple's CPU-scheduling talk explicitly
   warns against busy-waits and yield).

**Static review of the worker (not measured; confirm with the ml1129 wprof
profile first):**
- mad_air_build_tables_ex / mad_air_resolve are rebuilt twice per draw with
  O(ranges x params) walks plus vmap_get under view_lock. Precompute a plan
  per (pso stage, root signature) and dirty-track.
- ~22 unfiltered Metal state sets per draw (pso, viewport, scissor, 7
  setBuffer, depth/stencil, blend, 5 raster calls). Filter redundant ones
  per encoder.
- useResource every draw (heaps, 16 IA slots, the index buffer, up to 64
  used) even though a residency set exists, plus an 8 KB memset of `ur`
  every draw.
- Argument ring: 7-10 zeroed 1088 B slots per draw (~12-16 MB/frame written
  into shared memory) and a 64 KB chunk refill every ~7 draws.
- ~4 bridge transitions per encoder for create, fence wait, fence update and
  end (~1,040/frame).

## §121 — ph-rdr83: Game Mode off changes nothing; first worker profile; ml1130 cuts memsets

**Game Mode turned off mid-session** (Control Center, during shader loading):
- the memory limit STAYED at 8.6 GB (HUD); from launch without Game Mode it
  is ~6.47 GB;
- the clamp still hit at ~16:49:30 (P 4.7 -> 1.44 GHz, then ~1.8 GHz,
  ~1.5 W, 4.2 cores busy).
So SEM and DPS are not the cause; it is the default steady state (§120).

**Worker profile** (ml1129 wprof, ~8,900 running samples of the W thread,
9 bursts):

| Cost | Share |
|---|---|
| encoder create/end (render 19.3, compute 7.3, blit 3.7, endEncoding 5.8) | ~36 % (AGX driver self 23.5 %) |
| encodeCommands (render 15.4, compute 3.9, blit 3.0) | ~22 % |
| ucrtbase memset (its q-register loop) | ~11.7 % |
| libsystem bzero/memset/memmove | ~6.5 % |
| kevent_id | 9.6 % (origin unknown) |
| D3D12.DLL self | ~20 %, of which mad_air_resolve 9.4 % |

- Our DLL loads as D3D12.DLL, so the inclusive-by-our-function table was
  empty. Fixed in ml1130: the profiler also matches d3d12.dll.
- Symbolize with `llvm-objdump --syms` on the same DLL: RVA = .text offset +
  0x1000. Keep a copy per build (scratchpad/madeira_d3d12-ml11xx.dll).

**ml1130** (installed, `build/ipa/Madeira-20260923-1700-ml1130.ipa`):
exec_ring_take_z clears only the bytes each table uses:
- arg table: arg_qwords*8;
- cb table: up to the highest cb entry + 2;
- VB table: popcount entries;
- root-constant CBV: 256 B;
- draw args: 64 B;
- root slot: the root layout end, plus 512..576 for draw params and draw info.
The `ur` useResource arrays (render and compute) are no longer bulk-cleared;
each entry is zeroed as it is used. Before, ~14.5 KB were zeroed per draw;
now ~1-1.5 KB.
- Expected: memset largely gone from wprof, W instructions/frame down.
- Risk: a shader reading an unwritten table word (previously zero). Watch
  for rendering changes.

**Next candidates, by measured share:**
1. Encoder count (~260/frame; creation is the biggest single cost).
2. Redundant per-draw state in encodeCommands.
3. mad_air_resolve plan caching.
4. What the worker does in kevent_id.

## §122 — ph-rdr84 (ml1130): memset cut worked but the worker is only ~18 % of CPU; RDR2.exe has NO volatile metadata, so every game load/store pays TSO

**ml1130 memset reduction:**
- worker wprof memset share ~12 % -> ~5 % (the rest is the small per-entry
  clears, ucrtbase+62e20 small-size path);
- worker instructions/frame 47.8 M -> 45.1 M;
- all listed threads 253 M/frame (unchanged overall).
- The user saw no fps change, as expected: the worker is ~18 % of CPU
  instructions/frame.
- Game Mode toggles: the clamp still hit at 17:11:22; the log ended ~40 s
  later, too short to compare Game Mode on and off after the clamp.

**Game threads:** cpu-split samples are 78-85 % x64 JIT (the game's own
translated code), with the rest ARM64EC images and kernel. So ~80 % of all CPU
is translated game code, and FEX's code quality and TSO cost dominate.

**RDR2.exe (pulled from the phone, 114 MB):**
- PE32+, image base 0x140000000;
- data directory 10 (load config) is rva 0 / size 0, so there is no load
  config, no CFG and **no volatile metadata**.
- FEX's VolatileMetadata=true therefore has nothing to use: every
  non-exempt load/store in the game's code is emitted with TSO ordering
  (LDAPR/STLR-class) because TSOEnabled=1.
- Only oo2core_5_win64.dll and bink2w64.dll are exempted (our
  FEX_EXTENDEDVOLATILEMETADATA).

**Experiment, config only (NOT applied):**
`env.FEX_EXTENDEDVOLATILEMETADATA = oo2core_5_win64.dll:bink2w64.dll:RDR2.exe`
- Effect: TSO off for the game module; other x64 modules keep it.
- Risk: MSVC x64 std::atomic acquire/release compiles to plain MOVs, so any
  lock-free publication in the game can misbehave (rare corruption, hangs,
  crashes). LOCK-prefixed atomics stay atomic.
- Measure: probe IPC and Minst/frame, and fps before and after the clamp.
- Do not save the game during the test.
- If the gain is large, the follow-up is a selective map: keep TSO only
  around instructions near LOCK ops or in known synchronisation code (FEX
  extended metadata supports instruction-level TSO re-enable).

## §123 — Deep lever analysis for "double FPS" at 720p low / ultra textures (from ph-rdr78/82/83/84/85 + code; no phone)

**Framing.** After the clamp both sides are saturated under one shared
budget (DPS): CPU ~4 busy cores at ~1.8/2.3 GHz, ~1.5 W; GPU ~24 ms/frame,
~90 % busy. 2x fps needs roughly half the energy per frame on BOTH sides,
several multipliers stacked, or frame generation.

### Finding A (strongest, new): the game's critical threads spend ~35-40 % of running time INSIDE Win32 imports, mostly transition + sync overhead

Guest-RIP profile (ml1111/1112: the RIP sits at the IAT call site while host
code runs the import), aggregated per run:

| Import | ph-rdr78 (9,879 samples) | ph-rdr82 | ph-rdr83 |
|---|---|---|---|
| ntdll RtlLeaveCriticalSection | 13.2 % | 15.9 % | 16.1 % |
| kernel32 QueryPerformanceCounter | 16.5 % | 12.2 % | 8.3 % |
| kernel32 SetEvent (loop over an array of 0x30-byte structs at RDR2+0x2595040 = waking job workers) | 3.5 % | 4.6 % | 5.4 % |
| GetLastError | | ~2.9 % | ~2.3 % |
| HeapFree | | ~2.9 % | ~2.3 % |

Also present: RtlEnterCriticalSection 0.6-2.5 %, SetThreadAffinityMask,
WaitForSingleObject ~1 %, SetLastError, TlsGetValue.
- Taken from kernel32 RVAs 0x34800/0x34740/0x34870/0x35ec0/0x34540/0x41870 and
  ntdll 0x91ce0/0x91120/0x91490 against the export tables.
- The FEX CB_SUMMARY "hottest_rip" 0x71f8901490 is ntdll RtlFreeHeap.
- GetLastError is one TEB load; for it to reach ~2.5 %, the x64->ARM64EC
  round trip itself must be expensive. MSVC's CRT wraps per-thread data in
  GetLastError + Tls/FlsGetValue + SetLastError, so these calls are very
  frequent.
- FPCR microbenchmark (M4, scratchpad fpcr_bench.c): an MSR FPCR toggle pair
  (value changes) costs 27 ns; the same value, or mrs+skip, is free. That is
  part of each transition if AFP bits differ between JIT and EC code. It is
  not the whole cost.
- LeaveCriticalSection at 13-16 % is contended wakes. Wine's CS spins ONLY if
  SpinCount != 0. Otherwise Enter sleeps immediately (RtlWaitOnAddress ->
  NtWaitForAlertByThreadId), and every Leave with a waiter does
  RtlWakeAddressSingle -> NtAlertThreadByThreadId, a full syscall-dispatcher
  round trip. That matches the ~31-33k alert wakes/s (sync-census) plus
  madsync's ~10-20k waits/s: ~1,200 sleep/wake pairs PER FRAME.
- QPC at 8-16 % despite the ml1117 user-mode counter: either transition
  overhead or the game spin-waiting on time (a timer loop) -- undetermined.

**Levers:**
1. **Guest-side (x64) bodies for trivial hot imports**: GetLastError,
   SetLastError, TlsGetValue, FlsGetValue, GetCurrentThreadId, QPC via
   RDTSC/CNTVCT (QPF must match).
   - Redirect those IAT entries of x64 modules to small x64 stubs in guest
     memory, so FEX JITs them inline with no transition.
   - Generic (any x64 app), not per-game.
   - Removes ~5-20 % of critical-thread time (all of GetLast/SetLast/Tls,
     most of QPC if it is transition-bound).
2. **Adaptive spin in RtlEnterCriticalSection when SpinCount == 0**: spin a
   few microseconds on LockCount before registering as a waiter.
   - A spinning waiter never increments LockCount, so Leave does no wake
     syscall either. This could remove most of the ~32k wake pairs/s. Short
     spins cost less power than a sleep/wake round trip.
   - Check Windows' own dynamic-spin behaviour to stay faithful.
3. **Cheaper wake/signal paths**: SetEvent and NtAlertThreadByThreadId as
   minimal unix calls with no madsync global lock / pthread_sigmask pair
   (Astra: this needs a lifetime protocol).
4. **cpu-count 4/3** (config): fewer job workers means fewer SetEvent/CS
   wakes and less contention. The cheapest test.
5. **Heap**: HeapFree ~2.5 % plus RtlFreeHeap as the most frequent dispatcher
   target. Check Wine's heap lock/LFH use under this workload.

### Finding B: GPU side (the other half of the shared budget)
1. **Encoder fence chain**: fence-chain 0 HALVED GPU time (§100) but broke
   rendering. Per-resource hazard tracking (wait only on the last writer or
   reader of what an encoder touches) could recover most of that. Under DPS,
   GPU savings buy CPU clock.
2. **Attachment traffic**: our estimate is ~470-580 MB/frame of attachment
   load+store (~18 GB/s at 37 fps), which is significant DRAM energy on a
   phone.
   - ~180 passes per 600 lists end at "list-end" and ~150 at "dispatch", i.e.
     store+reload round trips.
   - Merge passes across list boundaries; Clear/DontCare loads when fully
     overwritten; DontCare stores for discarded or transient targets
     (DiscardResource hints).
3. **Programmatic Metal GPU capture** (MTLCaptureManager to a .gputrace in
   Documents; needs MetalCaptureEnabled in Info.plist) to get real per-pass
   GPU cost and limiter counters in Xcode. Do this before choosing among
   B1-B2.
4. Upscaling (render below 720p + MetalFX spatial): a straight GPU saving.

### Finding C: presentation
The HUD frame intervals are exact 16.67 ms multiples: presentation is at
60 Hz even though CADisableMinimumFrameDurationOnPhone = true (nothing
requests 120 Hz). With 25-30 ms frames, 60 Hz quantization costs throughput
and smoothness. ProMotion (CADisplayLink / preferredFrameRateRange, or
presentDrawable afterMinimumDuration) plus an explicit cap could give
+5-15 % delivered fps in the 30-60 band.

### Finding D: the only literal 2x
Frame interpolation: MetalFX Frame Interpolation (iOS 26+) with the game's
motion vectors and depth (RDR2 has TAA velocity), or a generic optical-flow
interpolator on final frames. It doubles DISPLAYED fps, not simulation or
input latency, and needs buffer identification plus UI handling.

### Already measured / not levers
- BC textures are native (supportsBCTextureCompression=YES).
- The game preset is already low.
- Game Mode is not the cap.
- Global TSO off livelocks (selective TSO: +14 % IPC measured in loading).
- Worker memsets done (ml1130).

### Proposed next probe build (measurement only, when the phone is back)
- Per-second call counts for the hot imports (count in the EC DLLs:
  kernelbase GetLastError/SetLastError/TlsGetValue/QPC/SetEvent; ntdll
  Enter/Leave CS split contended/uncontended, plus the SpinCount distribution
  of contended sections).
- The QPC caller pattern (spin loop or not).
- Divide by the sampled shares to get the per-call transition cost.

## §124 — ml1131: the call-cost / sync probe build (measurement only; built, NOT yet installed — phone unavailable)

IPA: `build/ipa/Madeira-20260923-1811-ml1131.ipa` (the ml1130 D3D12 DLL, unchanged).
Everything below only counts or samples; no behaviour changes.

**1. FEX (xtajit64.dll; Module.S / Module.cpp / libarm64ecfex.def)**
- `IosXpFex` DATA export, 4496 u64 words:
  - [0] magic;
  - x64->EC calls: sp-sharded 16x64 B, STADD/LDADD, in ExitFunctionEC;
  - FPCR writes actually performed in ExitFunctionEC;
  - EC->x64 calls through ExitToX64 (non-bypass);
  - a 4096-entry ring: every 64th x64->EC call of a shard stores its target
    (as the caller addressed it). Word indices 8/136/264, ring index at 392,
    ring at 400.
- The disassembly was verified: the TEB-load triplet the x18 patcher matches is
  untouched; all registers used are already clobbered on those paths.
- Copy: scratchpad/xtajit64-ml1131.dll.

**2. PE ntdll (sync.c, time.c, ntdll_misc.h, ntdll.spec)**
- `ios_xp_nt` DATA export (layout in ntdll_misc.h, mirrored as
  ios_xp_nt_view in server_ios.c).
- Contended critical-section enters, with the SpinCount==0 share, spin wins,
  CNTVCT wait time plus a histogram (<2/10/50/200/1000 us, >=1 ms), and leave
  wakes.
- RtlWaitOnAddress / WakeAddressSingle / WakeAddressAll counts.
- Every 4th contended enter stores its section in a 1024-entry ring.
- A per-thread-slot (tid hash) histogram of the gap between successive QPC
  reads (<1/10/100 us, <1 ms, >=1 ms), which answers "spin loop or not".
- Built with build/wine-pe/build-ntdll.sh: 1,572,864 bytes = SizeOfImage
  1,245,184 + 0x50000.
- Previous ntdll: scratchpad/ntdll-ec.pre-ml1131.dll.

**3. Unix ntdll**
- wine/dlls/ntdll/unix/sync.c: NtSetEvent/Reset/Pulse; NtWaitForSingle/Multiple
  with zero-timeout polls and empty polls; NtYieldExecution (and how many slept
  via ml1063); NtDelayExecution with a requested-delay histogram.
- build/ntdll-unix/server_ios.c `ios_xp_api_report`, every 4th xprobe tick
  (~1 s). It finds both blocks via a PRIVATE module map of the game PEB (TEB
  from a P/E role thread) and cached PE export parsing. Lines:
  - `[xp-api]`: x64->EC/s, FPCR writes/s, EC->x64/s | CS contended/s (spin0 %),
    spin-won/s, wait ms/s, wait histogram/s, wakes/s | WaitOnAddress/wake1/
    wakeAll per s | SetEvent/Reset/Pulse per s | wait1/waitN/polls/empty per s
    | yield/s (slept) | delay/s with histogram | alert wait/wake per s.
  - `[xp-api-top]`: the top 28 x64->EC call targets as `module!export`, with
    estimated calls/s (share of the new ring entries x total rate).
  - `[xp-api-qpc]`: QPC/s, the global gap distribution, and the busiest four
    threads with their <1 us / <10 us gap shares.
  - `[xp-api-cs]`: the top 8 contended sections, with share, SpinCount,
    ContentionCount, heap+off or module+off, and Wine's internal name if any.

**4. `calltest-x64.exe`** (build/x64-tests; app button "x64 call cost").
- Best-of-3 ns/op for: empty loop, integer chain, lock inc, plain store,
  GetLastError, SetLastError, TlsGetValue, GetCurrentThreadId, QPC, free CS
  pair, SetEvent (set / auto), ResetEvent, WaitForSingleObject (signaled / empty
  poll), HeapAlloc+Free, malloc+free, SwitchToThread, Sleep(0), an event
  ping-pong round trip, and a contended CS with spin 0 vs spin 4000.
- Output goes to stdout AND C:\calltest.txt.

**Test plan when the phone is back:**
1. Install ml1131 and tap "x64 call cost" (cool phone, ~10 s); pull the log plus
   `Documents/wine/drive_c/calltest.txt`.
2. Play the usual scene ~3 min; pull the log.
3. Optionally `cpu-count = 4` for a third run.

**Reading it:**
- Per-call transition cost = calltest GetLastError ns (pure transition).
- Share of critical threads = [xp-api-top] rates x calltest costs.
- If most contended waits are <10-50 us and SpinCount==0 dominates, adaptive
  spinning is the lever.
- QPC gaps mostly <1-10 us on one thread means a time-spin loop; otherwise it
  is call overhead.

## §125 — calltest on the iPhone 18 Pro (ml1131b) + ph-rdr86 unix sync counters

**calltest-x64.exe** (best of 3; `research/logs-rdr2/calltest-ml1131-iphone18pro.txt`;
the clock ended clamped at P ~1.8 GHz):

| Operation from x64 | ns/op |
|---|---|
| empty loop / integer chain / lock inc | 2.3 / 1.6 / 2.3 |
| GetLastError / SetLastError / TlsGetValue / GetCurrentThreadId | 51 / 52 / 52 / 51 (pure x64->EC round trip, ~90 cycles at the measured ~1.8 GHz) |
| QueryPerformanceCounter | 70 |
| Enter+Leave CS, uncontended | 118 |
| HeapAlloc+HeapFree 64 B | 141 |
| WaitForSingleObject(empty, 0), the ml1063 lock-free poll | 174 |
| SetEvent (set / auto) / ResetEvent | 544 / 546 / 540 |
| WaitForSingleObject(signaled, 0) | 642 |
| SwitchToThread, Sleep(0) back-to-back | 122,000 (the ml1063 yield throttle's 100 us sleep) |
| event ping-pong ROUND TRIP, 2 threads | 24,500 (~12 us per wake) |
| contended CS spin 0 | 422 |
| **contended CS spin 4000** | **58,456**, i.e. spinning is ~140x WORSE |

(malloc+free and plain store were optimised away by the compiler: 0.0.)

**ph-rdr86** (20.6 min desert/combat at 29.8 fps avg; unix counters only,
since the FEX/nt blocks were read from the stale PE-mapped .data, fixed in
ml1131b). Per second:
- SetEvent 7.3k;
- WaitForSingleObject 33.5k, of which zero-timeout polls 25.5k (96 % empty);
- WaitForMultiple 0.3k;
- Sleep(0) 9.3k, Sleep(1-4 ms) 1.6k;
- yields 10.8k, of which 2.7k were throttled into 100 us sleeps;
- alert waits 21k (~700/frame);
- CPU capped ~2.25 GHz / 1.7 W / 3.6 busy cores.

**Consequences for the §123 levers:**
- Guest (x64) bodies for trivial imports: CONFIRMED worthwhile. Each call is
  ~51 ns (~90 cycles) of pure transition at the clamped clock.
- **Adaptive CS spinning: REFUTED here** (spin 4000 = 58 us/op vs 0.42 us).
  Threads share cores and the clock is capped, so a spinner steals the owner's
  core. Do not build it.
- SetEvent/ResetEvent/Wait(signaled) at ~0.55-0.65 us each: the syscall
  dispatcher plus madsync (global lock, sigmask) path. A user-mode fast path for
  "state unchanged / no waiter" would cut most of the 7.3k SetEvent/s and the
  signaled-wait cost.
- **Cross-thread event wake ~12 us** (ping-pong 24.5 us round trip) vs thread
  alerts ~4 us (ml1122 census). ml1122 also measured madsync wake latency avg
  26.6 us. If job-system wakes are on the frame's critical path this is the
  biggest latency item; it is the madsync wake path (Astra: deferred wake needs
  a lifetime protocol).

## §126 — ph-rdr87 (ml1131b, 8 min gameplay): the import-transition lever is small; ONE lock of ours is contended 100-170k times/s -> ml1132

Log: `research/logs-rdr2/ph-rdr87-ml1131b-xpapi.txt`. The ml1131b block line
read valid magic on both blocks, so all counters are live this time. Gameplay
23:00-23:08:50, 30-35 fps. Figures are steady-state gameplay rates.

**Transitions (FEX ExitFunctionEC counter):**
- x64->EC ~1.0-1.2 M calls/s, each with an FPCR write; EC->x64 ~30/s.
- At the calltest cost (51 ns) that is ~60 ms/s, ~1.7 % of the process's
  ~3.65 busy cores.
- **§123 Finding A (35-40 % of critical-thread time "in imports") is NOT
  borne out.** Likely cause: the guest-RIP profile's RIP is only synced at
  transitions, so the guest code AFTER an import is charged to that import.
  That fits Leave 13-16 % vs Enter 0.6-2.5 % (long body after Leave, short
  CS body after Enter) and QPC 8-16 % (job runs after its timestamp).
- Lever 1 (x64 bodies for trivial imports) is therefore worth at most ~2 %
  CPU. Deprioritised.

**Top call targets (sampled every 64th transition, calls/s):**

| Target | Calls/s |
|---|---|
| ntdll RtlLeaveCriticalSection / RtlEnterCriticalSection | ~100-114k each |
| ntdll RtlQueryPerformanceCounter | ~102k (4 threads 00b0-00bc at ~20k each; 56 % of gaps < 1 us, so per-job timing, not a time spin) |
| D3D12 res_GetGPUVirtualAddress / res_GetDesc | ~89k / ~80k |
| D3D12 device_CopyDescriptors | ~82k |
| D3D12 CreateShaderResourceView / CreateConstantBufferView | ~76-83k / ~73-76k |
| D3D12 SetGraphicsRootDescriptorTable | ~68-70k |
| D3D12 IASetVertexBuffers / IASetIndexBuffer / DrawIndexedInstanced | ~36-49k / ~30k / ~21-25k |
| kernelbase InitOnceExecuteOnce | ~27k |

Symbolise: our DLL `llvm-objdump --syms`, RVA = value + 0x1000. Wine EC DLLs
in wine/build-arm64ec: **RVA = value + 0x10000** (.text is at 0x10000 there).

**THE FINDING: contended critical sections**
- 95-170k contended entries/s.
- **100 % of the sampled ones are one lock: madeira_d3d12's `live_lock`.**
  - Its address is device + 0x78. The device is the parameter of the
    mad_fence_worker thread in `[thr-create]`; the offset was matched against
    `struct mad_device`.
  - It has spin 0.
- The game's own locks are essentially uncontended.

| Counter | Value |
|---|---|
| Wait inside RtlpWaitForCriticalSection | 190-300 ms/s |
| Wait histogram | mostly < 2 us, a tail to 1 ms |
| RtlWakeAddressSingle | ~95-170k/s |
| WaitOnAddress | 20-40k/s |
| Real thread sleep/wake pairs (alert wait/wake) | 16-32k/s |

- Who takes it: `mad_resolve_address`, on every GPU address lookup:
  - at record time on the game's 4+ recording threads (IASetVertexBuffers,
    IASetIndexBuffer, root descriptors);
  - at replay on the submission worker (root descriptors and descriptor-table
    buffer entries).

**Other rates (per second):**
- SetEvent ~9.5k; WaitForSingleObject ~11.7k; zero-timeout polls only ~1k
  (ph-rdr86 had 25.5k: a different scene or phase).
- Yields 6-10k, of which 2.7-3.5k were throttled into 100 us sleeps.
- Sleep(0) ~5k; Sleep(1-4 ms) ~1.6k.

**GPU:** 82-91 % busy, 25-27 ms GPU per frame at 30-35 fps, ~225 encoder
fence waits per frame. This scene is close to GPU-bound as well as
CPU-power-capped.

**Threads (35 fps window):**
- ~3.65 cores busy (P 1.9 GHz, E 2.36 GHz, 1.34 W, ~39 mJ and ~310 M
  instructions per frame).
- No thread above ~41 % of a core.
- Our submission worker W00f8 is ~45 M instr/frame.

### ml1132 (installed, `build/ipa/Madeira-20260923-2328-ml1132.ipa`): lock-free address lookup

- `mad_resolve_address` now searches the sorted index under a sequence count
  (`aidx_ver`) instead of `live_lock`.
- Writers are unchanged and still hold `live_lock`: track, untrack and
  rebuild bracket every index change with mad_aidx_begin/end (count odd while
  changing).
- A reader falls back to the unchanged locked path, so every answer is one
  the locked search would give, when it sees any of:
  - an odd or moved count;
  - a pending rebuild (`aidx_dirty`);
  - an empty index;
  - no match.
- The index block is never freed on growth (a reader may still be in it);
  the retired blocks are bounded by the final size.
- `naidx` is stored with release after its block; readers load `naidx`
  (acquire) before `aidx`.
- Disassembly checked: ldar loads, `dmb ishld` before the re-check, and a
  full fence after the writer's first increment.
- One variable: nothing else changed.

**Next run, check:**
- `[xp-api] CS contended` should drop from 100-170k/s to near zero, or name
  the next hot lock.
- `[madeira-d3d12] ml1132 address lookups on the locked path: N (true misses
  M)`: N must be a small fraction of the lookups. If true misses are high,
  those lookups still serialise on the lock; the next step is a negative
  cache or finding what the unmatched addresses are.
- Compare energy (mJ) and Minst per present in `[xp]`, and fps, at matched
  scenes. No fps prediction: this removes measured waste (blocking, ~170k
  wake calls/s, ~30k context switches/s), but the scene is near GPU-bound.

**Remaining big levers (ranked by evidence):**
1. GPU: per-resource hazard tracking to replace the global encoder fence
   chain. fence-chain 0 halved GPU time (§100) but broke rendering; ~225
   waits per frame now.
2. CPU: ~80 % of CPU is the game's translated code (cpu-split): FEX codegen
   and selective TSO (+14 % IPC in loading with TSO off; RDR2.exe has no
   volatile metadata).
3. Presentation: 120 Hz / ProMotion pacing (Finding C).
4. Frame interpolation (Finding D, the only literal 2x).

## §127 — ph-rdr88 (ml1132): lock fix confirmed; THE CLAMP IS A CPU ENERGY BUDGET, and loading spends it -> ml1133 ECO

Log: `research/logs-rdr2/ph-rdr88-ml1132.txt`.

**ml1132 confirmed:**

| Counter | ph-rdr87 | ph-rdr88 |
|---|---|---|
| Contended CS entries/s | 95-170k | ~150-250 |
| CS wait | 190-300 ms/s | 25-30 ms/s |
| Alert sleep/wake pairs/s | 16-32k | ~150 |

- `ml1132 address lookups on the locked path: 0 (true misses 0)`.

**60 fps in game: 38-39 s** (in-game rendering from 23:49:35.4, locked 60
until 23:50:13, clamp at 23:50:14).
- Earlier runs, in-game time before the clamp:
  - ph-rdr82: 9 s; ph-rdr83: 8 s; ph-rdr84: 6 s; ph-rdr86: 5 s;
  - ph-rdr87: 11 s, at 47-53 fps.
- Confound: this run started in a lighter area.
  - ~220 M instr per frame vs 400-480 M in ph-rdr87.
  - In-game CPU power at 60 fps was 2.1-4.2 W (avg 2.97 W, P 3.2-4.1 GHz)
    vs 4.1-4.8 W in the other runs.

**The model (scratchpad clamp2.py over the [xp] 250 ms lines):**
- Every run clamps after the same CPU energy counted from app start:
  422 / 426 / 430 / 430 / 442 J (ph-rdr82-87), and 478 J in ph-rdr88.
- Counting only power ABOVE ~2.3 W, all six agree within +-3 %:
  249 / 246 / 248 / 245 / 251 / 249 J.
- So the governor allows ~2.3 W of CPU indefinitely plus a burst credit of
  ~250 J. When the credit is gone, it clamps the CPU clock (the process then
  sits at ~1.3-1.4 W, P ~1.8-2.1 GHz).
- The credit refills below ~2.3 W:
  - ph-rdr86: after ~30 s of ~0.8 W spells (menus), P went back above 3 GHz
    for 15 s;
  - ph-rdr87: the same once, for 12 s.
- The GPU does not visibly count: the loading screen, with the GPU idle,
  spends the same credit.

**Where the credit goes (credit above 2.3 W):**

| Run | Loading | Gameplay |
|---|---|---|
| ph-rdr82 | 231 J (93 %) | 19 J |
| ph-rdr83 | 232 J (94 %) | 14 J |
| ph-rdr84 | 233 J (94 %) | 15 J |
| ph-rdr86 | 235 J (96 %) | 11 J |
| ph-rdr87 | 229 J (91 %) | 22 J |
| ph-rdr88 | 222 J (89 %) | 27 J |

Loading runs 65-87 s at 4.5-5.6 W. **The game gets only the scraps.**

**Consequences:**
- With the full ~250 J at the start of gameplay:
  - ph-rdr88's light scene (~3.0 W at 60 fps, 0.7 W over): ~6 min at 60 fps;
  - heavy scenes (~4.5 W, 2.2 W over): ~2 min.
- Sustained 60 needs CPU power at 60 fps <= ~2.3 W at burst clocks, or
  <= the clamped allowance once clamped. Post-clamp here was ~28 mJ/frame at
  ~2 GHz, i.e. 1.7 W at 60 fps: the light scene is ~25 % away on CPU.
- The GPU also slows after the clamp: 15 -> 19-23 ms per frame for the same
  area. So GPU work per frame must also fall ~25 % there.
- Pausing in a menu refills the credit.

### ml1133 (installed, `build/ipa/Madeira-20260924-0015-ml1133.ipa`): ECO switch

- `wine/dlls/ntdll/unix/sync.c`:
  - `madeira_set_eco()` bumps `ios_eco_gen`.
  - Every guest thread re-applies its QoS on its next NtWaitForSingleObject /
    NtWaitForMultipleObjects / NtYieldExecution / NtDelayExecution /
    NtWaitForAlertByThreadId (a `__thread` generation check).
  - QoS can only be set on self.
- Class while ECO is on: madeira.cfg `eco-qos` = utility (default),
  background or initiated. Otherwise USER_INTERACTIVE, as before.
- `thread_ios.c` thread start now goes through `ios_eco_apply_self()`.
- `madeira.cfg eco = 1` starts with ECO on.
- Overlay: green `ECO` pill next to CAP (filled = on).
- Toggles are logged: `[eco] ml1133 HH:MM:SS.mmm eco ON/OFF (guest threads -> class)`.
- The phone's madeira.cfg now has `eco = 1`. Backup:
  scratchpad `madeira-cur.cfg.pre-ml1133`.

**Test:**
- Launch with ECO on; the loading screen should be slower.
- Tap ECO off as soon as gameplay appears.

**Read from the log:**
- Loading duration and CPU W during loading: the target is <= ~2.3 W.
- P vs E ms in `[xp-t]`.
- Credit above 2.3 W spent before gameplay.
- In-game seconds of 60 before the clamp.
- Risks: audio crackle or timeouts in the loading screen at low QoS;
  utility QoS also throttles I/O.

**If confirmed, next:**
- An automatic ECO: generic "spend burst only while rendering real frames",
  e.g. GPU ms/frame < 2 while CPU > 2.3 W.
- Or a governor that holds the process at a chosen power, now that the budget
  is known.

### Resolution: screen-shaped virtual desktop

- The phone is 2622x1206 in landscape (2.174:1; 874x402 pt).
- madeira.cfg `desktop-size = 1408x648`: 2.173:1, 912k px = 99 % of 720p.
  The compositor aspect-fits it to 873.5x402 pt, so it fills the screen
  (1280x720 left 80 pt bars each side).
- RDR2 `Settings/system.xml` (in `C:\Program Files\Red Dead Redemption 2\Settings`):
  - `windowed = 2` (borderless) follows the desktop;
  - screenWidth/Height edited 1280x720 -> 1408x648. Backup: scratchpad
    `rdr2-system.xml.pre-ml1133`.
- Aspect ratio is Auto (Hor+: the same vertical FOV, wider view, a little more
  scene to draw).
- The Dynamic Island and the rounded corners cover the screen edges; RDR2's
  HUD safezone setting can pull the HUD in.

## §128 — ph-rdr89 (ml1133): ECO through loading doubled in-game 60 fps (38 s -> 85 s); corrected budget model; post-clamp is GPU-bound

Log: `research/logs-rdr2/ph-rdr89-ml1133-eco.txt`. Setup:
- ECO on from launch, off at 00:21:21.6 when gameplay appeared;
- same spot as ph-rdr88, camera untouched;
- desktop 1408x648 (swapchain 1408x648, compositor fills the screen).

| Phase | Time | Avg CPU | CPU energy | fps | Instr/frame |
|---|---|---|---|---|---|
| Loading + menus, ECO on | 00:18:49-00:21:21, 152 s | 1.17 W | 178 J | ~30 | |
| In game, ECO off | 00:21:21-00:22:46, **85 s** | 2.76 W | 235 J | 59.4 | 256 M |
| Clamped steady | 00:23:00-00:28:10 | 1.34 W | | 40.1 | |
| ECO taps in game | 00:28:45-00:30:03 | 1.27 W | | 33.8 | |

- The user's stopwatch said 1:25 for the in-game period, which matches.
- Loading used 178 J vs ~370 J in the full-clock runs and took ~2x longer.
- Clamped steady was 33 mJ/frame.

**Model correction (§127 was wrong):**
- The "2.3 W free + 250 J credit" fit is REFUTED: it predicted ~6 min, and
  ph-rdr89 clamped after only 58 J of such credit.
- The first clamp is best fit by a ONE-TIME allowance of ~425 J of CPU energy
  counted from app launch, with no cooling term over these minutes
  (scratchpad heat-model grid).
- Clamp-time error per run:

| Run | Error |
|---|---|
| ph-rdr82 | -1 s |
| ph-rdr83 | -2 s |
| ph-rdr84 | -2 s |
| ph-rdr86 | +3 s |
| ph-rdr87 | -5 s |
| ph-rdr88 | -18 s (probably a cooler start) |
| ph-rdr89 | +9 s |

  rmse 8 s over 7 runs.
- ECO worked by halving the loading's share: 178 J instead of ~370 J, so
  gameplay got ~235 J instead of ~116 J.

**After the clamp: a ~1.35 W average limiter with a short window.**
- ECO for 21 s (0.82 W, 26 fps) bought a 6 s burst (4.23 W, 56 fps).
- Burst frames cost **75 mJ** vs 31-33 mJ clamped (full clock is 2.3x less
  energy-efficient). In-game toggling therefore averaged 33.8 fps vs 40.1
  steady: it time-shifts frames and loses some.
- Same for the one-time allowance: the in-game burst ran at ~46 mJ/frame.
  An allowance spent at lower clocks buys more frames.

**The GPU is throttled by the clamp too, and is then the limit:**
- The camera was static. At full clock: 15.0 ms GPU per frame at 60 fps, 90 %
  busy.
- Right after the clamp: 20-26 ms per frame at 88-92 % busy for the SAME view,
  so the GPU clock dropped ~1/3.
- Post-clamp fps (~40) is GPU-bound. A perfect CPU would still give ~44 fps
  here.
- Sustained 60 in this light spot therefore needs:
  - GPU time per frame at the clamped clock from ~22 ms to <= 16 ms (-27 %);
  - CPU energy per frame from ~33 mJ to <= ~22 mJ (-33 %).
- The GPU items are the ranked levers of §126:
  - per-resource hazard tracking instead of the global encoder fence chain
    (fence-chain 0 halved GPU time, §100);
  - attachment load/store actions;
  - render scale / MetalFX.

**Next:**
1. User test, config only: RDR2 Resolution Scale 5/6 (or 3/4). Post-clamp fps
   should rise if it is GPU-bound as above.
2. Automatic ECO (generic): ECO while GPU ms/frame < ~2 for a few seconds with
   the CPU busy (loading screens and menus), off once real frames render. It
   preserves the one-time allowance with no taps.
3. GPU: per-resource hazard tracking.

## §129 — ml1134: fence-chain = 6, barrier-driven encoder sync (the GPU lever)

**Why now:**
- After the clamp the GPU is ~90 % busy at 20-26 ms per frame, so the frame
  is GPU-bound (§128).
- Fences off halved GPU time (§100, §106), but rendering broke.
- ph-rdr89 per frame: ~260 encoders, each waiting for the whole previous one
  (mode 1), and ~239 barriers.
- Only ~54 of the encoders are render passes. Almost all the rest are compute
  encoders that `MC_BARRIER` closed, each reopened behind a full GPU drain.
- ~400 MB per frame of attachment load+store (upper-bound estimate) is a
  separate lever, not touched here.

**Mode 6 (see the comment at f6_begin in madeira_d3d12.c):**
- Every encoder updates its own fence from a per-queue pool of 64 and joins
  `q->f6_pend` (max 40, with the attachments it wrote).
- An encoder waits for every pending encoder when any of these holds:
  - a ResourceBarrier or list start since the last sync point;
  - it is a render pass whose attachments a pending encoder wrote (D3D12
    orders render-target and depth writes without barriers);
  - it is a blit after a pending blit;
  - the pending set is full.
- Otherwise it waits for nothing and overlaps its predecessors.
- At a barrier:
  - an open encoder that did not sync at its start is closed;
  - a synced compute encoder stays open (serial dispatch orders the
    dispatches after the barrier); `f6-compute-open = 0` disables that;
  - a synced render pass stays open, as in mode 1.
- The device fence is waited for at every list's first sync and updated only
  by a join blit encoder in `mad_queue_flush`, which waits for all pending
  encoders. So the cross-queue and cross-batch order is the same as mode 1.
  The Present blit also waits for it.
- Stats: `[perf] ml1134 fence-chain 6 per frame: N encoders, N synced (N for a
  shared attachment), N overlapped; N compute encoders kept open at a barrier;
  N overlapped render passes closed at a barrier; N joins`.
- Installed: `build/ipa/Madeira-20260924-0100-ml1134.ipa`. Phone cfg:
  `fence-chain = 6` (backup `madeira-cur.cfg.pre-ml1134`) and `eco = 1`.

**Test (same spot as ph-rdr88/89):**
- ECO on through loading, off in game, camera still.
- Compare:
  - GPU ms/frame at 60 (ph-rdr89: 15.0 ms, 90 % busy);
  - post-clamp GPU ms/frame and fps (20-26 ms, ~40 fps);
  - seconds of 60 before the clamp (85 s).
- Check rendering carefully: flicker, missing geometry, wrong shadows or
  lighting.
- If it is broken:
  - first try `f6-compute-open = 0`: it separates "serial dispatch does not
    order untracked writes" from "the overlap rule is wrong";
  - then `fence-chain = 1` to revert.

**Open risks:**
- Serial compute dispatch coherence for untracked resources (assumed, not
  proven on device).
- Hazards D3D12 orders without barriers that are not attachments.
- The overlapped-then-closed render passes cost an extra attachment store and
  load (counted).

## §130 — ph-rdr90 (ml1134, fence-chain 6): no GPU gain in the matched view; the freezes were a SMALL JIT POOL (VA layout lottery); reverted to mode 1

Log: `research/logs-rdr2/ph-rdr90-ml1134-f6.txt`. User report:
- ~1:10 of 60 fps, with hitches;
- then 35-40 fps with constant freezes (camera, walking);
- no visual glitches.

**Mode 6 did what it was built to do, but it bought nothing:**

| Per frame | Mode 1 | Mode 6 |
|---|---|---|
| Encoders | ~260 | ~186 |
| Compute encoders kept open across a barrier | | ~104 |
| Synced encoders | | ~163 (RDR2 puts a barrier before nearly every encoder) |
| Overlapped encoders | | ~23 |
| Shared-attachment syncs | | ~15 |

- **Matched static view at 60 fps:** 15.0 ms GPU per frame at 89-90 % busy,
  identical to mode 1 (ph-rdr89). Encoder boundaries are cheap here.
- Post-clamp GPU 9.5 ms/frame at 25-40 % busy looks better than ph-rdr89's
  22 ms, but the user was moving (a different view). CONFOUNDED; do not cite it.
- Without the freeze and hitch seconds, post-clamp fps was 39.3 vs 40.1: the
  CPU limit.
- Config reverted to fence-chain = 1 on the phone. The mode 6 code stays for
  later experiments.

**The freezes (~1 s, one every ~14 s after the clamp) are NOT mode 6.** They
are FEX translation-cache generation rollovers:

| Run | JIT pool | Rollovers (`[gen] alloc#`) | Code buffers |
|---|---|---|---|
| ph-rdr86 | | 6 | |
| ph-rdr87-89 | 544-560 MB | 4 | |
| **ph-rdr90** | **432 MB** | **52** | 49 degraded to 16-32 MB ("pool pressure") |

- Each rollover migrates ~50 threads to a new code generation, and
  real_compiles jumps ~20k in under a second.
- During a freeze the game submits nothing (ecl 0, pres 0) while its threads
  burn CPU (x64->EC 55k/s, instructions per second unchanged).
- Seconds under 5 fps in gameplay:

| Run | Freeze seconds | Gameplay |
|---|---|---|
| ph-rdr86 | 1 | 1181 s |
| ph-rdr87-89 | 0 | |
| ph-rdr90 | 12 | 167 s |

**Why the pool was small (hole census, ml1036):**
- ph-rdr90 free holes: 0x12453c000+442MB | window | 0x148000000+253MB |
  0x159c00000+276MB | 0x16b748000+328MB. A ~31 MB mapping at ~0x157d00000
  split the hole above the window.
- ph-rdr89 had 0x148000000+562MB there.
- The ml1040 constructor (JITAllocator.c, runs before main) reserves 256-1024 MB
  at 0x148000000. It failed ("no early pool placeholder was obtained") because
  253 < 256: the intruder was there BEFORE our constructor.
- It is not Madeira.debug.dylib (that loads at 0x104ee8000). The JITAllocator
  comment records the same 31 MB intruder at 0x15dd00000 on an earlier bad
  launch ("launch C").
- ~400 MB of the pool is PE image copies, so a 432 MB pool leaves FEX
  ~20-30 MB of code buffer.

**Next for this bug:**
1. Name the intruder: at constructor time, vm_region_recurse over
   [0x148000000, 0x190000000), stash base/size/user_tag in globals, and have
   StikJITHelper log them.
2. Mitigation: when the pool is < ~500 MB, show the user "unlucky memory layout
   this launch, relaunch for a smooth session" before the game starts.
3. Real fix, depending on (1): reserve earlier, or shrink the image-copy share
   of the pool.

**GPU, next:** the static-view GPU time (15 ms at full clock, ~22 ms clamped) is
not encoder-boundary stalls. Test RDR2 Resolution Scale 5/6 or 3/4 (mode 1)
to see how much of it scales with pixels. If it does, attachment load/store
traffic (~400 MB/frame upper bound) and render scale are the levers.

**ml1135** (installed, `build/ipa/Madeira-20260924-0128-ml1135.ipa`; the D3D12 DLL is ml1134's, run in mode 1):
- The JITAllocator.c constructor records the first mapping above the window
  when no placeholder fits.
- StikJITHelper logs it: `ml1135: the placeholder was blocked at image load by
  a mapping at 0x..+NMB (VM tag T, prot P)`.
- A pool shrunk below 500 MB logs `⚠️ SMALL JIT POOL (N MB) on this launch ...
  relaunch`.
