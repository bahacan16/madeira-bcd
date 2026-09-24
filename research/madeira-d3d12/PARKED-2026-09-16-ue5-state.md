# Parked: UE 5.4 / Empire of the Ants on the native D3D12 path

**Parked 2026-09-16.** Written to be picked up cold, by me or by anyone else.
Nothing here needs the conversation it came from.

Companion documents:
- `HANDOFF-empire-black-scene.md` — the full narrative, addenda 1-34. Addendum
  34 covers the same ground as this file in review form, aimed at Astra.
- `tests/offline/README.md` — the offline Metal/converter harnesses and what
  each one settled.

---

## 1. Where it stands

The game **renders in-game with Nanite**, at roughly 2 FPS through remote
Metal. Geometry is complete and stable. Shadows work. Lighting is correct in
structure but the cave interior is far too bright.

Two things were declared "solved" this session and both hold up:

1. **Nanite works.** `r.Nanite=1` converts and dispatches the whole pipeline —
   `MicropolyRasterize`, `HWRasterizeVS/PS`, `NodeAndClusterCull`,
   `RasterBinBuild`, `PatchSplit`, `CalculateSafeRasterizerArgs`. In the best
   run the hardware path carried more work than the compute path
   (`HWRasterize=1454` vs `MicropolyRasterize=750`).
2. **The black hatching on rock faces is gone, and was never our bug.** It is
   UE's interleaved-gradient dither for LOD crossfades on *non-Nanite* meshes.
   Nanite has continuous LOD and never dithers.

The one substantial thing still wrong is lighting: the cave is lit roughly
uniformly instead of being nearly black. See §7.

---

## 2. Exact state of everything

### Deployed code

| rev | hash | what |
| --- | --- | --- |
| ml934 | `2ddffe69bab498bc` | attachment-less render passes (the Nanite hole fix) |
| ml934b | `be8117a3a56ead11` | + render area in the encoder-reuse comparison |
| ml934d | `df80f6234afdfdf1` | + a fresh encoder per attachment-less pass |
| **ml935** | **`a90f574997fb1e1a`** | **+ the 3D-UAV depth-window probe — CURRENTLY DEPLOYED** |

`Madeira-ml934.ipa` at the repo root contains **ml934 only**. If it goes to
anyone, they do not have ml934b/d/ml935. Rebuild from source for a current one.

### Devices and paths

- **VM**: `root@192.168.64.X` port 22222, password `alpine`. ⚠️ **The address
  increments on every VM rebuild** — it went .6 → .7 → .9 in one day. The
  *host* side is always `192.168.64.1`, so rmetald never needs changing;
  only SSH does. Find the live one with `arp -a | grep 192.168.64`.
- **App container**: `…/Data/Application/<UUID>/Documents`. ⚠️ **The UUID
  changes on reinstall.** Find it with
  `ls -lt /var/mobile/Containers/Data/Application/*/Documents/madeira-log.txt`.
  At parking: `6FF0ECFB-D3A6-4939-9421-CB1CFF733F70`.
- **App bundle** (where the DLLs actually load from — *not* the prefix):
  `/var/containers/Bundle/Application/<UUID>/Madeira.app/arm64ec-windows/`.
  At parking: `35903541-2700-48EF-A3E2-AD132DE5EF55`. ⚠️ Also changes on
  reinstall, and a deploy to a stale path silently does nothing. **Always
  verify by hash**, both sides, after every deploy.
- No `awk` on the VM; `sha256sum` yes, `shasum` no, `scp` no (use
  `ssh … 'base64 -d > dest'`).

### Deploy recipe

```sh
bash build/madeira-d3d12/build-pe.sh
B="/var/containers/Bundle/Application/<UUID>/Madeira.app/arm64ec-windows"
for f in madeira_d3d12.dll d3d12.dll; do
  base64 -i build/madeira-d3d12/out-pe/$f | sshpass -p alpine ssh -p 22222 \
    root@192.168.64.X "base64 -d > '$B/$f'"
done
# then verify, always:
shasum -a 256 build/madeira-d3d12/out-pe/madeira_d3d12.dll | cut -c1-16
sshpass -p alpine ssh -p 22222 root@192.168.64.X "sha256sum '$B/madeira_d3d12.dll' | cut -c1-16"
cp build/madeira-d3d12/out-pe/*.dll app/Madeira/arm64ec-windows/   # so an IPA carries it
```

Debug configuration only. **Release has crashed the guest.**

### Host (rmetald)

```sh
cd research/remote-metal
RMETAL_TOKEN=37e133031705a59823b8544e nohup sh -c '
  while ! ifconfig | grep -q "inet 192.168.64.1 "; do sleep 2; done
  while :; do host/rmetald 192.168.64.1; sleep 1; done
' >>/private/tmp/rmetald-ml885.log 2>&1 &
touch /tmp/rmetald-no-autodump     # DO NOT SKIP -- see below
```

The bridge-wait loop matters: `192.168.64.1` only exists while the VM is up,
and rmetald exits immediately if it cannot bind. The token must match
`Documents/madeira-remote.txt` in the container.

### ⚠️ Three traps that have each cost real time

1. **`/tmp/rmetald-no-autodump` must exist.** Without it, ml899's schedule
   fires a full ~250-file frame dump every 100 presents between 1500 and
   3200, freezing rendering for seconds at a time. A `/tmp` wipe deletes the
   flag and it silently comes back, reading exactly like a rendering bug.
2. **Nanite streams for a long time at 2 FPS.** Cluster data pages in on a
   per-frame budget, so a fresh load looks like large missing geometry for a
   while and then fills in. I started diagnosing this twice. Let the scene
   settle before measuring anything.
3. **`MTL_SHADER_VALIDATION=1` costs most of the frame rate** and has never
   reported anything on this title. Do not leave it on.

### Configuration (`Empire-dx12.bat` in the game folder)

A **Shipping build never reads `Saved/Config/Windows/Engine.ini`** — cooked
builds skip the generated ini. Overrides must go in the three `UserEngine.ini`
layers the bat writes, and the bat rewrites them on every launch, so editing
the ini files directly is pointless.

```
r.RDG.TransientAllocator=0              resource aliasing, predates this work
s.*TimeLimit / *ExtraTime / *Granularity  ml900: loading is time-sliced per
                                        frame and our frames take seconds.
                                        NOT lighting. Keep these in any test.
r.Nanite=1                              on, works
r.DynamicGlobalIlluminationMethod=1     Lumen on
r.Lumen.DiffuseIndirect.Allow=1         gates the above; both or neither
r.SkyLight.RealTimeReflectionCapture=1  measured improvement, keep (§5)
r.Shadow.Virtual.Enable=0               VSM never tried
r.ReflectionMethod=0                    Lumen reflections never tried
r.VolumetricFog=0  r.VolumetricCloud=0  never tried
```

`Empire-stock.bat` sits beside it: everything at engine defaults except the
`s.*` loading slices and the RDG allocator. That is the configuration the
CrossOver reference screenshots were taken in, and it is the cleanest
comparison available. It has been attempted once and lost its coin flip to
the loading crash (§8.2).

---

## 3. Code changes, with reasons

All in `src/pe/madeira_d3d12.c` unless noted.

### ml934 — attachment-less render passes (the important one)

`exec_begin_render` had:

```c
if (!e->nrt && !e->depth) return 0;
```

Nanite's hardware rasteriser binds **no colour target and no depth** — it
rasterises into UAVs, using the 64-bit atomics. D3D12 allows this; we refused
to create an encoder, so every such draw was dropped. Worse, dropped *before*
the skipped-draw counter, so nothing in the log ever named it.

Metal renders attachment-less passes given `renderTargetWidth/Height` and
`defaultRasterSampleCount`, all three of which were **already** plumbed
through `WMTRenderPassInfo` and honoured by both the remote host and the local
path. Nothing can infer the area without attachments, so it comes from the
viewport the command list has set (`exec_attachless_area()`).

**Effect**: closed the Nanite holes, and took the 12288x2048 shadow atlas from
**63.2%** still at its cleared value to **20.5%** — Nanite rasterises shadow
depths too, so the dropped draws had been removing casters as well as visible
geometry. That is why light streamed through solid rock.

This also un-dropped three pass types that had been discarded since long
before Nanite or Lumen: `ShadowObjectCullVS/PS`, `MeshSDFObjectCullVS/PS`,
`RasterizeToRectsVS/ClearTextureRWPS`. ml934 is the first time they have ever
executed. Attachment-less draws now run at 736x416, 12288x2048, 1024x1024,
512x512, 256x256, 192x192 and 12x7 — every viewport matching its scissor.

### ml934b — the area belongs in the encoder comparison

`exec_same_targets` decides encoder reuse by comparing attachments. An
attachment-less pass has none, so they all compared equal and a pass at
12288x2048 could inherit an encoder created at 736x416. Added `enc_w/enc_h`,
compared through the same `exec_attachless_area()` helper so the two call
sites cannot drift.

**Effect**: the GPU page fault stopped. 30 GPU errors before, 0 after, and
none since.

### ml934d — one encoder per attachment-less pass

`MC_RTS` (our `OMSetRenderTargets`) only updated state; it never closed the
encoder. So consecutive attachment-less passes shared one encoder — and
**Metal tracks hazards only between encoders, never within one**. UE's cull
chain is "pass writes a visibility list, next pass reads it", so the reader
could see a list the writer had not finished. That races differently every
frame. `MC_RTS` now ends the encoder when either side of the boundary is
attachment-less; passes with real attachments keep the reuse optimisation.

**Effect**: geometry stopped cycling in and out.

### ml935 — probe only

Logs `FirstWSlice`/`WSize` for 3D UAVs. See §6.4 for the result. ⚠️ The log
line prints `tex_layers` where it means depth, so 3D textures show as `xN=1`.
Harmless, but fix it before believing that field.

### ml933 — the instrument (in `research/remote-metal/host/rmetald.m`)

Four dump defects, each of which had produced a wrong conclusion:

- Depth dumps bound a 2DArray to a `depth2d` sampler, so **every depth dump
  had read a constant since ml932**. Now picks `depth2d_array` by texture
  type. This is what made the shadow atlas look empty while 1424 shadow
  draws a frame were filling it.
- Only array slice 0 was dumped. Now one PNG per slice, `_sN`.
- 3D textures were skipped entirely, which hid the colour-grading LUT, the
  local-exposure grid and the volumetric froxels. Now dumped by z-plane.
- `rm_tone` clamps negatives to 0 and saturates at 255 — exactly where
  control-buffer values live. Float textures of ≤8192 pixels now also write a
  `.txt` of raw numbers. This is what finally made the bilateral grid
  readable (its R channel is negative log-luminance, not zero).
- Dump directories are wiped per dump; stale PNGs from earlier runs were
  being read as the current frame.

---

## 4. Instrumentation available

| trigger | effect |
| --- | --- |
| `touch /tmp/rmetald-dump-now` | one full frame dump to `/tmp/rmetald-dump/fN/` — every attachment, every live written texture, per-slice, per-z-plane, plus `.txt` raw values for small float textures |
| `touch /tmp/rmetald-gputrace-now` | one-frame Xcode GPU trace; needs `MTL_CAPTURE_ENABLED=1` on rmetald |
| `/tmp/rmetald-no-autodump` | **absence** enables the every-100-presents auto dump. Keep it present. |

Guest log tags worth knowing: `[attachless]` (every attachment-less pass with
its encoder number — host and guest encoder counters are in **lockstep**, so a
faulting `enc#N` from a GPU error resolves directly to one of these lines),
`[uav3d]`, `[draw-dump]`, `[dispatch-dump]`, `[cap]`/`[cap-data]`, `[srv-stuck]`
(names the wineserver objects a wedged thread is waiting on), `[waiters]`.

Log path: `<container>/Documents/madeira-log.txt`, rotated to
`madeira-log.prev.txt` **on app launch** — not on bat launch, since the bat
runs inside the already-running app.

---

## 5. Findings that hold up

- **Attachment-less passes are used by four shader pairs**, not just Nanite
  (counts from one in-level run): `HWRasterizeVS/PS` 294, `ShadowObjectCull`
  12, `RasterizeToRects/ClearTextureRW` 3, `MeshSDFObjectCull` 3.
- **64-bit atomics are real**, measured through MSC: `InterlockedMax` on
  `RWTexture2D<uint64_t>` and `InterlockedMax64` on `RWByteAddressBuffer`
  both return the correct maximum under contention. **M4 Max / Apple9 only.**
- **Array-slice mapping is correct** in both directions, 3/3.
- **`ExecuteIndirect` has never been called**, even with Nanite on. The
  count-buffer gap is unimplemented but dormant.
- **Lumen executes and its surface cache carries light**: card depth 0%
  blank, albedo 74%, normal 55%, and two of four RG11B10F card lighting
  atlases populated at 78% blank — the same footprint as the geometry
  atlases. `LumenCardBatchDirectLightingCS` dispatches 29 threadgroups.
  `ConeTraceGlobalOcclusion` goes 1 → 0 when Lumen turns on, so DFAO is
  correctly handed over rather than double-counted.
- **The GBuffer material is correct**: GBufferB reads metallic **0**,
  specular 0.50, roughness **0.93**; GBufferC is proper rock albedo. So the
  metallic sheen users see is added at the lighting stage, not by the
  material — which rules out the material path and the Nanite shading pass.
- **`r.SkyLight.RealTimeReflectionCapture=1` measurably helps.** Walls came
  down 18-22% while the exterior barely moved (131.9 → 127.9). That
  asymmetry is the signature of real occlusion rather than a dead cubemap,
  which would have dragged the exterior down too. Corroborated by natural
  shadows appearing on the log in the menu scene.

---

## 6. Refuted — do not re-tread

Each was tested, not argued away.

1. **The tonemapper amplifies noise ~6x.** Refuted by the Nanite result: the
   dither came from the non-Nanite mesh path, and the CrossOver reference had
   Nanite **on**, so its input had nothing to amplify. Several rounds of
   measurement were against a mismatched baseline. The tonemapper is a
   monotone per-pixel curve: 98.7% sign agreement with its input, 46.8%
   (uncorrelated) with the pre-TSR buffer, so it reads the right resource.
2. **`RG11B10Float` is not writable from compute.** All of `RGBA8Unorm`,
   `RGBA16Float`, `RG11B10Float`, `RGB9E5Float` accept `ShaderWrite` and the
   data lands. (`tests/offline/rg11b10/`)
3. **3D UAV writes are broken.** Full production path — DXIL → MSC with
   `ForceTextureArray` → descriptor table → `kIRArgumentBufferBindPoint` — all
   8 depth slices of an 8³ `RWTexture3D<float>` wrote exactly.
   (`tests/offline/tex3d/`)
4. **3D UAV depth windows are ignored.** *True but dormant.*
   `FirstWSlice`/`WSize` are honoured for RTVs and never for UAVs, and Metal
   cannot express a depth window in a texture view at all — only array
   layers. But the ml935 probe shows **`FirstWSlice=0` in all 16 cases**.
   Worth fixing for correctness; not a current cause.
5. **An oversized attachment-less render area page-faults.** A 100 MB overrun
   into a 16 KB buffer completes cleanly; out-of-bounds fragment writes do not
   fault on this GPU. (`tests/offline/attachless/`)

### Retracted claims of mine — check the surviving reasoning too

- "The shadow atlas is empty" — false, the depth dump path was broken (§3).
- "TSR locks the dither into its history", then "TSR works, the tonemapper
  amplifies" — both overstated; I had mislabelled the 816x460 RGB10A2 render
  targets as TSR's output when they are the tonemapper's.
- "Lumen isn't lighting anything" — over-read; two of four card lighting
  atlases are populated.
- "The distance field is 93% saturated" — **false, and the worst.** Those
  1224x692 R8 volumes are the TSR history guide planes, which I had already
  characterised at 75-82% saturated earlier in the same session. The real
  candidate volumes (1024², 512², 8 planes) are ~75% near-zero, plausibly
  normal for a sparse brick atlas. **There is no evidence the distance field
  is broken.**
- "The bloom flashes are downstream of the missing geometry" — false, they
  persist with geometry complete. Scene colour peaks at 18.6 linear with
  0.05% of pixels above 10, which is sane. They are downstream of the cave
  being too bright: auto-exposure lifts the frame to make a dim cave
  visible, which blows the genuinely bright exit past the bloom threshold.

Five theories died in a row. That pattern is the main argument for §7's
recommendation.

---

## 7. The open problem: the cave is far too bright

Latest measurements, final 960x540 frame against the CrossOver reference:

| region | ours | reference |
| --- | --- | --- |
| left wall / foreground | 60.4 | **0.7** |
| right wall | 52.9 | **1.7** |
| cave opening (exterior) | 127.9 | 95.6 |
| full frame | 63.2 | 13.1 |

Cave-to-exterior ratio **2.1x** against the reference's **≥48,000x** (the
reference figures are display values, so the true linear gap is larger).
Cave median linear radiance **0.095**; the reference implies ~2e-6.

The exterior is close to correct. The problem is entirely that surfaces which
should be occluded are receiving light.

The one measured anomaly left is Lumen's trace distribution:
`ScreenProbeTraceScreenTextures` **33**, `TraceMeshSDFs` **3**,
`TraceVoxels` **1**. Screen-space traces dominate and the long-range
fallbacks barely run, so a ray leaving the screen returns sky. A cave whose
off-screen rays all return sky is lit uniformly — which is what we see.

**But I do not know what those counts should be**, and that is exactly why
five theories died. The recommendation is to stop theorising and get ground
truth from D3DMetal rather than flip more cvars:

1. **A CrossOver screenshot at matched settings.** The bottle at
   `~/Library/Application Support/CrossOver/Bottles/Test` already has matched
   `UserEngine.ini` in all three layers (Nanite on, Lumen on, 960x540,
   identical scalability groups), plus `Empire-madeira-match.bat` and
   `GameUserSettings.ini` at 960x540 (original backed up as
   `.bak-madeira`). The existing reference predates that config, so its
   settings are unknown and every number above is approximate.
2. **A Metal capture from inside D3DMetal**, to compare Lumen's atlases and
   trace counts against a working implementation. `xcap.dylib` is installed in
   the bottle and is now a **universal** binary — it watches for
   `/tmp/d3dm-capture-now` and arms itself only in the process that has
   actually loaded D3DMetal. ⚠️ An **arm64-only** build of it killed every
   wine process in the bottle: CrossOver's loaders are **x86_64**, and
   `DYLD_INSERT_LIBRARIES` pointing at the wrong architecture aborts the
   process rather than being skipped. `cxbottle.conf` is currently **clean**;
   the backup is `cxbottle.conf.bak-madeira` and the broken version is
   `cxbottle.conf.broken`. Test any injection on one throwaway wine command
   before touching the bottle config.

Untried lighting features that may each close part of the gap:
`r.Shadow.Virtual.Enable`, `r.ReflectionMethod`, `r.VolumetricFog`. Each adds
dispatches and we are transport-bound, so expect FPS cost.

---

## 8. Other open items, in rough priority

### 8.1 The intermittent loading crash — the most expensive problem

**Roughly half of all launches.** Unrelated to Nanite or Lumen; it predates
both. Presents as a hang, not a crash, because a UE fatal deadlocks against
its own crash reporter — GameThread, RHIThread and RenderThread all park in
`wait_select_reply`.

Signature: 7-10 `C0000005`. The first is a **store** —
`movq %rax, 0x8(%rbx)` — inside a bounds-checked unrolled copy loop at
`exe+0x5F63/0x5F64` band; then repeated `exe+0xF46D55`,
`lock cmpxchgq %r14,(%rcx)` with `rcx` NULL, which is the cascade. Runs that
reach the level have **zero** C0000005. So some buffer is shorter than the
game believes it is.

Eliminated: the ClearUAV byte-fill (4 occurrences in healthy runs, 0 in one
crashing run), QueryInterface refusals (byte-identical 16 IIDs in all runs),
unknown texture formats (same 6 in all runs), pipeline creation failures
(none logged), GPU errors (0), `GetCopyableFootprints` (conservative; unknown
formats fall back to 4 bytes/pixel and log).

**What it needs**: a probe mapping a faulting address to the owning D3D12
resource. The fault machinery already prints addresses and pool ownership;
what is missing is resource attribution. That is an IPA build and a couple of
runs, and it would pay for itself quickly at a 50% failure rate.

### 8.2 `ClearUnorderedAccessViewUint` writes the wrong value

The fill is byte-granular, so a clear to `1` writes `0x01010101` — 16,843,009
instead of 1. Real, fires ~4x per run, logged honestly as "approximated by
byte". Does **not** correlate with any crash. Texture UAV clears are
separately a silent no-op and have never fired. Fixing it properly needs a
32-bit pattern fill: `fillBuffer:` is byte-only, so either a small compute
kernel or a staging-buffer blit.

### 8.3 Performance: ~2 FPS, transport-latency bound

RPCs are 50-70% of wall time at ~0.2 ms mean, tens of flushes per census
window. The fix is batching, not GPU work. Nanite and Lumen both add
dispatches on top.

### 8.4 `ExecuteIndirect` count buffers

Unimplemented — we issue every record up to the maximum instead of the
GPU-written count. Dormant (never called, even with Nanite on), but live code
the moment something uses it. Doing it properly means Metal indirect command
buffers.

---

## 9. If picking this up cold

1. Find the VM address and the two UUIDs (§2). Verify the deployed DLL hash
   matches `build/madeira-d3d12/out-pe/`.
2. Start rmetald with the bridge-wait loop, and `touch
   /tmp/rmetald-no-autodump`.
3. Launch, and expect a coin flip (§8.1). Let Nanite streaming settle before
   measuring anything.
4. Read §6 before forming a theory. Five plausible ones have already died,
   and four of the five were killed offline for free — prefer that to a
   device run.
