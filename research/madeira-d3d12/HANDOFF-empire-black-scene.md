# Handoff: UE 5.4 / D3D12 title renders a black scene through madeira-d3d12

Written 2026-09-14 (session ml893 → ml902). Nothing in this session is committed.
The tree has all the uncommitted work described below; every build is reproducible
with the chains in `memory/reference_build_and_deploy_chains.md`.

## Where things stand (one paragraph)

The engine reaches its first playable level on the VM through the native D3D12
runtime and renders every pass of a frame, but the scene comes out black: every
GBuffer and scene-colour target is all zeros while UI, depth-derived masks and
several compute-written textures are populated. Nanite is NOT the cause (the
same result with `r.Nanite=0`, where 135 ordinary six-target base-pass draws per
frame write nothing). The loading-screen widget stays up over the black scene,
almost certainly waiting on media the story mode starts (Electra, see below).
All the crash classes that ended runs earlier are closed or worked around; a run
now lasts as long as you leave it.

## Exact evidence (all from host frame dumps + client census, ml899-ml902 runs)

Frame at present ~1800-2400, Nanite OFF (`ants47`, host dump f19):

| texture (736x416 unless noted) | Metal fmt | written? |
|---|---|---|
| GBuffer A-E + SceneColor, 6 MRT targets (RG11B10, BGRA8 x3, RGBA16Unorm, RGB10A2, usage RT+UAV) | 92/80/110/90 | **0.0 %** |
| Depth (Depth32Float_Stencil8) | 260 | not readable by the dump yet (next build reads it) |
| R8Uint 736x416 (stencil copy / lighting channels) | 13 | 99.3 % |
| R8Unorm 736x416 (shading mask / custom depth mask) | 10 | 99.4 % |
| RG16Unorm x2, RGBA8, RG8 736x416 (compute-written, usage read+write) | 60/70/30 | 99.4-100 % |
| RGBA16F 736x416 and 368x208 (post chain inputs) | 115 | 100 % |
| Back buffer 960x540 | 90 | 100 % (the loading image) |

The 99.4 % pattern is 304,290 of 306,176 pixels: full screen minus a ~43x43 area.
Whatever writes those textures sees geometry coverage almost everywhere.

Census of the same frame (`[draw-dump]` / `[dispatch-dump]`, one frame at present
1800): 233 draws with 0 colour targets (depth pre-pass + shadow depth),
135 draws with 6 colour targets (`vs='Main' ps='MainPS'`, depth 736x416 DXGI 19),
30 with 3, 2 with 1, and 0 skipped. Vertex buffers, index buffers, descriptor
tables all bound. Nanite ON gives the same GBuffer result with 129 per-material
compute shading dispatches (`MainCS`, indirect, offsets honoured) also writing
nothing, while Nanite's own raster + depth export DID populate the shading mask.

So: geometry is submitted, rasterised at least for depth-derived passes, and the
colour outputs of both the MRT draw path and the compute shading path are lost.

## Ranked candidates (none verified — do not "fix" without the next run's data)

1. **Depth test EQUAL after the pre-pass, non-invariant position.** UE's base pass
   uses `DepthFunc = EQUAL` (depth write off) against the pre-pass depth. Metal
   Shader Converter output may not be position-invariant between the pre-pass
   VS and the base-pass VS (fast-math), so EQUAL fails for every fragment. This
   is the classic Metal port failure and matches "depth-derived masks written,
   colour never written". Discriminator: next run's census prints each draw's
   depth func/write and the dump reads the depth buffer. If depth is populated
   and the 6-MRT draws use EQUAL → this is it. Candidate remedy: treat EQUAL
   with depth-write-off as GREATER_EQUAL/LESS_EQUAL (per the pre-pass function),
   or get invariant positions from the converter.
2. **Winding / cull.** `p->raster.winding` maps FrontCounterClockwise directly and
   cull FRONT/BACK directly; if the projection Y convention differs from D3D,
   every back-face-culled mesh is culled. UI draws are cull-none so they would
   survive. Discriminator: same census fields (cull, winding) + depth content
   (a culled pre-pass leaves depth at its clear value).
3. **Viewport/scissor.** Applied from `e->vp` / `e->sc` (clamped to attachment).
   Census will print them per draw.
4. **Colour write masks / blend for MRT > 1.** `rp.colors[i].write_mask` is set per
   target from the PSO blend desc; would not explain target 0 being empty.
5. Nanite compute shading: texture UAV descriptors carry only
   `texture_view_id = r->gpu_resource_id`, metadata 0 (madeira_d3d12.c
   `device_CreateUnorderedAccessView`). Other compute UAV texture writes do
   land, so this is secondary; it would matter again once Nanite is turned back on.

## Tooling that exists now (use it, do not rebuild it)

- **Host frame dump** (`research/remote-metal/host/rmetald.m`, running binary):
  `touch /tmp/rmetald-dump-now` → next present writes every render-pass
  attachment AND every live GPU-written 2D texture ≥200x100 as PNG under
  `/tmp/rmetald-dump/fN/` with one `[rmetald-dump]` log line each
  (size, Metal format, usage, non-zero pixel ratio). Auto-dumps at host presents
  1500..3200 every 100 (`touch /tmp/rmetald-no-autodump` to stop). Log:
  `/private/tmp/rmetald-ml885.log` (supervisor loop relaunches `host/rmetald`
  on exit; replace the binary then `pkill -x rmetald`).
- **Client census** (`research/madeira-d3d12/src/pe/madeira_d3d12.c`): one frame
  of `[draw-dump]`/`[dispatch-dump]` lines at command lists 12000-12399 (menu)
  and at presents 1500,1700,...,2900 (`g_census_on`). ml903 adds per-draw
  cull/winding/depth-func/depth-write/viewport/scissor/write-mask fields.
- **Engine crash context**: `Saved/Crashes/UECC-*/CrashContext.runtime-xml`
  (UTF-16) in the game's `%LOCALAPPDATA%` — module+offset callstack; symbolise
  offline with the pulled exe (`scratchpad/pefun.py`, `rng.py`, full objdump
  `exe-text.asm`, call graph `.calls.pkl`). This Shipping build writes no log
  file (`-abslog` is ignored).
- **Config that the engine actually reads**: ONLY
  `<game>\Empire\Config\UserEngine.ini` (the two `Unreal Engine\...` user
  layers are never created — Wine cmd fails on those paths). The launch batch
  rewrites it every start. Current contents: `r.RDG.TransientAllocator=0`
  (required: the transient allocator returns NULL and RDG dereferences it),
  streaming/registration time budgets = 2000 ms, Lumen/VSM/volumetrics/sky
  capture off, `r.Nanite=0` (temporary, for the black-scene bisection).

## Closed problems (do not re-investigate)

- App SIGABRT in `CA::Render::Encoder::grow` / SwiftUI AsyncRenderer trap =
  CoreAnimation's `VM_MEMORY_LAYERKIT` (tag 51) range starved. Measured at
  startup (`[layerkit-range]`, 9.7 GB, randomised per launch, always inside the
  35-48 GB fixed-mappable window). ml900-ml902: Wine view placement skips it
  (soft), emulator arena retried outside it (falls back inside; boot must never
  lose the arena), `anon_mmap_alloc` asks for the heap range first. Last run:
  0 refusals in 32 min.
- On-screen keys were SwiftUI Buttons; their press animation ran on the async
  renderer and died. Now `HoldKeyView` (gesture, no animation). Keep it so.
- RDG transient allocator NULL → guest RenderThread fault: the CVar above.
- Electra media (UE's player; Windows path = Media Foundation; no H.264 decoder
  in the prefix, `msmpeg2vdec.dll` missing): pressing Enter at the LOADING
  SCREEN triggers a teardown race → `Electra::ExecAsync` faults → CRT R6025.
  Work-around: one Enter on Story, hands off. Candidate real fix (untested):
  `set WINEDLLOVERRIDES=mfplat,mf,mfreadwrite=` in the batch so Electra fails at
  init. The loading widget is probably waiting for this media to end.
- Frame time ~2-4 s: ~7,500 remote round trips per 1.7 s, one per compute
  dispatch (`RM_OP_COMPUTE_INTO`) plus shadow hashing. Batching compute encodes
  into the batch messages is the perf lever; unrelated to the black scene.
- rmetald assertion "encoder released without endEncoding" on level teardown:
  fixed (open-encoder registry).

## How to run and read one iteration

1. Install the current IPA (`Madeira-ml9NN.ipa` in the repo root), launch the
   virtual desktop, run `Empire-dx12.bat`, wait for the menu (~5 min), ONE Enter
   on Story, hands off. Level render starts ~8 min later (present ~1800).
2. Pull the log: `ssh -p 22222 root@192.168.64.5 base64 <Documents>/madeira-log.txt`
   (see `memory/project_empire_ants_demo_candidate.md` for the container path).
   `madeira-log.prev.txt` holds the previous run.
3. Host dumps as above; census lines in the pulled log.

The next decisive run is: ml903 census fields + depth readback, Nanite off.

## Addendum (after Astra's audit) — ml904

Two contract defects Astra found in the runtime, both on the base-pass path, are
implemented in ml904 (`Madeira-ml904.ipa`), still untested at the time of writing:

1. **Zero-stride vertex streams.** `exec_draw` promoted a bound stream with
   `StrideInBytes == 0` to the pipeline's packed stride. The six-target base-pass
   draws bind slot 1 with stride 0 and a per-draw 4-byte offset (a constant
   per-draw stream), so every vertex read a different element. Now the bound
   stride is honoured; a zero stride becomes a Metal constant-step layout
   (`MTLVertexStepFunctionConstant`, step rate 0), keeping the packed stride for
   the element layout, with a second attempt at stride 0 if Metal refuses.
   Failure falls back to the base pipeline and logs `stride variant failed (...
   slot1 stride N)`.
2. **ClearDepthStencilView flags.** Flags, the stencil value and rects were
   ignored and both aspects were always cleared. Now the flags select which
   aspect gets a Clear load action (the other loads), the stencil value is
   carried, a second clear naming the other aspect merges, rects are still
   ignored (logged). The first 24 clears are logged with flags/values.
3. Census now also prints stencil enable/func/read/write masks and the
   reference (`st=...`).

Still open from the audit: RTV/DSV descriptors discard the view description
(mip/slice), so two views of one resource are treated as the same attachment;
the dump measures at presentation, not per pass; do NOT replace EQUAL depth
tests globally.

## Addendum 2 — ml905: depth was never written; typed buffer views implemented

ml904 run (ants49, Nanite off, host depth readback working): the scene depth
buffer (Depth32Float_Stencil8 736x416) is **0.0 % written** after a frame with
113 depth-only draws (`dtest=1/func7/w1` = GREATER_EQUAL + write, cull back or
none, viewport 735x414 full, scissor full, stencil off/ref 0), while the shadow
depth maps are fully written. So scene geometry produces no fragments at all:
the vertex stage, not shading or depth test. The depth-only draws bind slot 0
with a 4-byte stride — UE's manual vertex fetch: positions/tangents/UVs are
read in the VS from `Buffer<float>`/`Buffer<float4>` SRVs (typed buffers), the
IA stream only carries ids.

`device_CreateShaderResourceView` encoded typed buffer SRVs as raw buffer
descriptors (address+size, no texture view, typed bit never set — the comment
said so). The converter compiles `Buffer<T>.Load` as a texture-buffer read, so
those loads returned nothing → every vertex at the origin → no fragments.

ml905 adds `mad_typed_buffer_view()`: a texture-buffer view
(`MTLBuffer_newTexture`, `WMTTextureTypeTextureBuffer`) per (format, element
offset, count, access) cached on the resource (16 entries, released with it,
added to the residency set), descriptor = gpuVA+byte offset, textureViewID,
metadata = size | element-offset<<32 | typed bit 63 (IRDescriptorTableSetBufferView
contract). Applied to typed SRVs and typed UAVs (not RAW, not structured).
Logs `typed buffer view: ...` for the first 12 and `typed buffer view FAILED`
if Metal refuses one (then the old raw descriptor is used). Also in ml905: the
ml904 constant-step layout crashed rmetald (Metal asserts stepRate 0 for
constant step; host/on-device decode forced 1) — fixed both.

Electra: ants49 crashed on `Electra::ExecAsync` at present ~1800 with NO input
after the Story selection, i.e. the teardown race fires when the media stops on
its own (level ready?), not only on Enter. That crash now gates seeing the
level; `WINEDLLOVERRIDES=mfplat,mf,mfreadwrite=` in the batch is the untested
candidate.

## Addendum 3 — ml905 run (ants50): typed buffer views did NOT change the result

- Typed views were created (12 logged, 0 failed: R32_UINT, RGBA32F, RGBA8,
  R32F, RGBA8_SNORM, incl. UAV variants) — the implementation works as far as
  Metal is concerned, but the scene depth is STILL 0.0 % and all GBuffers 0.0 %.
- Looking at the PNGs: the "99.4 %" R8/RG16/RGBA8 textures are UNIFORM fills
  (white with a thin black border), not geometry coverage. The back buffer of
  this run is pure black (no loading widget was drawn this time; the user saw
  black at ~3 FPS). So NOTHING scene-related rasterises; every earlier
  "coverage" reading was a clear/fill.
- In this frame the depth-only draws are ExecuteIndirect draws (`INDIRECT
  args-off=...`, slot0/slot1 stride 4 = instance/id streams, args from a
  GPU-written buffer) and the six-target base-pass draws are plain indexed
  draws with a real position stream (slot0 stride 12) + constant slot1. Both
  produce nothing, so the failure is not specific to typed fetch.
- Electra teardown fault at present ~1800 again (cycle 393), with no input
  after Story.
- Host dump now also prints `uniform=NN%` per texture (share of pixels equal to
  the centre pixel), so a cleared depth/shadow map (uniform ~100 %) is
  distinguishable from one with geometry. The shadow-map pass draws the same
  scene meshes with the same vertex factories; if the shadow maps show
  variation, the vertex path works and the main view's View/projection is the
  problem; if they are uniform too, the vertex path is globally broken.
- Remaining candidates, none verified: the View uniform buffer / root CBV
  contents as seen by the VS (matrices zero → w=0 → everything clipped), the
  constant-step slot's attribute mapping, the converted VS reading the
  descriptor heap (stage visibility / argument buffer for the vertex stage), a
  viewport/projection convention. The decisive tool is a CONTROLLED guest test
  (`research/madeira-d3d12/tests/windows/`, cube canary infrastructure) that
  draws a triangle via (a) real vertex stream + root CBV matrix, (b) SV_VertexID
  + Buffer<float> SRV fetch, (c) a constant-step slot, and reads back the RT.
  HLSL → DXIL is compiled with the Homebrew Wine vkd3d chain (see memory).
- VM IP changed to 192.168.64.6 (was .5); rmetald still listens on 192.168.64.1.

## Addendum 4 — controlled vertex-path test (ml906) is built and installed

`research/madeira-d3d12/tests/windows/vfetch_test.c` + `shaders/vfetch.hlsl`
(DXIL via `wine toolchains/dxc-win/bin/x64/dxc.exe`, header `vfetch_dxil.h`;
built by `build/madeira-d3d12/build-pe.sh` as `d3d12-vfetch-x64.exe`).
Installed on the VM at `C:\windows\sysx64\d3d12-vfetch-x64.exe` with
`Desktop\vfetch-test.bat` (writes `C:\vfetch-result.txt`; every line also goes
to the app log as `[vfetch] ...`). Also staged in
`app/Madeira/arm64ec-windows/` for the on-device (local Metal) run.

Cases, all through the production runtime, each into a cleared 64x64 target
read back and counted, corners + centre printed:
1 vertex stream + identity matrix (root CBV is root parameter 1, at byte 256 of
  its buffer; root parameter 0 is a 32-bit constant) → whole target CBV colour
2 same with a 0.5 scale matrix → coverage ~9/16; the uncovered corner reveals
  the Y convention
3 positions/ids through typed Buffer<float>/Buffer<uint> SRVs at FirstElement
  64/16 with sentinels around, table at heap index 5 → pixel colour = id 0x0304
4 constant-step (stride 0) id stream, sentinels beside the intended element →
  pixel colour = id 0x0102 (a stepping bug shows 0xBBBB etc.)
5 depth: near draw, stencil-only clear (depth 1.0 given, must be ignored), far
  draw with LESS → far rejected
6 case 1 via ExecuteIndirect, args at byte 100 of an upload buffer
7 compute probe: CBV colour/matrix row, PosBuf[0..3], IdBuf[0..3] read by a CS
  through the same bindings and written to a UAV, read back and printed

## Addendum 5 — ml906 result on the VM, and the ml907 split

ml906 (runtime ml905, remote Metal on the VM): cases 1, 2, 3, 4, 6 PASS
(stream + CBV at param 1; half-scale; typed-buffer fetch at FirstElement 64/16;
constant-step id stream; ExecuteIndirect at byte 100). Two FAILS:
- case 5: A=0 B=4096 — the far draw covered everything. Indistinguishable
  between "depth test never works" and "depth lost across the stencil-only
  clear" from this data alone.
- case 7: the compute probe read back ALL zeros (CBV, matrix, typed buffers).
  Indistinguishable between "compute reads see zeros" and "the compute WRITE
  (table UAV, structured) never lands / never reads back". The dispatch did
  execute (list executed: 9 commands, 1 draws), the CS root layout matches the
  VS/PS one, no host-side compute skips were logged.

ml907 (test only, runtime unchanged) splits both:
- 5a near→far in ONE pass, no clear between (depth test alive?); 5b far→near
  (LESS passes and writes land?); 5c the original stencil-only-clear pattern.
- 7a write-only kernel through the table UAV (write/readback path alone);
  7b the original probe; 7c the same probe through a ROOT UAV (new root
  parameter 4, `UAV(u1)`) so table-UAV vs root-UAV is separated.
Installed at C:\windows\sysx64\d3d12-vfetch-x64.exe (165376 bytes,
md5 fec07fed3caae98a855d566744382331); Desktop\vfetch-test.bat unchanged.

## Addendum 6 — ml907/ml908 results: ONE runtime bug found and fixed (27/27)

ml907 split (runtime still ml905): 5a PASS, 5b PASS (the depth test works
within a pass, LESS + writes are right), 5c FAIL (depth lost across the
stencil-only clear). 7a/7b/7c all PASS — including the original probe that
read zeros on ml906; 7d (ml908) replays the ml906 shape exactly (dispatch opens
the command buffer, root param 4 unset) and PASSES. The ml906 compute zeros did
not reproduce; treat them as unexplained, not as a bindings bug.

A native Mac Metal test (scratchpad depth_stencil_clear.mm) showed Metal keeps
depth for "pass 2 = depth LOAD + stencil CLEAR" on Depth32Float_Stencil8 in all
four variants, so the loss was ours.

ROOT CAUSE (ml904 clear merge, exec_add_clear): a consumed pending clear is
dropped by `pend[i] = pend[--npend]`, which leaves the vacated slot's bytes in
place. The next clear allocated that slot and the merge test read the STALE
entry (same resource, is_depth, flags=3), so a stencil-only clear was OR'd into
a phantom depth+stencil clear -> depth cleared to 1.0 -> every draw after it
tests against an empty depth buffer. This is exactly UE5's depth prepass ->
stencil clear -> base pass order, so the prepass result was discarded before
the base pass ran.

FIX (ml908): merge only into an entry that is still pending (index found before
allocation). vfetch on the VM: 27 checks, 0 failed, implementation marker
Sep 15 05:50. Deployed by overwriting the DLL inside the installed bundle on
the VM (the loader maps system DLLs from the bundle, NOT from a file dropped
into the prefix's sysx64 — the symlink replacement was ignored); ml905 backup
at /var/root/madeira_d3d12.dll.ml905.

NOT yet verified: whether the game now shows geometry. Depth was one of the
"never written" symptoms; the GBuffer 0.0% symptom may or may not share the
cause.

## Addendum 7 — ml908 game run (ants52): dies at ~4.5 min, before the level; the blocker is media teardown, not rendering

Run: ml908 runtime confirmed loaded (marker Sep 15 05:50). Enter on Story ->
fade to black -> stays black -> at Present #1800 the app's remote-Metal
session drops ("client gone" on rmetald) and the game is in its crash handler
(app process stays alive, GameThread parked in a server wait, 7 threads
waiting > 60 s). rmetald counted 128 blank presents (no draws) out of the last
~130 before the end: the game had stopped drawing anything (a movie that we
cannot decode is playing = black), it was not a rendering fault.

CrashContext.runtime-xml (AppData\Local\Empire\Saved\Crashes\UECC-*): the last
THREE runs all say `ErrorMessage = Unhandled Exception: 0x80000005`,
`CrashType = Crash`, SecondsSinceStart 254 / 276 / 259. The one before those
was "GPU Crash dump Triggered" at 171 s. So the death is deterministic at
~4.3-4.6 min of game time, regardless of input.

Native-fault order in the app log (ants52, first fault of the run = SEGV #1):
1. tid 0158 "Electra::ExecAsync": c0000005 at exe RVA 0x5125a98 —
   `mov rax,[rcx]; call [rax+0x18]` with rcx = 0: a COM method call on a NULL
   interface pointer (4th vtable slot). Same RVA as the earlier Electra fault.
2. tid 0178 "AudioMixerRenderThread(1)": c0000005 at RVA 0xF32882 inside UE's
   lock-free list Push (cmpxchg [rbp] with rbp = 0, list head freed) — the
   audio mixer being torn down under the render thread. Handled by UE's
   __except (hit=2).
3. tid 0098 (unnamed, created at startup = UE's crash-reporting thread; it is
   the one suspending every thread and calling PSAPI/RtlCreateQueryDebugBuffer/
   dbgcore MiniDumpWriteDump, which is missing -> c06d007f): after
   UnregisterAudioSessionNotification + "Mismatched CoUninitialize" it faults
   itself at RVA 0xF46D55 (another lock acquire on a NULL object) and that one
   reaches the unhandled-exception filter frame.
Faults 2 and 3 are consequences of the engine teardown started by 1.

Media facts: every movie the game ships is MP4 (Content\Movies\*.mp4:
C4_Memory_3/6, MOVIE_LoreMemory_Hub2_001/002) -> Electra -> Windows Media
Foundation. mf.dll / mfplat.dll load, `msmpeg2vdec.dll` (the H.264 MFT) is
MISSING (dll-missing #24), and there is no winegstreamer on iOS. Electra
therefore has a player with no decoder; the video shows black and its
teardown (movie end, or Enter skipping it) calls through a NULL interface.
Bink is only used for the splash (Saved\Bink\Movies\Spl...).

The reported code 0x80000005 is NOT raised by the exe (the only 0x80000005
immediates are NGX parameter values and a CPUID leaf) and never appears in our
exception logs; it is exactly EXCEPTION_ACCESS_VIOLATION (0xC0000005) with bit
30 cleared. Suspect the EXCEPTION_RECORD handed to the x64 __except filter
(FEX RethrowGuestException / EC dispatch) loses bit 30 somewhere. Cosmetic for
the crash itself, but worth a look because UE's filter logic keys on the code.

Options (none applied — not confident):
a) `set WINEDLLOVERRIDES=mfplat,mf,mfreadwrite=` in the batch so Electra's
   WMF path fails at startup instead of at teardown. Risk: Electra delay-loads
   mfplat and may fault at module start; unknown whether it degrades cleanly.
b) Diagnostic only: move the four .mp4 files aside so the cutscene/loading
   movie cannot open, to see whether the level renders after the black phase.
   Not a shippable fix (per-game), only to answer "is media the last blocker".
c) Real fix: an H.264 MFT for iOS (VideoToolbox-backed winegstreamer
   replacement) — large.
Also open: `dbgcore.dll` missing makes the crash handler raise c06d007f; harmless.

## Addendum 8 — CORRECTION to addendum 7 (Astra): the 0x80000005 was OUR LOGGER

Astra found the initiating event: ants52 line 46151 — five `[draw-dump]`
records truncated at 512 bytes (no newline) on tid 00f0 (RHISubmissionThread),
then that thread's SEH dispatch. `d3d12_log` formatted into a 512-byte buffer,
so a long census record lost its newline; `__wine_dbg_output` accumulates
fragments until a newline; ntdll's `append_output` (wine/dlls/ntdll/thread.c,
1020-byte `output[]`) then calls `RtlRaiseStatus(STATUS_BUFFER_OVERFLOW)` =
0x80000005 on the calling thread. The engine's submission thread died of our
census, the engine tore down, and the Electra / audio-mixer faults in addendum 7
are consequences. No exception-code bit was lost. The ~present-1800 timing is
the census window (`g_census_on`). Same signature in ants49/50/52.

Fix (ml909): `d3d12_log` now uses a 1000-byte buffer (< 1020) and forces a
terminating newline on every record, truncated or not; new export
`MadeiraD3D12LogProbe(msg, repeat)` drives the production logger from the test;
vfetch case 8 sends 40 x 3000-byte + 300 x newline-less records under a
vectored handler and requires no exception. Deployed into the VM bundle
(md5 64d6bec8...), test exe into sysx64 (md5 707e136a...). Media Foundation and
the movies are UNCHANGED, as Astra asked. Whether the level renders after
present 1800 is still to be observed.

## Addendum 9 — ml910 capture build; ants54 died early of a NEW, unrelated fault

ml910 adds a GPU-ordered draw capture (exec_capture_draw / mad_capture_flush):
in census frames it ends the pass and blits, before the draw, the indirect
args (or first indices), the first 48 bytes of streams 0-2 and 64 bytes of
each root CBV into a shared ring, printed after the fence wait as
`[cap]` / `[cap-data]` records. It never ran in ants54 (0 records): the run
died at t+195 s while the MENU was loading, before any census frame with
level draws.

ants54 primary fault (new; absent from ants52/53): GameThread (0094) read of
0x1e3090118 = `lock xadd [rcx+0x38], -1` (a refcount Release) at exe RVA
0x29e062d, function [0x29e05f8,0x29e065d). rcx = 0x1e30900e0 lies in a 32 MB
anonymous region 0x1e2000000+0x1f40000, prot=1 max=1 share_mode=3 user_tag=35,
"never resident, refcnt=0, every page fails to materialise" -- a mapping whose
backing memory object is gone. Neighbouring tag-35 32 MB regions exist
(0x1e4000000). Candidates: a Metal shared-buffer mapping (an UPLOAD/READBACK
resource's contents) torn down while the engine still held an object inside
it, or a refcount early-release on our side. Followed by the usual crash
cascade (Electra 0x5125a98, crash thread, audio). No capture code involved.
Treat as intermittent until it repeats; if it does, identify who maps tag-35
32 MB regions (grep the phys-map inventory) and log res_Release / shadow
unmaps with addresses.

## Addendum 10 — ml911 captures, ml912 draw-argument contract, and a flaky compute drop

ml911 (ants56, 15 min, no faults): the descriptor tables of the captured
scene draws decode cleanly; the buffers they point at (GPUScene: instance
ids 0x01005b09.., a 1.5 MB table, a 64 KB buffer with real floats like
2601.7/-62.57/108.17, a 1.3 MB buffer starting with an identity matrix) hold
real data, not zeros. So everything the GPU receives at a scene draw looks
valid: indirect args, indices, positions, primitive-id stream, View matrix,
GPUScene. Cull mode is not it either (997 of the frame's scene draws use
cull NONE).

ml912: a documented contract we violated. metal_irconverter_runtime.h's
IRRuntimeDraw* helpers bind, for EVERY draw, the draw's argument block at
vertex buffer index 4 (kIRArgumentBufferDrawArgumentsBindPoint,
IRRuntimeDrawParams = the D3D12 argument block verbatim) and a uint16
index-type flag at index 5 (kIRArgumentBufferUniformsBindPoint: 0 =
non-indexed, MTLIndexType+1 for indexed). Converted vertex shaders that use
SV_VertexID / SV_InstanceID read them. exec_draw now writes the block into
the draw's argument slot (slot grown 512 -> 576 bytes, params at +512, flag
at +544) for direct draws and binds the indirect argument buffer itself for
indirect draws. vfetch still passes its draw cases (28 checks, only 7d
failed, see below). Whether it makes the level appear is the pending run.

Flaky compute drop (case 7d = ml906 shape: a Dispatch that opens a fresh
command buffer, no blit before it): passed in ml908b/ml909/ml909b, FAILED in
ml906 and ml912 -- the probe kept the previous case's rows, i.e. the dispatch
did not execute or did not land. Host log shows nothing (no compute skips,
no encoder-ended-early, no shader-validation report). Readback is synchronous
(RM_OP_WAIT_COMPLETED then wmtr_rb_drain), so it is not a stale download.
Buffer UAVs are NOT added to uav_res (CreateUnorderedAccessView returns early
for buffers) so the compute encoder relies on the residency set alone for
them; texture UAVs get useResource. Unproven suspect. If the game's culling
dispatches drop the same way, indirect counts would be stale, but the ml910
captures showed live counts.

Host: rmetald now runs with MTL_SHADER_VALIDATION=1 (reports invalid device
loads/stores to the log). MTL_DEBUG_LAYER was tried and dropped: it logs
"redundant state" for every draw.

## Addendum 11 — ml912 result, host shader-validation findings, ml913, and the GPU-trace plan

### ml912 (draw arguments at bind points 4/5) did NOT change the outcome
ants59, 18 min, no faults: level drawing 524 draws/frame at presents
1800-2800 (121 base-pass draws into 6 targets, 74 scene-depth draws, 108
shadow draws). Host dump at present ~2900: scene depth 0.0% written, all six
GBuffers uniform, shadow atlas uniform at its clear value. The MENU also has a
3D scene (presents 427-440: up to 322 base-pass draws) and it is equally
black. Cull mode is not it (997/1998 scene draws use cull NONE). The captured
draws (ml910/ml911) had real indices, positions, ids, indirect counts, View
matrix and GPUScene data.

### Host GPU shader validation (MTL_SHADER_VALIDATION=1) on the ml912 run
Three fault classes, ALL in compute kernels; ZERO reports from any vertex or
fragment function over ~300 frames:
1. 6520x "Invalid texture type MTLTextureType2D bound to shader, expected
   MTLTextureType2DArray" (+18 the reverse) in kernels named MainCS. Cause:
   CreateShaderResourceView/UAV ignored ViewDimension (77 Texture2DArray, 35
   Texture3D, 3 TextureCube, 1 CubeArray SRVs per run were "treated as
   TEXTURE2D"). Metal only binds a texture to a slot of the same type.
   Addressed in ml913 (unverified until a run with ml913+): mad_texture_view_id() makes cached MTLTexture views of the
   requested type/levels/slices (2D, 2DArray, Cube, CubeArray, 3D, 1D, MS) for
   SRVs and UAVs; resources record tex_type/tex_pf/mips/layers.
2. 8132x "Invalid device load at offset 72 / 24 ... buffer: Out of bounds of
   user address space" from the SAME five compute pipelines: a pointer inside
   no live allocation. Offsets 24/72 look like constant-buffer fields. ml913
   adds exec_desc_check(): in census frames, every buffer descriptor in the
   bound tables of each draw/dispatch is resolved against the live resource
   list; a miss prints `[desc-check] '<shader>' table pN heap[i]: va=... points
   at NO live resource`. Not yet run. This is DIAGNOSTIC only; the invalid
   loads themselves are not fixed.
3. 1x ScatterCopyCS store at offset 346144 into a 196608-byte buffer (GPUScene
   upload past the end of its destination; once, probably at a resize).
The debug layer (MTL_DEBUG_LAYER) was also tried: it only produced "redundant
state" and "unused binding at index 0/2/4/5" (fragment stage does not read
those) -- and it stalls every compute batch ~0.7 s, so it is off again.

### Case 7d flake
Passed 3 runs, failed with ml906 and ml912: the dispatch that opens a fresh
command buffer sometimes does not land (probe keeps the previous rows). Host
logs nothing. Buffer UAVs are never added to uav_res (only textures), so a
compute encoder gets no useResource for them and relies on the residency set.
Unproven; recorded, not fixed.

### What is left for "vertex outputs never rasterize"
Inputs verified; reads verified valid by Metal itself; draw args now bound;
depth/stencil/clear/cull/viewport/scissor verified. The only unobserved thing
is the vertex stage's OUTPUT. Plan (ml914, host): touch
/tmp/rmetald-gputrace-now at present time -> MTLCaptureManager captures one
full frame to /tmp/rmetald-N.gputrace (rmetald restarted with
MTL_CAPTURE_ENABLED=1, validation off). Open it in Xcode, pick a
drawIndexedPrimitives in the 736x416 depth-only pass (or a base pass with six
colour attachments), and use the Geometry viewer: it shows post-transform
positions per vertex. Zero/NaN/huge w, or all vertices identical, or
everything behind the camera each point at a different cause (GPUScene
decode, View matrix row/column convention, LWC tile math, attribute mapping).
Also worth reading in the trace: the Bound Resources of that draw (attribute
layout vs stage-in, buffer 4/5 contents).

Open questions for Astra: (a) with reads valid and inputs valid, which
conversion-level difference between the vfetch shaders (pass) and UE's base
pass VS (fail) is most likely -- matrix majorness as consumed through the
root CBV (UE's HLSL uses row_major float4x4 with mul(v, M)); the DF/LWC
double-float helpers; SV_InstanceID -> GPUScene instance index; or the
stride-0 primitive-id stream being per-instance in UE's layout? (b) is a
Metal vertex descriptor with a per-instance stride-0 stream plus
[[base_instance]] semantics equivalent to D3D's for instanceCount 890?

## Addendum 12 — Astra's review of addendum 11, and ml914

Astra: (1) our direct indexed draws put StartIndexLocation (elements) in
IRRuntimeDrawIndexedArgument.startIndexLocation, while Apple's helper stores
the index buffer BYTE offset handed to Metal. ml914 stores
ib_off + start*index_size, exactly what the Metal draw uses. (2) "vertex
reads valid" is not "vertex inputs correct": a shader can legally read the
wrong/stale/zeroed buffer; the compute invalid loads and the GPUScene
out-of-range store can corrupt inputs upstream. Agreed; the ml913 descriptor
check is diagnostic, the loads are not fixed. (3) no speculative transposes /
winding flips / depth overrides. Agreed; none applied.

vfetch ml914 adds: 9a indexed draw with index-buffer offset 8, start index 4,
BaseVertexLocation 64 through the SV_VertexID shader (expects id 0x0506 via
wide typed views at heap 7/8); 9b DrawInstanced(3 verts, 3 instances,
StartInstanceLocation 5) through a new VSInst (colour = SV_InstanceID):
expects the last instance to paint (2,0,0), not (7,0,0).

Trace procedure (host now runs with MTL_CAPTURE_ENABLED=1, validation off):
touch /tmp/rmetald-gputrace-now while the level is drawing -> one frame to
/tmp/rmetald-N.gputrace. In Xcode: open the trace, pick a
drawIndexedPrimitives in the 736x416 depth-only pass or a 6-attachment base
pass, then (a) Geometry viewer: post-transform positions; (b) Bound
Resources: buffer 4/5 contents, stage-in attribute layout, the primitive-id
constant; (c) follow that id into the GPUScene buffers bound in the same
draw; (d) if wrong, the last compute dispatch that wrote them.

## Addendum 13 — THE LEVEL RENDERS (ml913 + ml914, run ants60)

Host frame dump f19 taken at rmetald present ~7536 while the loading overlay
was still up: scene depth 736x416 99.4% non-uniform (a real depth image:
terrain, a cave mouth, foreground ground), GBuffers att06/09/10/11 99.4%
non-uniform (base colour shows an ANT standing on a rock in front of a cave),
shadow atlas 54.8% uniform (was 100%), the 1024x1024 shadow map non-uniform.
Every earlier run had 0.0% on all of these. PNGs saved in the scratchpad
`first-render/` (att00 depth, att09/att11 GBuffers, att48 back buffer).

What changed between the last black run (ml912, ants59) and this one:
- ml913: dimension-correct texture views for SRVs/UAVs (2D/2DArray/Cube/3D/1D,
  mips, slices), replacing "treated as TEXTURE2D".
- ml914: IRRuntimeDrawIndexedArgument.startIndexLocation = index buffer BYTE
  offset handed to Metal (Apple's helper convention), instead of the D3D
  element count.
Both shipped together, so which one unblocked rasterization is NOT isolated.
The prepass vertex shaders sample no textures, which points at ml914, but
that is inference; an A/B (ml913 alone) would settle it if it matters.
vfetch: 31/31 (cases 9a1/9a2/9b confirm D3D SV_VertexID / base vertex /
SV_InstanceID semantics through the bind-point-4/5 block).

Still open: the intermittent compute drop (case 7d, 2 of 7), the compute
invalid loads (8132/run, five MainCS pipelines) and the one GPUScene
out-of-range store, the [desc-check] hits (all one 64 KB buffer at
0x100484a8000, probably a released or null buffer still referenced by table
entries the shader may not read), and the loading overlay: with the world
rendering, whether the game leaves the loading screen is the next observation.

Host: the one-frame GPU trace (ml914 trigger) STALLED rmetald: capture started
at present 7536, /tmp/rmetald-1.gputrace reached 3.1 GB and stopped growing,
no further present, rmetald idle at 0% CPU with 13.8 GB resident, main thread
in the AppKit run loop. Killed and restarted. Do not trigger the trace on a
full frame again without a size bound (fewer command buffers, or
MTLCaptureDestinationDeveloperTools with Xcode attached).

## Addendum 14 — the level renders, the game's loading widget never leaves (ants61)

Facts from the ml914 run at t+482 s (loading image on screen the whole time):
- The world is rendered every frame behind the overlay: scene depth 99.3%
  written, GBuffers non-uniform, 524 draws/frame (census presents 2000-2200),
  ~3 presents/s. Back buffer = the loading image (uniform=0% because of the
  image itself).
- No faults. Memory flat at 2.35 GB for 5 min. NO new resources: 0 heap
  allocations, 0 texture creations, 0 typed/texture views in the last 4 min;
  transport upload volume constant (~85 MB / 30 s of per-frame constants).
  So no texture streaming is in flight.
- GameThread 3-5% CPU, in the wineserver wait most of the time; alert storms
  RenderThread->GameThread (frame sync) and GameThread->FAsyncLoadingThread
  (the async loader is idle, waiting for work). One guest sample of the game
  thread: exe RVA 0x1a51595 with a return chain 0x1d1d041 / 0x1b48791 /
  0x1b4fe6b / 0x1b768b7 -- generic engine functions, no string references.
- Threads parked > 60 s: only the four BinkAsy + Bink IO (idle since the
  splash) and OutputDeviceRedirector. PSOPrecompilePool #0-5 alive and idle.
- No media activity (Electra thread idle, no mfplat calls after startup).
- Runtime: NO "unimplemented" notes at all this run; refused QIs are the usual
  ID3D12Device9+/GraphicsCommandList5+ ones; 10 TIMESTAMP query heaps whose
  results resolve to zero; ResolveQueryData answers occlusion "visible".
- The overlay is the game's own `ULoadingScreenWidget` (states Hidden,
  BlackFadein/Visible/Fadeout, ImageFadein/ImageVisible/ImageFadeout;
  "LoadingImages", "MissionLevel", "WinSequence"...). The exe also references
  `IsStreamingCompleted`. Which condition ends ImageVisible is unknown.

Hypotheses, in order:
1. The widget waits for `IsStreamingCompleted()` and the texture streamer has
   requests that never complete under our runtime (nothing streams, memory
   flat). Test: instrument counts of CreateCommittedResource(texture),
   CopyTextureRegion(B2T) and fence GetCompletedValue per 10 s; if requests
   are issued but never finish, look at what completion the streamer polls.
2. The widget waits on GPU timestamp queries (10 TIMESTAMP heaps, all zero)
   -- e.g. a "GPU ready / frame time" gate. Test: return a monotonically
   increasing timestamp from ResolveQueryData for TIMESTAMP heaps (and a
   sane GetTimestampFrequency) -- honest, cheap, and it also unblocks any
   "stat gpu" logic.
3. The widget waits for a player input ("ImageVisible" until a key). Test:
   press Enter / Space / Esc (free).
4. The engine loading screen only: `-NoLoadingScreen` variant batch staged at
   `<game>\Empire-dx12-nols.bat` (engine switch; the custom widget probably
   ignores it).

Not tried: any of them (user's call). Nothing else changed; rmetald is back
to a plain run (MTL_CAPTURE_ENABLED=1 harmless). vfetch 31/31 on ml914.

## Addendum 15 — IN GAME. HUD visible, world black: the temporal upscaler outputs zero

The loading widget left after the user pressed keys (Enter/Space/Esc: it
waits for input). HUD renders ("Exit the cave", objective marker, top-right
counters). The world is black on the back buffer.

Host dump f20 (in game, ml914), tracing the chain by texture:
- Base pass MRT0 = SceneColor, 736x416 RG11B10F (att06): 69% nonzero,
  non-uniform -- a LIT cave interior with daylight through the opening.
  Lighting works (DeferredLightPixelMain x6, sky, shadow projection all run).
- Post-process outputs at 816x460 (screen percentage 85 -> 960x540), three
  RGB10A2 textures (att45/46/47): 0.0% nonzero. These are written by the
  temporal super-resolution compute kernels ('MainCS' 102x58 = 816/8 x 460/8,
  153x87 etc.).
- Tonemapper (ScreenPassVS/MainPS -> 960x540 back buffer) reads those ->
  black; Slate UI draws on top -> only the HUD is visible (att48 4.1%).
So the break is the TSR compute stage. The five compute pipelines that Metal
shader validation flagged (8132 "invalid device load at offset 24/72 ... out
of user address space" + 6520 "expected Texture2DArray, got Texture2D") are
almost certainly these: TSR keeps its history in Texture2DArrays and reads
several constant buffers. ml913 fixed the 2D/2DArray view type; the invalid
loads are NOT fixed and are the likely cause of zero output.

Staged, not yet run: `<game>\Empire-dx12-notsr.bat` = the normal batch plus
r.AntiAliasingMethod=0, r.TemporalAA.Upsampling=0, r.ScreenPercentage=100.
With TSR bypassed the tonemapper reads scene colour directly; if the world
appears, the remaining bug is isolated to TSR's inputs (then: run one TSR
frame under MTL_SHADER_VALIDATION and match the faulting pipeline UIDs to
kernels by creation order, or dump the descriptor tables of those
dispatches with the ml911 capture extended to compute).

## Addendum 16 — STATE OF PLAY for Astra (2026-09-15, end of session)

### The breakthrough (proven)
The level renders. Host frame dumps taken in-level show scene depth 99.4%
written (terrain, cave mouth), all GBuffers non-uniform (base colour: an ant
on a rock in front of a cave), shadow atlas and 1024^2 shadow map with
content, and SceneColor (736x416 RG11B10F, base-pass MRT0) holding a LIT
cave interior with daylight through the opening. PNGs: scratchpad
`first-render/`, and the artifact "Ant on a Rock". The HUD ("Exit the cave",
objective marker, counters) reached the back buffer once.

Runtime changes that got us here, in order (all in madeira_d3d12.c):
- ml908  exec_add_clear: merge only into a STILL-PENDING clear (stale slot
         bytes made a stencil-only clear also clear depth). vfetch 5c.
- ml909  d3d12_log: 1000-byte buffer + forced newline (512-byte truncation
         fed newline-less fragments to __wine_dbg_output -> ntdll raised
         STATUS_BUFFER_OVERFLOW 0x80000005 on the RHI submission thread at
         the census window; this was the "crash at ~present 1800").
- ml912  bind points 4/5 per draw (IRRuntimeDrawParams + uint16 index type)
         per metal_irconverter_runtime.h; ml914 startIndexLocation = index
         buffer BYTE offset (Apple's helper convention). vfetch 9a1/9a2/9b
         confirm D3D SV_VertexID (raw index), base-vertex stream fetch,
         SV_InstanceID (excludes StartInstanceLocation).
- ml913  dimension-correct MTLTexture views for SRV/UAV (2D/2DArray/Cube/
         CubeArray/3D/1D, mips, slices) instead of "treated as TEXTURE2D".
The first render appeared with ml913+ml914 together; which one unblocked
rasterization is NOT isolated (prepass shaders sample no textures -> ml914
is the likely one). Everything else that was verified along the way:
ml910/ml911 GPU-ordered captures (indirect args, indices, positions, id
stream, View CBV, GPUScene descriptor tables all real), Metal shader
validation (zero vertex/fragment faults). vfetch: 31/31.

### Where it is stuck now: two gates, both outside the renderer's correctness
A. The game's own loading widget (ULoadingScreenWidget, states ... ->
   ImageVisible -> ImageFadeout) stays up while the world is already drawing
   behind it. It left exactly ONCE (run ants61prev): at app-clock t+964 s,
   after a trackpad click on the window followed by Enter; the world had been
   drawing since ~t+250. Since then, three runs with Enter (68 presses),
   clicks, Esc, Space at t+340..t+700 did nothing. Observed during the wait:
   no faults, memory flat (2.35-2.4 GB), zero new textures/heaps, no shader
   conversions pending, no media activity, PSO pool idle, async loader idle,
   only Bink threads parked. Hypothesis: a frame-count-paced engine check
   (texture-streaming "IsStreamingCompleted", referenced by the exe) that
   needs thousands of frames and we run at ~3 fps -> ~10 min after the level
   draws. Untested. Staged: `<game>\Empire-dx12-nostream.bat` = original
   batch + r.TextureStreaming=0 (switch present in the build). Also staged
   but now known NOT to apply the way written: `Empire-dx12-notsr.bat`
   (r.AntiAliasingMethod=0 etc.; the game's GameUserSettings scalability
   overrides ConsoleVariables; GameUserSettings.ini was tried with
   sg.AntiAliasingQuality=0 / sg.ResolutionQuality=100 and has been RESTORED
   to the original values 2 / 87).
   ALTERNATIVE hypotheses not excluded: the widget needs the audio narration
   to end (audio counters do advance), or a Bink loading movie duration
   (Bink threads idle -> unlikely), or a specific input the app does not
   deliver (mouse move? gamepad?).
B. With the widget gone (that one run), the world is BLACK on screen: the
   HUD draws, the 3D view does not. Chain measured by dump: SceneColor has
   the lit scene -> three post-process-material draws ('MainVS'/'MainPS' into
   816x460 RGB10A2, att45/46/47) are 0.0% -> tonemapper reads them -> black.
   816x460 = 85% screen percentage output (sg.ResolutionQuality 87). The
   first PP material reads the temporal upscaler's output; no 816x460
   GPU-written texture appeared in any dump (rmetald walk now includes
   2DArray textures, untested). The TSR compute kernels are the strong
   suspect: under MTL_SHADER_VALIDATION they produced 8132 "Invalid device
   load at offset 24/72 ... Out of bounds of user address space" and 6520
   "expected Texture2DArray, got Texture2D" (the latter fixed by ml913, the
   former NOT fixed, five pipeline UIDs, all 'MainCS'). Also 1 ScatterCopyCS
   store past the end of a 196608-byte buffer. Nothing here is proven to be
   the black-output cause; it is the best-supported lead.

### Open, unproven, recorded
- Case 7d flake: a dispatch that opens a fresh command buffer occasionally
  does not land (2 of 7 test runs). Buffer UAVs get no useResource (only
  textures are added to uav_res), so compute encoders rely on the residency
  set alone for them. Host logs nothing.
- [desc-check] hits: table entries pointing at one 64 KB buffer that is not
  a live resource (0x100484a8000-ish) in prepass/base-pass VS tables — may be
  an unread stale slot or our null buffer (which is not in the live list).
- 10 TIMESTAMP query heaps answer zero.
- Host: rmetald runs with MTL_CAPTURE_ENABLED=1; a full-frame GPU trace
  (/tmp/rmetald-gputrace-now) WEDGES it (3 GB frame, disk filled) — do not
  use without bounding it. Frame dumps: touch /tmp/rmetald-dump-now.
- Mac disk was at 100% during the trace; ~9 GB free after cleanup;
  DerivedData is 14 GB, rmetald log ~125 MB.

### What I would do next (in order)
1. One run with `Empire-dx12-nostream.bat`; Enter at the loading image once
   the census shows base-pass draws (~4 min). If the widget clears quickly,
   gate A is the streaming cycle; if not, instrument the GameThread's
   per-tick guest RIPs (the sampler only catches it in the wineserver wait)
   or log the wait objects it blocks on.
2. With the HUD up: dump again; find the 816x460 producer (now that arrays
   are walked). If it is TSR, run one short window with
   MTL_SHADER_VALIDATION=1 and map the faulting pipeline UIDs to kernels by
   creation order in rmetald (log the UID at newComputePipelineState), then
   capture that dispatch's descriptor tables/CBVs (extend ml911's
   exec_desc_check/capture to compute).
3. A/B ml913 vs ml914 only if it matters for the writeup.

## Addendum 17 — Astra's review of addendum 16; ml918 built

Accepted corrections: gate B (black world after a lit SceneColor) IS a
renderer-correctness problem; TSR is a suspect, not a demonstrated cause; the
dump walk reads slice 0 / mip 0 only, so a black dumped array slice proves
nothing about the slice a pass consumes; do not fabricate timestamps.

ml918 (runtime, in the VM bundle):
- Texture views now honour the SRV Format (typeless->typed, sRGB/linear;
  depth textures keep their own format) and Shader4ComponentMapping (mapped
  to a Metal swizzle; identity 2,3,4,5 is always passed, never zeros -- the
  all-zero swizzle of ml913 meant ZERO channels locally and "no swizzle" on
  the remote). View cache keyed by (type, levels, slices, format, swizzle).
- [desc-check] labels hits that fall inside the runtime's 64 KB null buffer.
- Post-process capture (Astra's plan, runs under the loading overlay too):
  for the first three 816-wide single-target draws (K5..K7) and the first
  ScreenPassVS draw into the 960x540 back buffer (K8) in each census frame:
  every texture descriptor of every table resolved to (resource name, WxH,
  pixel format, texture type, and the exact VIEW: type/format/swizzle/levels/
  slices), a GPU-ordered 4x4 centre block of up to six of those inputs
  BEFORE the draw, the first 64 bytes of each root CBV, and a 4x4 centre
  block of rt[0] AFTER the draw. Texels are decoded per format
  (RG11B10F, RGBA16F, RGB10A2, 8-bit, R16F/RG16F/R32F/RGBA32F) and printed
  as `[cap-data] K5 IN ... (r g b a) x4` / `K5 OUT ...`.
  Decision table: input already black -> follow its producer; input has the
  cave, output black -> that material's bindings/constants/sampling; output
  correct here but black at present -> later clear/reuse/ordering.
Still to run: the no-streaming batch (gate A), with the verification Astra
asked for (does the setting take effect: watch for full-mip texture
creation / higher footprint; record frame count and elapsed time at the
transition).

## Addendum 18 — TSR works; the first post-process MATERIAL draw is the black link (ants68, ml918)

- Gate A this run: original batch, ml918. Enter pressed at t+211 (Story),
  again at ~t+300 twice; the THIRD Enter (line 48157, t+301s) cleared the
  overlay immediately (Niagara SimulateMainCompute compiles 60 lines later).
  So it is not a 16-minute rule; the earlier failures are unexplained
  (possibly the widget only accepts input in a narrow state). The
  no-streaming batch (ants67) took effect and pushed the footprint to
  3807 MB (4096 MB ceiling) -> allocation failures -> AV cascade before the
  level. Not usable; do not retry without a memory plan.
- Host dump in-level with arrays walked: TSR's history textures at 1224x692
  (RG11B10F + R8, 150% of 816x460) hold the fully resolved, upscaled cave
  WITH the ant (scratchpad first-render/tsr-history_1224x692.png). TSR is
  therefore NOT the black link. The 816x460 RGB10A2 targets written by the
  three post-process-material draws ('MainVS'/'MainPS') are 0.0%; the
  tonemapper reads them; back buffer black except UI.
- ml918 PP capture, first run: K5 (first 816-wide draw) OUT = (0 0 0 0) x4
  immediately after the draw; K6, K7, K8 the same. Root CBVs of K5 look
  sane (screen-size params 816/460/408/230, a View row, material scalars).
  Its table p0 has 9 texture descriptors + 2 buffers, but ml918 could not
  resolve texture ids to resources (textures are not in the live list) so
  no IN texels were captured.
- ml919 (in the bundle now): resolves texture descriptors through srv_res /
  uav_res (+ views). Next run's census will print, for K5, each input's
  resource/view and a 4x4 centre block of up to six inputs BEFORE the draw.
  Decision: inputs show the cave -> the material draw itself (bindings,
  samplers, constants, a fade/blend parameter) is the fault; the
  PostProcessInput0 texel is black -> follow which texture TSR's output pass
  writes (likely a resolve from the 1224x692 history) and why it is black.

## Addendum 19 — ml919 answered the K5 question; ml920 fixes a latent dangling-pointer bug

ants69 (ml919): the first post-process material draw's inputs, GPU-ordered,
before the draw: p0[1] "Texture" 816x460 RG11B10F (a UAV-written texture =
the temporal upscaler's OUTPUT) = (0 0 0 1) at the centre; p0[2] 408x230
RG11B10F RT = black; p0[6] 816x460 RGB10A2 RT = black; p0[8] 3840x2160 R8 =
0; p0[9]/p0[10] 1920x1080 R8 = 0.50 (masks). So the material draws are
innocent: their scene input is already black. The TSR history (1224x692) is
correct, so the fault is in the TSR pass that writes the 816x460 output
(a compute kernel; the history->output resolve) or in the UAV it writes
through. Its output texture is created as a plain "Texture" (UAV flag, no
RT flag) in RG11B10F: check that Metal on the host allows shader WRITE to
RG11B10Float (MTLTextureUsageShaderWrite on that format) -- if a compute
kernel writes an unwritable format, Metal silently drops the writes.

The run then died at the next census: BUS->AV in madeira_d3d12.dll
(exec_draw+0x31c = the inlined mad_texture_of_view walking dev->srv_res):
srv_res/uav_res never dropped released resources (mad_untrack only cleans
the GPU-address 'live' list), so they hold dangling pointers; the residency
loops read r->texture from them for hours (still mapped), my walk read
+0x400 (xview ids) and hit an unmapped page. ml920: res_Release now removes
the resource from both registries. This also stops feeding freed texture
handles to useResource ("compute skip: resource ... unresolved" seen on the
host earlier).

ml920 also adds K9: in census frames, the first dispatch whose tables name
a 816-wide RG11B10F texture -> all table entries resolved (textures to
resource/view, buffers to live/NOT LIVE), root CBVs, a 4x4 block of that
texture BEFORE and AFTER the dispatch. That names the kernel and shows
whether it writes.

## Addendum 20 — K9 result: the upscaler output is DARK, not black; exposure is the suspect (ants70, ml920)

- K9 picked the history-update kernel ('MainCS' 153x87 = 1224x692/8); it
  lists the 816x460 output but does not write its centre (before == after).
  In-level the 816x460 RG11B10F output holds ~(0.0004, 0.0004, 0.0003) at
  the centre -- a very dark cave floor, not zero (during the loading phase
  it was exactly 0). The tonemapper's output is exactly 0 in RGB10A2.
- The first "post-process material" draw K5 is in fact the TONEMAPPER
  (PostProcessTonemap.usf has MainVS/MainPS). Its resolved inputs:
  [1] scene colour 816x460 RG11B10F, [2] bloom 408x230, [9] 32^3 colour LUT
  (3D, RGB10A2), [8] 1x1 RGBA8, [10] scene depth, [11] an 816x460 RGB10A2 RT,
  material textures, and TWO BUFFER descriptors: [0] buf+65536 and
  [3] buf+16. UE 5.4 keeps the auto-exposure result in EyeAdaptationBuffer
  (a structured buffer written by EyeAdaptationCS 1x1x1, fed by
  SetupLogLuminanceCS/HistogramConvertCS). Scene colour 0.0004 x exposure:
  exposure ~1 or 0 -> black; a correct auto-exposure would lift it ~100x.
  Metal compute writes to RG11B10Float verified OK on the host (native
  test), so the dark value is a real render value, not dropped writes.
- ml921/ml922 (in the bundle): the PP capture now also dumps buffer inputs
  (64 bytes) and 1x1 textures; K10 captures the EyeAdaptationCS dispatch:
  tables resolved, buffer inputs BEFORE, buffer outputs AFTER, CBVs.
- The ml920 dangling-pointer fix held (no faults in a 12-minute run).

## Addendum 21 — Tonemapper zero-output root cause: static samplers were never bound (ants71, ml923 built, NOT yet run)

K10/K5 capture data (ants71, ml922), all read back on the host in GPU order:

- EyeAdaptationCS: exposure buffer BEFORE = AFTER =
  `3.43426 3.43426 1 4 1 0 ...` (hex 405bcaf3 405bcaf3 3f800000 40800000).
  The 1x1 exposure texture matches (3.434 3.434 1 4). So exposure is valid
  and the kernel is not the fault (it simply converged).
- Tonemapper (PostProcessTonemap MainVS/MainPS, K5) inputs, all NON-zero:
  scene colour 816x460 RG11B10F = 0.00036, exposure buffer = 3.43426,
  bloom 408x230 = 0.0005, colour LUT 32x32x32 RGB10A2 = (0.479 0.477 0 0)
  non-uniform, colour-grading matrix CBV = (1.976 1.986 1.968 1 ...).
  Tonemapper OUTPUT rt0 = exactly (0 0 0 0).

Non-zero inputs and an exactly-zero output means the pixel shader's texture
reads return zero, i.e. sampling itself fails. The tonemapper samples every
input through UE's six GLOBAL STATIC samplers (root-signature static
samplers), and so does every other post-process pass.

What the converter actually does with static samplers (Metal Shader
Converter UserManual.md, "Define a Global Root Signature" example): a root
signature with one SRV table and one static sampler produces TWO top-level
entries, "offset 8: a uint64_t referencing a table with one sampler. The
sampler table should be encoded like so: for each sampler: 64-bit GPU VA of
the sampler, 64-bit 0, 64-bit LOD bias." The static samplers are NOT baked
into the shader as constexpr samplers (the ml861 comment in madeira_ir_abi.h
saying so is wrong). The runtime has to build that table and write its
address into the implicit trailing slot -- the "extra" reflection entry that
mad_root_layout already accounts for (addendum: "static samplers add one
implicit trailing table entry"). Nothing ever wrote that slot, so every
static-sampler texture read in every UE shader sampled through a null
sampler. That matches the symptom pattern exactly: passes that use dynamic
samplers or plain Load() (base pass, depth, GBuffers, shadows, TSR compute
kernels, the LUT-generation pass) render correctly; the tonemapper, which
samples everything through static samplers, outputs zero.

ml923 (built, in build/madeira-d3d12/out-pe/, md5 b783e6e3..., NOT yet
deployed -- the VM was unreachable, 0/2 ping, ARP incomplete):

1. At root-signature creation: one Shared MTLBuffer of nsamplers x 24-byte
   descriptors; each = {sampler gpu_resource_id, 0, MipLODBias float bits},
   made resident, address kept in rs->stab_gpu. Log line:
   `[madeira-d3d12] static sampler table: N/N samplers at <addr>` (4x).
2. Reflection: if the converter lists one more entry than the signature has
   params and it is a table, its offset is recorded as pso->static_off. Log:
   `[root-layout] ... static-sampler table slot at <off>` (6x).
3. exec_arg_slot_for writes rs->stab_gpu at pso->static_off for graphics
   AND compute (compute passes e->cpso now).
4. mad_sampler_info(): a full D3D12 sampler-desc -> Metal mapping used by
   both static samplers and CreateSampler: min/mag/mip filter bits
   (0x10/0x04/0x01), anisotropic 0x40, comparison 0x80 -> compare function,
   all five address modes, border colour, min/max LOD. Before ml923
   CreateSampler distinguished only point vs linear, forced ClampToEdge and
   NotMipmapped, never a comparison function -- that is a second, separate
   defect (tiling textures, shadow PCF and mip selection were all wrong for
   dynamic samplers too). CreateSampler is the only other caller.

Confidence: high on the mechanism (manual text + measured data agree), but
unverified on hardware. What the next run must show:
- the two new log lines above (table built, slot found for the tonemapper's
  'MainVS' PS layout and for compute kernels);
- K5 OUT rt0 non-zero; the RGB10A2 backbuffer non-black; the cave visible
  behind the HUD.
If the slot is NOT found for a layout that has static samplers, the
reflection count/type check in mad_apply_reflected_layout is what to look at.
Backup of the previous DLL on the VM: /var/root/madeira_d3d12.dll.ml905
(older); take a fresh backup of the installed one before overwriting.

## Addendum 22 — ml923 ran (ants72): samplers bound, still black; the exposure chain is dead and the tonemapper zero is not explained by dark input

ml923 result: both new log lines appear (`static sampler table: 6/6 samplers`
x4, `static-sampler table slot at 40/72/88` for the VS/PS layouts). So the
static samplers ARE bound now, and the screen is still black behind the HUD.
The sampler fix was real (the manual is unambiguous) but it was not the
cause of the zero tonemapper output. Note: the VM came back on 192.168.64.6.

Facts from ants72 (three in-level census frames, lists 58642/73511/88180,
plus host dump f21 of the same run):

1. Exposure is frozen. EyeAdaptationCS output == input on every frame:
   `3.43426 3.43426 1 4` (hex 405bcaf3 405bcaf3 3f800000 40800000), the 1x1
   exposure texture the same. The 16x2 RGBA32F histogram it reads is 0 at
   the sampled texel in every frame. The dispatch chain per frame is
   SetupLogLuminanceCS 4x2, MainAtomicCS 230x1, HistogramConvertCS 1x1,
   EyeAdaptationCS 1x1, CopyEyeAdaptationToTextureCS 1x1 (all converted,
   none skipped). Its CBV `0.1 0.9 0.000175781 188744 2 1 0.1 3 1 0.0333
   0.333 0.000976562 0 0.18 0 0.1` = low/high percentiles, min/max average
   luminance (a huge range, so this is AUTO exposure, not manual), comp 2,
   speeds 3/1, histogram scale/bias, grey 0.18. With a working histogram
   the exposure for this scene would be ~1000, not 3.43.
2. The scene is bright where it should be. The host dump maps floats
   through v/(1+v) then gamma 2.2 (rm_tone in rmetald.m), so a PNG value of
   ~220/255 means a raw value ~2.5. tex061 (TSR history 1224x692) shows the
   cave with a sunlit opening at that level; the floor is ~0.009, the centre
   texel 0.0004. Yet att45/46/47 (the three 816x460 RGB10A2 post-process
   targets) are 0/375360 non-zero -- the tonemapper writes exact zero even
   for pixels whose input is 2.5. A dark scene cannot explain that.
3. The tonemapper's 12 resolved inputs (in-level): [0] exposure buffer
   (3.43426), [1] scene colour 816x460 RG11B10F, [2] bloom 408x230, [3] a
   16-byte buffer (1 1 1 1), [4] Texture 7x4 pf105 t7 = a 3D RG32Float =
   UE's LOCAL EXPOSURE bilateral grid, [5] 26x15 R16F blurred log-luminance
   (-9.98 at centre = 2^-10, consistent with the scene), [6] 128x128 BC7
   (bloom dirt / grain), [7] colour-grading CBV (1.976 1.986 1.968 1 ...),
   [8] 1x1 RGBA8 black, [9] LUT 32^3 RGB10A2 (centre 0.479 = neutral),
   [10] scene depth, [11] an 816x460 RGB10A2 RT. [4] and [6] were not
   decoded by the capture (formats unsupported until ml924).
4. Buffer descriptors already carry the converter's size metadata (checked;
   `buf+off/size` in the census). Not the cause.
5. Texture atomics without MTLTextureUsageShaderAtomic: TESTED NATIVELY on
   the M4 Max (scratchpad/texatomic/t.m): atomic_fetch_add on an R32Uint
   texture gives the right count with or without the usage flag. So the
   missing flag does NOT explain a dead histogram. No usage change made.

Working hypothesis (unproven): two dead things share one cause -- the
histogram (built by MainAtomicCS with atomics) and, probably, the local
exposure grid (a 7x4xN RG32F 3D texture written by a compute kernel). The
tonemapper divides by the grid's weight channel; a zero grid gives 0/0 =
NaN, exp2(NaN) = NaN, and RGB10A2 stores NaN as 0 -- exact zero output
regardless of scene colour, which is what we see in BOTH the loading phase
and in-level. Zero-output-with-bright-input is the signature of NaN, not of
darkness.

ml924 (deployed to both bundle DLLs, cksum 290744862; capture-only, no
runtime behaviour change):
- exec_capture_region(): any rectangle of any subresource incl. z-slices
  of 3D textures (new tex_depth field); textures with <=256 texels are
  dumped IN FULL, sequentially (histograms, grids), integers for
  R32Uint/RG32Uint, RG32Float decoded.
- Named-kernel capture for K10 EyeAdaptationCS, K11 HistogramConvertCS,
  K12 MainAtomicCS, and K13 = the first dispatch naming a 3D RG32Float
  texture (the grid producer): every table entry resolved, every small
  texture and buffer BEFORE and AFTER the dispatch, root CBVs.
- K5 (tonemapper) now dumps 12 inputs, the grid's middle z-slice and its
  (0,0,0) texel, and the LUT's (0,0,0) texel.

What ants73 must answer (all in `[cap-data]` lines):
- K12 AFTER: is the atomic scatter texture (expect 128x1 or 64x2 R32Uint)
  non-zero after MainAtomicCS? If zero -> atomics from a converted kernel
  are lost (then test MSC-converted atomics natively, not raw Metal).
- K11: does HistogramConvertCS turn it into a non-zero 16x2 float texture?
- K13/K5: is the local-exposure grid non-zero (x = weighted log-lum sum,
  y = weight)? If y == 0 everywhere, the NaN path above is confirmed and
  the producer kernel is the next target.
- K5 LUT (0,0,0): the black end of the LUT (expect ~0).

## Addendum 23 — FOUND: the colour-grading LUT has only slice 0; RTV/DSV sub-views were ignored (ants73 -> ml925)

ants73 (ml924 capture, in-level, list#73684-73687):

- Colour LUT 32x32x32 RGB10A2 [K5 p0[9]]: texel (0,0,0) = (0 0 0 0) and
  the CENTRE OF SLICE 16 = (0 0 0 0) x4. Slice 0's centre read 0.479 in
  every earlier run. The LUT is written on slice 0 only. The tonemapper
  looks colour up in that LUT (log-encoded rgb -> xyz); any pixel whose
  blue axis lands past slice ~1 reads an unwritten slice and comes out as
  exact zero -- the bright cave opening (2.5) included. Near-black input
  reads texel (0,0,0), also zero. That is the whole "exact zero output
  from non-zero input" symptom, in both loading and level phases.
- Local-exposure grid 7x4x32 RG32F [K13/K5 p0[4]]: texel (0,0,0) =
  (-40960, 4096) = weighted log-lum -10 x weight 4096, i.e. valid; slice
  16 empty because the scene sits at -10 EV. The NaN theory is dead.
- Histogram: the 128x1 R32Uint scatter texture is all zero after
  MainAtomicCS in-level (every bin, three distinct 128x1 textures). Input
  scene colour there is ~1.3e-4, below the kernel's LuminanceMin (1/1024)
  with BlackHistogramBucketInfluence 0, so most pixels legitimately carry
  no weight; but the sunlit opening should register and does not. Still
  suspect: atomics from a CONVERTED kernel (raw Metal texture atomics work
  on the host with and without ShaderAtomic usage -- tested). Not fixed
  in ml925; needs an MSC-converted atomics test natively.

Root cause of the LUT (code): device_CreateRenderTargetView and
CreateDepthStencilView stored ONLY the resource pointer. Mip level, first
array slice, 3D first W slice and the slice COUNT of the view were all
dropped; every pass rendered level 0 / slice 0 / plane 0 with
renderTargetArrayLength 0. UE draws the LUT as 32 instances whose vertex
shader writes SV_RenderTargetArrayIndex (we report
VPAndRTArrayIndexFromAnyShader... = TRUE, so no GS), and Metal needs the
pass's renderTargetArrayLength = 32 with the 3D attachment's depthPlane at
the view's first slice; otherwise every layer > 0 is clipped. The same
defect hit cube/array shadow depth views (every face -> slice 0) and any
mip-chain RTV.

ml925 (deployed to both bundle DLLs, cksum 3361977719):
- RTV/DSV heap entries are now `struct mad_rtv { res; level, slice,
  layers, plane }` (stride 32; the resource pointer stays first so every
  old `*(struct mad_resource **)` reader still works).
- Create*View fill them from the D3D12 view desc (2D/2DArray/2DMS(Array)/
  1D(Array)/3D; NULL desc = whole resource); logs `layered RTV/DSV: ...`
  the first 6 times.
- OMSetRenderTargets / Clear*View carry the view; pending clears are keyed
  by (resource, view); exec_same_targets compares views; the render pass
  sets level / slice / depthPlane per attachment, renderTargetArrayLength
  = max layers when > 1, and the pass size from the mip level.
Host side already forwards all of these (rmetald.m 1620-1680).

Expect on the next run: `layered RTV: ... t7 level 0 first 0 layers 32`
for the LUT, K5 LUT slice-16 centre ~ (0.48 0.48 0.5), K5 OUT non-zero,
and the sunlit part of the cave visible behind the HUD. The cave interior
will still be nearly black: scene radiance ~3e-5 with GI/VSM/volumetrics
off in the batch and exposure frozen at 3.43 while the histogram stays
empty. Those are the next two items, in that order: histogram atomics,
then re-enable GI in the batch.

## Addendum 24 — ml925 first run (ants74) was invalidated by my own heap bug; ml925b deployed

ants74 result: still black. The `layered RTV: RenderTarget 32x32 t7 level 0
first 0 layers 32` line fired (so the view path works), but the LUT read
zero on BOTH texel (0,0,0) and slice 16, and the host log shows what
happened: from this run on, `render pass: DEPTH handle 0xf0f0f0f0f000701
did not resolve` x14189 and `GPU ERROR #1..#1793: MTLCommandBufferError
Internal Error` -- every command buffer failed. Cause: ml925 changed the
RTV/DSV descriptor stride from 8 to 32 bytes but device_CreateDescriptorHeap
still allocated `struct mad_resource *` (8 bytes) per descriptor, so the
game's view writes ran off the end of every RTV/DSV heap and corrupted the
heap allocator; a garbage depth handle reached the wire. Confidence: high,
this is exactly the failure shape and the fix is mechanical.

ml925b (cksum 2879399381, both bundle DLLs): heap slots are `struct mad_rtv`
(32 bytes), matching mad_descriptor_stride. Nothing else changed.

One thing I am NOT sure about: the very first "did not resolve" in the host
log (line 1908284) names a REAL remote handle 0x800000180001a6ff, before the
garbage ones start. It may be a depth texture released while a pass still
named it, or a first symptom of the same corruption. If ants75 (ml925b)
still shows `did not resolve` lines on the host, that is the next thing to
read, not the LUT.

Check on ants75: no `did not resolve` / `GPU ERROR` on the host; K5 LUT
slice-16 centre ~(0.48 0.48 0.5); K5 OUT non-zero; sunlit cave visible.

## Addendum 25 — ml925b clean on the host, LUT still slice-0 only; layered rendering PROVEN offline; suspect: a dropped geometry shader (ml926 probes deployed)

ants75 (ml925b): host log clean for this session (0 unresolved handles, 0 GPU
errors after the reconnect at host log line 1954466 -- the earlier
garbage-handle flood was entirely the ants74 session). `layered RTV: 32x32 t7
layers 32` fired. LUT texel (0,0,0) = 0 (legit black) and slice 16 = 0 x4:
STILL only slice 0 written. Screen black.

Offline proof (scratchpad/layered/: layered.hlsl via wine dxc, layered.mm
native, converter 4.0b2 on the M4 Max): a VS writing
SV_RenderTargetArrayIndex = SV_InstanceID, converted with an empty root
signature, drawn 3 verts x 4 instances with the draw-params block at bind
point 4, inputPrimitiveTopology = triangle:
  2D array + renderTargetArrayLength 4 -> slices 0..3 = (32/64/96/128, G=255 on 2)  OK
  3D texture + renderTargetArrayLength 4 -> same, all four depth planes    OK
  3D texture, array length NOT set        -> slice 0 = LAST instance (R=128), rest 0
So converter + Metal + our pass shape do layered 3D rendering correctly, and
the old no-array-length behaviour is "last instance wins on slice 0".

The game's slice 0 held the FIRST instance's colour (blue = 0 in ants72),
not the last. That does not match "layers clipped"; it matches "the pixel
shader saw LayerIndex = 0 for every instance". UE's volume rasterizer
(RasterizeToVolumeTexture: 4-vertex strip x N instances, WriteToSliceMainVS)
has two permutations: VS writes the layer (USING_VERTEX_SHADER_LAYER), or a
GEOMETRY shader WriteToSliceMainGS writes it and passes LayerIndex to the PS.
FWriteToSliceGS compiles only when the cooked shader platform lacks
vertex-shader-layer. device_CreateGraphicsPipelineState never looks at
desc->GS (the stream path parses it into g.GS and nothing reads it), so a GS
would be dropped silently: VS+PS run, no layer output, PS input layout no
longer matches the VS output -> LayerIndex reads 0, every instance paints
slice 0 with layer-0 colours. Exactly the observation. Unverified until the
next run shows it.

ml926 (cksum 1801578507, both bundle DLLs; probes only):
- `pipeline carries GS/HS/DS ... which this runtime DROPS` (first 12).
- `[dxil-hex]` full DXIL of WriteToSliceMainVS / WriteToSliceMainGS (once
  each) -> reassemble and `wine toolchains/dxc-win/bin/x64/dxc.exe -dumpbin`
  shows the output signature (SV_RenderTargetArrayIndex present or not).
- `[layered-draw]` census for every draw into a view with layers > 1 (first
  24) and K14: slice 0 and mid-slice centre of the target right after the
  first such draw.

If the GS is confirmed: options are (a) geometry-shader support through the
converter's mesh-shader path (IRCompiler links VS+GS into an object/mesh
pipeline; draw-params buffers required -- manual 1142), which is real work,
or (b) find why UE cooked the GS permutation: the DDSPI
bSupportsVertexShaderLayer flag for PCD3D_SM6 is baked at cook time and
cannot be changed by us. The 48^3 R8 pairs drawn every frame with the same
VS are the same pass shape and will show the same result.

## Addendum 26 — CONFIRMED: six pipelines carry a geometry shader we dropped; ml927 implements geometry shaders through the converter's mesh emulation (new IPA)

ants76 (ml926 probes): `pipeline carries GS (4124/0/0 bytes) which this
runtime DROPS; VS 4612 B, PS ...` for SIX pipelines -- every pipeline with
WriteToSliceMainVS (the colour LUT 32^3 and the 48^3 translucency volumes).
`[layered-draw]` shows them as 4-vertex strips x 32/48 instances into 3D
targets, and K14 confirms the target stays empty on slice 0 AND the middle
slice after the draw. The cook chose UE's geometry-shader permutation
(FWriteToSliceGS), so no vertex shader writes the layer; the runtime silently
ran VS+PS without the GS. That is the black screen: the tonemapper's LUT has
only whatever the last instance left on slice 0.

Offline proof that the fix path works (scratchpad/gsemu/): a VS + passthrough
GS (SV_RenderTargetArrayIndex from an interpolant) + PS converted with
IRCompilerEnableGeometryAndTessellationEmulation, a stage-in function
synthesized from the input layout, IRRuntimeNewGeometryEmulationPipeline
(object = vertex fn ".dxil_irconverter_object_shader" with constant
tessellationEnabled=false + linked stage-in; mesh = geometry fn with constant
vertex_shader_output_size_fc; fragment as normal), drawn with
IRRuntimeDraw[Indexed]PrimitivesGeometryEmulation: all four depth planes of
a 3D target correct, both non-indexed and indexed (start index is an
ELEMENT index; the index-buffer byte offset folds into the address).

ml927 (Madeira-ml927.ipa, 63.4 MB; rmetald rebuilt and restarted):
- winemetal: new API slot 133 MTLDevice_newGeometryEmulationPipelineState
  (WMTMeshRenderPipelineInfo for attachments + WMTGeometryEmulationInfo:
  four library handles, three function names, vertex size, max prims).
  Remote path = RM_OP_NEW_GEOM_PSO_INFO; rmetald builds the functions with
  their named function constants, links the stage-in function into the
  object stage and creates the mesh pipeline (exactly the runtime header's
  IRRuntimeNewGeometryEmulationPipeline, without needing the header).
  WMT_API_COUNT 134; remote guard regenerated; wow64 stub.
- converter ABI/shim: gs_emulation, input_topology, layout (semantic names
  + DXGI formats; IRFormat numbering == DXGI), out_buf2 for the stage-in
  metallib, ret_vs_output_size / ret_gs_max_prims / payload / passthrough.
- runtime: a PSO with desc->GS converts VS (emulation + stage-in from the
  input layout), GS (emulation) and PS (normal), then calls the new API;
  p->gs_emu. exec_draw on such a pipeline binds the top-level argument
  buffer, both heaps, draw params (4) and the full IRRuntimeDrawInfo (5)
  to the OBJECT and MESH stages, writes an IRRuntimeVertexBuffers table
  ({addr,length,stride} x 31) into the per-draw arg slot and binds it at
  object index 6, makes vertex/index buffers resident for object+mesh, and
  issues drawMeshThreadgroups with the runtime header's math ported
  (mad_gs_draw). Arg slot grew 576 -> 1088 bytes. Indirect draws on GS
  pipelines are skipped with a log (none seen in this game).
- The IRRuntimeDrawInfo (24 bytes) is now written for EVERY draw; its
  first uint16 is the index-type flag ml912 wrote, so vertex pipelines see
  exactly what they saw before.

Unverified until ants77: the game's real shaders through this path
(WriteToSliceMainVS reads ATTRIBUTE0 from a stream; the GS is 4124 bytes),
the host-side function names, and whether Metal accepts the pipeline with
UE's blend/attachment setup. Log lines to look for:
`VS(gs) converted with geometry emulation: vertex output N B ... stage-in M B`,
`GS converted with geometry emulation: ... gs max prims P`,
`geometry pipeline created: vs 'WriteToSliceMainVS' gs '...'`, and on the
host `[rmetald] geometry pipeline OK`. Then K5's LUT slice 16 non-zero and
K14's mid-slice non-zero, and the cave behind the HUD.

## Addendum 27 — ml927 runs: geometry pipelines build and draw; two crashes, one fixed, one attributed to the build configuration (ml927b IPA)

ants77 (ml927 IPA, Release build): crashed before the splash, twice at the
same game address (exe+0xf46d55, an interlocked op on address 0 = a null
pipeline object). Cause found: the engine creates pipelines from six
PSOPrecompilePool threads at once and the runtime took the converter's
entry names from a shared global (g_last_entry); the second geometry
pipeline asked the host for "MainPS.dxil_irconverter_object_shader" and was
refused, the PSO came back null. The FIRST geometry pipeline (the LUT) was
created fine on both sides: `geometry pipeline created: vs
'WriteToSliceMainVS' gs 'WriteToSliceMainGS' ps 'MainPS'` and on the host
`[rmetald] geometry pipeline OK`.

ml927b (d3d12.dll swap): per-call entry names; a geometry pipeline that
still cannot be built falls back to the plain VS+PS pipeline with a log
instead of failing the PSO. ants78: all three geometry pipelines created,
the 24 layered draws went through the mesh path, host 0 GPU errors -- and
the game still faulted at the same address about 200 s in, with NO
pipeline failure logged. What else changed: the ml927 IPA was built with
the RELEASE configuration (18 MB main binary), while every IPA that ever
ran this game (ml908 and earlier) was a DEBUG build (91 KB stub +
Madeira.debug.dylib). That is a second variable in the same run, so
ml927b.ipa is the same code built as DEBUG (Madeira-ml927b.ipa, 62.4 MB,
debug dylib carries the new unix code, metal-bridge export present).

If ants79 (Debug) still faults at exe+0xf46d55: the null object the game
dereferences is something the runtime hands it, and the next probe is to
log every ID3D12 object creation that returns S_OK with a null out-pointer
and every E_* return, on all threads, with the tid.

## Addendum 28 — FIRST IMAGE ON SCREEN (grey with stripes); the remaining blocker is the TSR resolve kernel writing a uniform output (ants79/80, ml928/ml929)

ants79 (ml927b Debug IPA): no crash. The screen is a dark blue-grey with
faint vertical stripes and a vignette (the user's "brushed metal"), HUD on
top, occasional flashes to black. ants80 (ml928) same. Host: 0 GPU errors.

What the captures establish, in pipeline order:
- Colour LUT 32^3: CORRECT on every slice now (K14: slice 0 blue 0, slice
  16 blue 0.65-0.68; K5 in-level the same). Geometry emulation works.
- Local-exposure grid 7x4x32 RG32F: texel (0,0,0) = (-40960, 4096), slice 16
  zero. No `UAV reinterprets` line fired, so the grid is not written through
  a reinterpreting view; ml928 (honour UAV formats) is correct D3D12
  semantics but was not the cause here.
- Converter-compiled `InterlockedAdd` on RWTexture2D<uint> through a
  descriptor table, natively (scratchpad/texatomic/msc_atomic.mm): 1024 x4,
  with AND without MTLTextureUsageShaderAtomic. Lost atomics are ruled out.
- Tonemapper (K5): input scene colour 0.00044 UNIFORM (all four centre
  texels equal; host dump: the 816x460 RG11B10F is 99.8% uniform), exposure
  3.43426, LUT correct -> OUT (0.0029 0.0039 0.0029): near-black with 1-LSB
  dither. K8 copies that to the backbuffer unchanged. The final display
  gamma lifts 0.003 to the dark grey on screen, and the dither becomes the
  stripes. The tonemapper is CORRECT for its input.
- The 1224x692 TSR history (t3, a 2D array) is detailed and "bright" in
  the dump ONLY because TSR stores history in a perceptual encoding
  (K13 reads its slice 0 at the centre as 0.00089). The 816x460 output the
  resolve kernel derives from it is uniform 0.00044. THAT kernel is the
  blocker: everything downstream (tonemapper, histogram -> exposure frozen
  at 3.43 because a uniform 1e-4 input is below LuminanceMin 1/1024) is a
  faithful consequence of a uniform scene-colour input.

ml929 (d3d12.dll, cksum 369940510): K9 now captures EVERY dispatch in a
census frame whose tables name the 816-wide RG11B10F texture (up to 10),
with the centre 4x4 before/after and a second 4x4 at (100,100) after. The
dispatch whose OUT differs from its IN is the resolve/writer; its tables
(2D-array views of the history: slices, mips, formats) and root CBVs are
in the same capture. Earlier, host shader validation flagged the TSR
kernels for invalid device loads and texture-type mismatches (ml915 note);
that is the class of defect to expect in that kernel's bindings.

Fallback if the kernel is not quickly fixable: the game's user settings
(GameUserSettings.ini, sg.AntiAliasingQuality) do NOT turn TSR off (level 0
keeps TSR at lower quality); r.AntiAliasingMethod=0 in the batch's
UserEngine.ini was tried in ml915 and appeared overridden. Verify what the
notsr batch actually produces before relying on it.

## Addendum 29 — the 816x460 scene colour is UNIFORM before any dispatch that names it; writer unidentified; next probe = host shader validation (ants81, ml929)

ants81 (ml929 K9-all): in the two in-level census frames only ONE dispatch
names the 816x460 RG11B10F texture -- TSR 'MainCS' 153x87 (= 1224x692 / 8,
the history-update kernel). The texture reads (0.1006 0.125 0.1367) at the
centre AND at (100,100), BEFORE and AFTER that dispatch, in both frames
(second frame 0.1084 0.1328 0.1367). No draw targets an 816x460 R11G11B10
resource (draw census: only the RGB10A2 post-process passes at that size),
and copies are not logged. So either that kernel rewrites the same uniform
colour every frame (its output is a function of collapsed inputs), or a
copy/clear does. The uniform colour is NOT a corner texel of the history
(history PNG corners ~ (27,30,33)) nor of the 736x416 base-pass colour
(att07 corner (64,70,73), centre (19,20,20)); it is brighter than both.

Correction to addendum 28: the TSR history PNG reads ~(27,30,33) per PIL at
every sampled point; the "bright cave" impression came from the image
viewer's contrast, not from the data. The history is dark and low
contrast; the K13 centre read of 0.00089 stands.

Compute pipelines DO get the static-sampler slot (mad_apply_reflected_layout
runs for CS; the log limit hid the CS lines). Root CBV buffers are in the
residency set from creation.

The best-supported lead remains addendum 11/16's validation result on the
TSR kernels: 8132x "Invalid device load at offset 24/72 ... Out of bounds
of user address space" in five 'MainCS' pipelines, never explained. A load
from a null/invalid buffer base returning zeros for constants at offsets 24
and 72 (extents / inverse sizes) is exactly what collapses every UV to one
texel and makes the whole output uniform. Next probe: rmetald restarted
with MTL_SHADER_VALIDATION=1 on the current build, one in-level window,
then match the reported pipeline/offsets against the K9 tables and root
CBVs of the 153x87 kernel (and any CBV inside its descriptor tables).

Not attempted, deliberately: forcing TSR off through config (uncertain
whether it takes effect; see addendum 28) and any runtime change without
the validation evidence.

## Addendum 30 — Astra's review (2026-09-16): the compute captures suppressed the dispatch they measured; retractions; ml930

Confirmed in code: exec_dispatch created the compute encoder, THEN ran the
census capture, whose blit (exec_begin_blit -> exec_end) ended that encoder
and zeroed e->cenc; the dispatch was then encoded into handle 0, which the
host accepted as nil and counted as replayed. So in every census frame the
captured compute dispatches (TSR update, histogram, exposure, the grid
producer) simply did not run. RETRACTED accordingly, as measurement
artefacts: "EyeAdaptationCS output == input", "histogram scatter all zero
after MainAtomicCS", "K9 OUT == IN so the kernel is not the writer", and
every "the exposure is frozen" conclusion built on them. The occasional
"flash to black" the user saw was the census frames themselves.

Also per Astra: the 102x58 'MainCS' (history at input-table entry 0, the
816x460 output in another table, a 408x230 beside it) is the likelier
history->scene-colour resolve and my K13 label hid it from the K9 scan;
table scans ignored root-signature range lengths and could attribute a
neighbouring dispatch's descriptors; the 64-byte CBV dumps cannot cover the
suspected load at offset 72; and ants81's own numbers are exposure 4.578,
scene colour ~(0.108,0.133,0.137), tonemapper out ~(0.31,0.38,0.39) -- do
not mix them with older runs.

ml930 (guest d3d12.dll + rmetald): pre-dispatch captures run BEFORE the
compute encoder is created (so the encoder the dispatch uses is fresh);
host refuses encodeCommands on a NULL/nil encoder with a log line; table
scans are bounded by the parameter's range lengths (mad_table_count) and
print the count; the named-kernel capture no longer returns before the K9
scan; K9 also dumps the big inputs (history array views with their
slice/mip/format) at the centre and at (100,100), before, and the output
at both points before/after; root CBVs are dumped as 96 bytes (kind 16).
Host validation (MTL_SHADER_VALIDATION=1) stays on for this run.

Other gaps Astra listed for SEPARATE tests: ResourceBarrier is ignored;
useResource lists cap at 256 and fill SRVs before UAVs; a uint UAV clear of
1 is byte-filled as 0x01010101.

## Addendum 31 — first captures with real dispatch execution (ants82, ml930): the TSR resolve writes ZEROS from a non-zero history; host validation is silent; ml931 dumps kernel bytecode

ants82 (ml930, host with MTL_SHADER_VALIDATION=1): screen BLACK behind the
HUD (not grey). Host: 0 GPU errors, 0 null-encoder refusals, and NOT ONE
shader-validation message in 1800+ frames (the env var is in the process;
the old run logged 28489 of them). So the old "invalid device load at
24/72" and type mismatches are gone with the current view code.

In-level census (list 57965), dispatches now executing:
- TSR update 'MainCS' 153x87: inputs 736x416 scene colour ~0.0008 at
  (100,100); history array view slices 0..2 (s0+3) slice 0 ~0.001;
  R8 array 0.81; history view slice 3 (s3+1) ~0.001; output UAVs = history
  slice 3 (p1[0] VIEW s3+1), R8 slice 3, a 1x1 (2DArray view of a 2D).
  CBVs (96 B): p2 = InputInfo 736 416 1/736 1/416 ... 0.998641 0.995192
  1.00136 1.00483; p3 = a view matrix block.
- TSR resolve 'MainCS' 102x58 (Astra's candidate, confirmed): p0[0] =
  history VIEW slice 3 (non-zero, ~0.001), [1] grid, [2] 408x230, ...;
  p1 (2 entries) = [0] the 816x460 scene colour UAV, [1] 408x230 UAV.
  Output at the centre and (100,100): 0 before, 0 AFTER. In non-census
  frames the host dump shows the same: 816x460 targets and every 408x230
  all zero, tonemapper output zero -> black.
So a kernel with valid (validation-clean) loads and non-zero history
input writes zeros. Its arithmetic zeroes it: a multiplier read as 0
(pre-exposure ratio, a weight, a resolution factor from a constant we
bind wrongly) is the shape to look for. The 96-byte root CBVs above are
the only root CBVs it has; anything else it reads is inside its tables
or the View uniform buffer.

Retraction of the ml929 read: the earlier "uniform 0.1 before and after"
was captured while the dispatch did not run; with the dispatch running,
the output is zero.

ml931 (d3d12.dll, deployed): every compute shader's DXIL is written to
C:\\madeira-cs\\cs_<pso pointer>_<bytes>.dxil (prefix drive_c) at pipeline
creation, and the K9 line prints the dispatch's pso pointer. Next: pull
the resolve kernel's file, `wine dxc.exe -dumpbin`, read what it
multiplies the history by, and match that operand to a binding. That is
the bounded reproduction Astra asked for, without a GPU trace.

## Addendum 32 — ROOT CAUSE OF THE ZERO SCENE COLOUR, read from the kernel bytecode: Texture2D vs Texture2DArray on the same resource; fix = converter ForceTextureArray + arrays everywhere (ml932 IPA)

ants83 (ml931): crashed at the loading->level transition, thread
"Background Worker #3", exe+0x5f61e4f reading 0x840159xxx (a page in the
TEB/stack band, not Wine-owned) -- a different site from the ants77/78
null-pipeline crash and not obviously ours; intermittent at that
transition, recorded, not chased. But the 67 compute kernels were dumped
to C:\\madeira-cs before it, and `dxc -dumpbin` on them identifies the TSR
resolve without needing the pso pointer:

  cs_0000000319EE6000_12664: MainCS, D3DStaticBilinearClampedSampler
  (s3, space1000), UpdateHistoryOutputTexture  Texture2D  t0,
  SceneColorOutputMip0 RWTexture2D u0, SceneColorOutputMip1 RWTexture2D u1
  (= the 816x460 and 408x230 outputs, exactly the K9 p1 table).

Its history input is declared Texture2D. The game binds it as a
Texture2DArray SRV (FirstArraySlice 3, ArraySize 1 -- the K9 table's
"VIEW(t3 ... s3+1)"), so since ml913 we hand Metal a texture2d_array view
where the converted kernel declares texture2d: Metal returns ZERO from the
sample. The update kernel (cs_..._42124) declares the same history as
texture2darray, and the 110080 one declares its inputs 2d again; the
manual's "Texture arrays" section says this is legal HLSL ("shaders may
treat textures as texture arrays and vice-versa") and that the converter
only guarantees it when every 1D/2D/cube texture is ALLOCATED as an array
and the shader is compiled with IRCompatibilityFlagForceTextureArray
(pre-3.0 default behaviour). That is also why ml913's dimension-correct
views fixed the "expected 2DArray got 2D" faults for the update kernel and
simultaneously silenced the resolve kernel: the two kernels want opposite
view types for one resource, and no per-view choice satisfies both.

ml932 (Madeira-ml932.ipa, Debug, 70.1 MB; the runtime DLL is also in the
installed bundle but the shim change needs the IPA):
- shim: IRCompilerSetCompatibilityFlags(ForceTextureArray) on every
  conversion (VS/PS/GS/CS).
- runtime: every 1D/2D texture is created as 2DArray (1D: height 1),
  multisampled as 2DMultisampleArray; SRV/UAV dimensions TEXTURE1D/2D/2DMS
  map to the array types with one slice, TEXTURECUBE to CubeArray (6);
  full-range views collapse to the base texture as before. 3D unchanged.
Expect: the resolve output non-zero, the tonemapper fed real scene
colour, histogram populated, exposure adapting, the cave on screen.
Risk: any code that assumed a plain 2D texture (presenter drawables are
still 2D and are never sampled; blits and attachments carry slices).

## Addendum 33 — 2026-09-16 09:05: THE GAME RENDERS IN-GAME WITH LIGHTING (ants85, Madeira-ml932.ipa)

User screenshot: cave interior, sky opening with sunlit terrain, the ant on
the rock, HUD, "1.0 FPS - 5534 frames" in the host window. First real
image of this title on the native D3D12 path. ml932 (ForceTextureArray +
arrays everywhere) was the last blocker; the zero scene colour came from
the Texture2D/Texture2DArray declaration mismatch across TSR kernels.

Still wrong, in the order to look at them:
1. Black parallel-line pattern on rock faces, plus black chunks that
   flicker in and out. Candidates: shadow-map acne/self-shadowing now that
   comparison samplers exist (ml923) with depth bias/slope not applied as
   D3D expects; TSR history rejection flicker; Nanite-off fallback
   meshes. Discriminate with a host dump: look at the shadow atlas
   (12288x2048), the lit scene colour before TSR (736x416 att) vs after.
2. Faint translucent vertical streaks over the whole frame: present since
   the grey era; the local-exposure bilateral grid (7 columns) or film
   grain / dither. Dump the tonemapper output with r.Tonemapper.Quality=0
   or r.LocalExposure=0 in the batch to bisect.
3. Intermittent crash at the loading->level transition (ants77/78 null
   PSO fixed; ants83 "Background Worker #3" exe+0x5f61e4f reading a
   TEB-band page -- not attributed).
4. Performance: ~1 FPS under host shader validation; turn validation off
   (restart the rmetald loop without MTL_SHADER_VALIDATION) before any FPS
   number is quoted. Then transport batching.
5. Re-enable r.Nanite, GI, VSM, volumetrics in the batch one at a time.

## Addendum 34 — 2026-09-16: NANITE RENDERS; the hatching was never ours; four Lumen theories killed; Lumen still floods the cave (ml933-ml935)

Written for review. The headline results are solid; several of my own
intermediate conclusions were wrong and are retracted explicitly below, so
they do not get re-inherited.

### Milestones

1. **Nanite renders.** `r.Nanite=1` works. `MicropolyRasterize`,
   `HWRasterizeVS/PS`, `NodeAndClusterCull`, `RasterBinBuild`, `PatchSplit`,
   `CalculateSafeRasterizerArgs` all convert and dispatch. In the best run
   `HWRasterize=1454` vs `MicropolyRasterize=750`, i.e. the hardware path
   carries more work than the compute path.
2. **The black hatching on rock faces is gone, and it was never a defect in
   our stack.** It is UE's interleaved-gradient dither for LOD crossfades on
   *non-Nanite* meshes. Nanite has continuous LOD and never dithers, so
   turning Nanite on removed the source. ⚠️ This also means the earlier
   CrossOver reference screenshot was taken with Nanite ON and therefore
   never had the dither either — several rounds of "our tonemapper amplifies
   noise ~6x versus the reference" were measured against a reference whose
   input had nothing to amplify. The tonemapper and TSR were behaving
   correctly the whole time. See Retractions.

### Code changes

| rev | change | why |
| --- | --- | --- |
| ml933 | rmetald dump: array-aware depth copy (`depth2d_array`), per-slice dumps (`_sN`), 3D volumes dumped by z-plane, raw float `.txt` beside small float textures, dump dir wiped per dump | every depth dump had been a constant since ml932 made all 2D textures arrays; slice 0 only hid array content; 3D textures were skipped outright; `rm_tone` clamps negatives to 0 and saturates at 255, which is exactly where control-buffer values live |
| **ml934** | `exec_begin_render`: allow a render pass with **no colour target and no depth**; area comes from the viewport | Nanite's HW rasteriser binds neither and writes only UAVs. We returned 0, so every such draw was dropped — and dropped *before* the skip counter, so nothing logged it. This is the Nanite hole fix. |
| ml934b | `exec_same_targets`: include the render area for attachment-less passes (`enc_w/enc_h`), via one shared `exec_attachless_area()` helper | with no attachments there was nothing to compare, so a pass at 12288x2048 could inherit an encoder created at 736x416 |
| ml934d | `MC_RTS`: end the open encoder when either side of the boundary is attachment-less | Metal tracks hazards only BETWEEN encoders. Consecutive attachment-less passes shared one encoder, so a cull pass could read a list the previous pass had not finished writing — raced differently every frame. This is the "geometry cycles in and out" fix. |
| ml935 | probe only: log `FirstWSlice`/`WSize` for 3D UAVs | see Killed theories #4 |

Deployed hashes (bundle `35903541-…`, VM `192.168.64.9`): ml934 `2ddffe69`,
ml934b `be8117a3`, ml934d `df80f623`, ml935 `a90f5749`. `Madeira-ml934.ipa`
at the repo root contains ml934 **only** — not ml934b/d/ml935.

### Solid, with evidence

- Attachment-less passes are used by four shader pairs, not just Nanite:
  `HWRasterizeVS/PS` (294 draws), `ShadowObjectCullVS/PS` (12),
  `RasterizeToRectsVS/ClearTextureRWPS` (3), `MeshSDFObjectCullVS/PS` (3).
  The latter three had been silently dropped since long before Nanite or
  Lumen — ml934 is the first time they have ever executed.
- ml934 tripled shadow-caster coverage: the 12288x2048 atlas went from
  **63.2%** still at the cleared value to **20.5%**. Nanite rasterises shadow
  depths, so the dropped draws were removing casters as well as visible
  geometry. That is why light streamed through solid rock.
- 64-bit atomics are real, measured: `InterlockedMax` on `RWTexture2D<uint64_t>`
  and `InterlockedMax64` on `RWByteAddressBuffer` both convert through MSC and
  return the correct maximum with 64 threads contending. Nanite's raster is
  not bluffing. **M4 Max (Apple9) only** — the A15 and the paravirtual device
  are untested, and matter if we ever render locally.
- Array-slice mapping is correct in both directions: UAV write and SRV read
  through a one-slice view of a 4-slice array, with `ForceTextureArray`, 3/3.
- `ExecuteIndirect` has never been called, even with Nanite on. The
  count-buffer gap is still unimplemented but still dormant.
- Lumen executes and its surface cache carries light: card depth 0% blank,
  albedo 74%, normal 55%, and **two of four** RG11B10F card lighting atlases
  populated at 78% blank — the same footprint as the geometry atlases.
  `LumenCardBatchDirectLightingCS` dispatches 29 threadgroups; radiosity and
  `CombineLumenSceneLightingCS` run. `ConeTraceGlobalOcclusion` goes 1 -> 0
  when Lumen turns on, so DFAO is correctly handed over, not double-counted.

### Killed theories (all tested, none cost a device run except #4)

1. **Tonemapper amplifies noise.** Refuted by the Nanite result: the dither
   source was the non-Nanite mesh path. The tonemapper is a monotone
   per-pixel curve — 98.7% sign agreement with its input, 46.8% (i.e.
   uncorrelated) with the pre-TSR buffer, so it reads the right resource.
2. **RG11B10Float not writable from compute.** `RGBA8Unorm`, `RGBA16Float`,
   `RG11B10Float`, `RGB9E5Float` all accept `ShaderWrite` and the data lands.
3. **3D UAV writes broken.** Full production path (DXIL -> MSC with
   `ForceTextureArray` -> descriptor table -> `kIRArgumentBufferBindPoint`):
   all 8 depth slices of an 8^3 `RWTexture3D<float>` wrote exactly.
4. **3D UAV depth windows ignored.** True — `FirstWSlice`/`WSize` are read
   for RTVs and never for UAVs, and Metal cannot express a depth window in a
   view. But the ml935 probe shows **`FirstWSlice=0` in all 16 cases**, so it
   is dormant. Worth fixing for correctness, not a current cause.
5. **Oversized attachment-less render area page-faults.** A 100 MB overrun
   into a 16 KB buffer completes cleanly; out-of-bounds fragment writes do
   not fault on this GPU. So the GPU page fault seen with Lumen (30 errors,
   `enc#166583 render`, `kIOGPUCommandBufferCallbackErrorPageFault`) was not
   this. Those errors have not recurred since ml934b.

### Retractions (my errors — please check the surviving reasoning too)

- "The shadow atlas is empty" — false. The depth dump path was broken by
  ml932 (a `depth2d` binding fed a 2DArray), so every depth dump read a
  constant. 1424 shadow-depth draws/frame were happening all along.
- "TSR locks the dither into its history" then "TSR works, the tonemapper
  amplifies" — both overstated. I mislabelled the 816x460 RGB10A2 render
  targets as TSR's output; they are the tonemapper's. Superseded entirely
  by the Nanite finding.
- "Lumen isn't lighting anything" — over-read. Two of the four card lighting
  atlases are populated.
- "The distance field is 93% saturated" — **false, and the worst of these.**
  Those 1224x692 R8 volumes are the TSR history guide planes, which I had
  already characterised at 75-82% saturated earlier in the same session. The
  actual candidate volumes (1024^2 and 512^2, 8 planes) are ~75% near-zero,
  plausibly normal for a sparse brick atlas. **There is no evidence the
  distance field is broken.**
- "The bloom flashes are downstream of the missing geometry" — false; they
  persist with geometry complete. Scene colour peaks at 18.6 linear with
  0.05% of pixels above 10, which is sane. The flashes are downstream of the
  cave being too bright: auto-exposure lifts the frame to make a dim cave
  visible, which blows the genuinely bright exit past the bloom threshold.

### Open

1. **Lumen floods the cave.** Cave-to-sky ratio ~196x against the
   reference's >=48,000x (reference numbers are display values, so the true
   linear gap is larger). Cave median linear radiance 0.095. The one
   remaining anomaly is lopsided trace counts —
   `ScreenProbeTraceScreenTextures=33`, `TraceMeshSDFs=3`, `TraceVoxels=1` —
   but **I do not know what those should be**, which is why four theories
   died. Recommended: stop theorising and get ground truth from D3DMetal.
   Both sides now carry matched `UserEngine.ini` (Nanite on, Lumen on,
   960x540, identical scalability groups); `xcap.dylib` in the bottle is now
   a **universal** binary (the arm64-only build killed every wine process —
   `cxbottle.conf` backup is `cxbottle.conf.bak-madeira`).
2. **Intermittent crash, ~50% of launches, unrelated to Nanite or Lumen.**
   Presents as a hang because a UE fatal deadlocks against its own crash
   reporter. Signature: 7-8 `C0000005`, first at `exe+0x5F63/0x5F64` band —
   a **store**, `movq %rax,0x8(%rbx)`, inside a bounds-checked unrolled copy
   loop — then repeated `exe+0xF46D55`, `lock cmpxchgq %r14,(%rcx)` with rcx
   NULL. Runs that reach the level have **zero** C0000005. So some buffer is
   shorter than the game believes. Eliminated: ClearUAV byte-fill (4
   occurrences in healthy runs, 0 in one crashing run), QueryInterface
   refusals (identical 16 IIDs in all runs), unknown formats (same 6),
   pipeline failures (none), GPU errors (0), `GetCopyableFootprints`
   (conservative; unknown formats fall back to 4 bytes/pixel and log).
   Needs a probe mapping a faulting address to the owning D3D12 resource.
3. **`ClearUnorderedAccessViewUint` with value 1 writes `0x01010101`** — the
   fill is byte-granular. Real, fires ~4x/run, does not correlate with any
   crash. Texture UAV clears are also a silent no-op (never fired yet).
4. **FPS** ~2 with Nanite, transport-latency bound: RPCs are 50-70% of wall
   at ~0.2 ms mean, ~30-60 flushes per census window. Batching is the fix.
5. ⚠️ **The periodic auto-dump (ml899, every 100 presents from 1500) is
   gated by `/tmp/rmetald-no-autodump`, which a `/tmp` wipe deletes.** It
   then freezes rendering for seconds every 100 frames and reads as a
   rendering bug. Recreate that file after any reboot.
