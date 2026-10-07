#!/usr/bin/env python3
"""Experimental frame generation in DXMT's winemetal present path.

dxmt/src/winemetal/unix/winemetal_unix.c presents every frame
(D3D11 through DXMT, and D3D12, whose runtime presents through the same
winemetal call) in _MTLCommandBuffer_presentDrawable. With MADEIRA_FRAMEGEN=1
(a game's config: env.MADEIRA_FRAMEGEN = 1) that call now:

  1. copies the drawable into a history texture (the layer is made
     framebufferOnly = NO so the drawable can be read),
  2. estimates motion between the previous and this frame on the GPU (block
     matching on 1/8-size luma; the game gives us no motion vectors),
  3. runs Apple's MetalFX frame interpolator (MTLFXFrameInterpolator,
     iOS 26) on the two frames, that motion field and a flat depth,
  4. presents the generated frame at once and the real one half a frame
     later (presentDrawable:atTime:).

So the panel shows twice the game's frame rate, at the cost of half a frame
of latency and the interpolator's GPU time. The game gives no depth, UI mask
or camera, so expect artifacts at edges and on the HUD; this is an
experiment. Knobs (environment): MADEIRA_FRAMEGEN_RADIUS (search radius in
1/8-size pixels, default 6), MADEIRA_FRAMEGEN_MVSCALE (motionVectorScale,
default 1), MADEIRA_FRAMEGEN_MVSIGN (-1 flips the vectors). The FPS caps are
bypassed while it is on (the game's own rate is what gets doubled).

Idempotent; fails by name if an anchor moved (the dxmt pin changed). Run
from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: frame generation"

ANCHOR_MODULE = "static NTSTATUS\n_MTLCommandBuffer_presentDrawable(void *obj) {"

MODULE = r'''/* madeira-bcd: frame generation (tools/patch-dxmt-framegen.py). */
static int g_fg_on = -1;
static int madeira_fg_enabled(void) {
  if (g_fg_on < 0) {
    const char *e = getenv("MADEIRA_FRAMEGEN");
    g_fg_on = (e && *e && *e != '0') ? 1 : 0;
    if (g_fg_on)
      dprintf(STDERR_FILENO, "[framegen] ON (MADEIRA_FRAMEGEN): MetalFX frame interpolation between game frames\n");
  }
  return g_fg_on;
}
static float madeira_fg_envf(const char *name, float dflt) {
  const char *e = getenv(name);
  return (e && *e) ? (float)atof(e) : dflt;
}

static const char *g_fg_msl =
  "#include <metal_stdlib>\n"
  "using namespace metal;\n"
  "kernel void fg_luma(texture2d<float, access::sample> src [[texture(0)]],\n"
  "                    texture2d<float, access::write> dst [[texture(1)]],\n"
  "                    uint2 gid [[thread_position_in_grid]]) {\n"
  "  if (gid.x >= dst.get_width() || gid.y >= dst.get_height()) return;\n"
  "  constexpr sampler s(coord::normalized, filter::linear, address::clamp_to_edge);\n"
  "  float2 inv = 1.0 / float2(dst.get_width(), dst.get_height());\n"
  "  float2 uv = (float2(gid) + 0.5) * inv, q = inv * 0.25;\n"
  "  float3 c = src.sample(s, uv + float2(-q.x, -q.y)).rgb + src.sample(s, uv + float2(q.x, -q.y)).rgb\n"
  "           + src.sample(s, uv + float2(-q.x, q.y)).rgb + src.sample(s, uv + float2(q.x, q.y)).rgb;\n"
  "  dst.write(float4(dot(c * 0.25, float3(0.299, 0.587, 0.114)), 0, 0, 1), gid);\n"
  "}\n"
  "kernel void fg_flow(texture2d<float, access::read> cur [[texture(0)]],\n"
  "                    texture2d<float, access::read> prev [[texture(1)]],\n"
  "                    texture2d<float, access::write> flow [[texture(2)]],\n"
  "                    constant int &radius [[buffer(0)]],\n"
  "                    uint2 gid [[thread_position_in_grid]]) {\n"
  "  int W = cur.get_width(), H = cur.get_height();\n"
  "  if ((int)gid.x >= W || (int)gid.y >= H) return;\n"
  "  int2 p = int2(gid), hi = int2(W - 1, H - 1);\n"
  "  float best = 1e9; int2 bd = int2(0);\n"
  "  for (int dy = -radius; dy <= radius; dy++)\n"
  "    for (int dx = -radius; dx <= radius; dx++) {\n"
  "      float sad = 0.002 * float(abs(dx) + abs(dy));\n"
  "      for (int py = -2; py <= 2; py++)\n"
  "        for (int px = -2; px <= 2; px++) {\n"
  "          int2 a = clamp(p + int2(px, py), int2(0), hi);\n"
  "          int2 b = clamp(p + int2(px + dx, py + dy), int2(0), hi);\n"
  "          sad += fabs(cur.read(uint2(a)).r - prev.read(uint2(b)).r);\n"
  "        }\n"
  "      if (sad < best) { best = sad; bd = int2(dx, dy); }\n"
  "    }\n"
  "  flow.write(float4(float2(bd), 0, 0), gid);\n"
  "}\n"
  "kernel void fg_motion(texture2d<float, access::sample> flow [[texture(0)]],\n"
  "                      texture2d<float, access::write> motion [[texture(1)]],\n"
  "                      constant float &scale [[buffer(0)]],\n"
  "                      uint2 gid [[thread_position_in_grid]]) {\n"
  "  if (gid.x >= motion.get_width() || gid.y >= motion.get_height()) return;\n"
  "  constexpr sampler s(coord::normalized, filter::linear, address::clamp_to_edge);\n"
  "  float2 uv = (float2(gid) + 0.5) / float2(motion.get_width(), motion.get_height());\n"
  "  motion.write(float4(flow.sample(s, uv).rg * scale, 0, 0), gid);\n"
  "}\n";

static struct {
  NSUInteger w, h;
  MTLPixelFormat fmt;
  id interp;                                 /* id<MTLFXFrameInterpolator> */
  id<MTLTexture> hist[2], luma[2], flow, motion, depth, out;
  id<MTLComputePipelineState> k_luma, k_flow, k_motion;
  int cur, have_prev, dead;
  uint64_t last_abs;
  double dt;
  unsigned long long frames, generated, no_drawable;
} g_fg;

static void madeira_fg_release(void) {
  int i;
  for (i = 0; i < 2; i++) {
    [g_fg.hist[i] release]; g_fg.hist[i] = nil;
    [g_fg.luma[i] release]; g_fg.luma[i] = nil;
  }
  [g_fg.flow release]; [g_fg.motion release]; [g_fg.depth release]; [g_fg.out release];
  g_fg.flow = g_fg.motion = g_fg.depth = g_fg.out = nil;
  [g_fg.interp release]; g_fg.interp = nil;
  g_fg.have_prev = 0;
}

static id<MTLTexture> madeira_fg_tex(id<MTLDevice> dev, MTLPixelFormat f, NSUInteger w, NSUInteger h,
                                     MTLTextureUsage u) {
  MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:f width:w height:h mipmapped:NO];
  d.usage = u;
  d.storageMode = MTLStorageModePrivate;
  return [dev newTextureWithDescriptor:d];
}

/* Pipelines once, textures and the interpolator per drawable size/format. */
static int madeira_fg_setup(id<MTLDevice> dev, id<MTLTexture> src) {
  if (g_fg.dead) return 0;
  if (!g_fg.k_luma) {
    NSError *err = nil;
    id<MTLLibrary> lib = [dev newLibraryWithSource:[NSString stringWithUTF8String:g_fg_msl] options:nil error:&err];
    if (!lib) {
      dprintf(STDERR_FILENO, "[framegen] shader compile failed: %s -- off\n", err ? err.description.UTF8String : "?");
      g_fg.dead = 1;
      return 0;
    }
    id<MTLFunction> f1 = [lib newFunctionWithName:@"fg_luma"], f2 = [lib newFunctionWithName:@"fg_flow"],
                    f3 = [lib newFunctionWithName:@"fg_motion"];
    g_fg.k_luma = [dev newComputePipelineStateWithFunction:f1 error:&err];
    g_fg.k_flow = [dev newComputePipelineStateWithFunction:f2 error:&err];
    g_fg.k_motion = [dev newComputePipelineStateWithFunction:f3 error:&err];
    [f1 release]; [f2 release]; [f3 release]; [lib release];
    if (!g_fg.k_luma || !g_fg.k_flow || !g_fg.k_motion) {
      dprintf(STDERR_FILENO, "[framegen] pipeline creation failed -- off\n");
      g_fg.dead = 1;
      return 0;
    }
  }
  if (g_fg.interp && g_fg.w == src.width && g_fg.h == src.height && g_fg.fmt == src.pixelFormat) return 1;

  madeira_fg_release();
  g_fg.w = src.width; g_fg.h = src.height; g_fg.fmt = src.pixelFormat;
  if (@available(iOS 26.0, macOS 26.0, *)) {
    if (![MTLFXFrameInterpolatorDescriptor supportsDevice:dev]) {
      dprintf(STDERR_FILENO, "[framegen] MetalFX frame interpolation is not supported on this GPU -- off\n");
      g_fg.dead = 1;
      return 0;
    }
    MTLFXFrameInterpolatorDescriptor *desc = [[MTLFXFrameInterpolatorDescriptor alloc] init];
    desc.colorTextureFormat = g_fg.fmt;
    desc.outputTextureFormat = g_fg.fmt;
    desc.depthTextureFormat = MTLPixelFormatDepth32Float;
    desc.motionTextureFormat = MTLPixelFormatRG16Float;
    desc.inputWidth = g_fg.w;  desc.inputHeight = g_fg.h;
    desc.outputWidth = g_fg.w; desc.outputHeight = g_fg.h;
    id<MTLFXFrameInterpolator> fi = [desc newFrameInterpolatorWithDevice:dev];
    if (!fi) {
      desc.uiTextureFormat = g_fg.fmt;   /* in case a UI format is required */
      fi = [desc newFrameInterpolatorWithDevice:dev];
    }
    [desc release];
    if (!fi) {
      dprintf(STDERR_FILENO, "[framegen] no interpolator for %lux%lu fmt %lu -- off\n",
              (unsigned long)g_fg.w, (unsigned long)g_fg.h, (unsigned long)g_fg.fmt);
      g_fg.dead = 1;
      return 0;
    }
    g_fg.interp = fi;
    NSUInteger lw = (g_fg.w + 7) / 8, lh = (g_fg.h + 7) / 8;
    for (int i = 0; i < 2; i++) {
      g_fg.hist[i] = madeira_fg_tex(dev, g_fg.fmt, g_fg.w, g_fg.h, fi.colorTextureUsage | MTLTextureUsageShaderRead);
      g_fg.luma[i] = madeira_fg_tex(dev, MTLPixelFormatR16Float, lw, lh, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    }
    g_fg.flow = madeira_fg_tex(dev, MTLPixelFormatRG16Float, lw, lh, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    g_fg.motion = madeira_fg_tex(dev, MTLPixelFormatRG16Float, g_fg.w, g_fg.h, fi.motionTextureUsage | MTLTextureUsageShaderWrite);
    g_fg.depth = madeira_fg_tex(dev, MTLPixelFormatDepth32Float, g_fg.w, g_fg.h, fi.depthTextureUsage | MTLTextureUsageRenderTarget);
    g_fg.out = madeira_fg_tex(dev, g_fg.fmt, g_fg.w, g_fg.h, fi.outputTextureUsage | MTLTextureUsageShaderRead);
    if (!g_fg.hist[0] || !g_fg.hist[1] || !g_fg.luma[0] || !g_fg.luma[1] || !g_fg.flow || !g_fg.motion ||
        !g_fg.depth || !g_fg.out) {
      dprintf(STDERR_FILENO, "[framegen] texture allocation failed -- off\n");
      madeira_fg_release();
      g_fg.dead = 1;
      return 0;
    }
    dprintf(STDERR_FILENO, "[framegen] interpolator ready: %lux%lu fmt %lu, motion search on %lux%lu\n",
            (unsigned long)g_fg.w, (unsigned long)g_fg.h, (unsigned long)g_fg.fmt, (unsigned long)lw, (unsigned long)lh);
    return 1;
  }
  dprintf(STDERR_FILENO, "[framegen] needs iOS 26 -- off\n");
  g_fg.dead = 1;
  return 0;
}

static void madeira_fg_dispatch(id<MTLComputeCommandEncoder> enc, id<MTLComputePipelineState> p, NSUInteger w, NSUInteger h) {
  [enc setComputePipelineState:p];
  [enc dispatchThreads:MTLSizeMake(w, h, 1) threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
}

/* Returns 1 when it presented (the generated frame, then the real one). */
static int madeira_fg_present_inner(id<MTLCommandBuffer> cb, id<CAMetalDrawable> drawable) {
  static mach_timebase_info_data_t tb;
  id<MTLTexture> src = drawable.texture;
  id<MTLDevice> dev = src.device;
  uint64_t now = mach_absolute_time();
  int ni, have_prev;

  if (!tb.denom) mach_timebase_info(&tb);
  if (g_fg.last_abs) {
    double d = (double)(now - g_fg.last_abs) * tb.numer / tb.denom / 1e9;
    if (d < 1.0 / 120.0) d = 1.0 / 120.0;
    if (d > 1.0 / 15.0) { d = 1.0 / 15.0; g_fg.have_prev = 0; }   /* a stall: no stale history */
    g_fg.dt = g_fg.dt > 0 ? g_fg.dt * 0.8 + d * 0.2 : d;
  }
  g_fg.last_abs = now;
  if (!src || !madeira_fg_setup(dev, src)) return 0;

  ni = g_fg.cur ^ 1;
  have_prev = g_fg.have_prev;
  {
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:src toTexture:g_fg.hist[ni]];
    [blit endEncoding];
  }
  {
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setTexture:g_fg.hist[ni] atIndex:0];
    [enc setTexture:g_fg.luma[ni] atIndex:1];
    madeira_fg_dispatch(enc, g_fg.k_luma, g_fg.luma[ni].width, g_fg.luma[ni].height);
    if (have_prev) {
      int radius = (int)madeira_fg_envf("MADEIRA_FRAMEGEN_RADIUS", 6.0f);
      float scale = 8.0f * madeira_fg_envf("MADEIRA_FRAMEGEN_MVSIGN", 1.0f);
      if (radius < 1) radius = 1;
      if (radius > 16) radius = 16;
      [enc setTexture:g_fg.luma[ni] atIndex:0];
      [enc setTexture:g_fg.luma[g_fg.cur] atIndex:1];
      [enc setTexture:g_fg.flow atIndex:2];
      [enc setBytes:&radius length:sizeof radius atIndex:0];
      madeira_fg_dispatch(enc, g_fg.k_flow, g_fg.flow.width, g_fg.flow.height);
      [enc setTexture:g_fg.flow atIndex:0];
      [enc setTexture:g_fg.motion atIndex:1];
      [enc setBytes:&scale length:sizeof scale atIndex:0];
      madeira_fg_dispatch(enc, g_fg.k_motion, g_fg.w, g_fg.h);
    }
    [enc endEncoding];
  }
  if (!have_prev) {   /* first frame (or after a stall): flat depth once, no generated frame */
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    rp.depthAttachment.texture = g_fg.depth;
    rp.depthAttachment.loadAction = MTLLoadActionClear;
    rp.depthAttachment.storeAction = MTLStoreActionStore;
    rp.depthAttachment.clearDepth = 0.5;
    [[cb renderCommandEncoderWithDescriptor:rp] endEncoding];
    g_fg.cur = ni;
    g_fg.have_prev = 1;
    return 0;
  }

  id<CAMetalDrawable> extra = nil;
  if (@available(iOS 26.0, macOS 26.0, *)) {
    id<MTLFXFrameInterpolator> fi = (id<MTLFXFrameInterpolator>)g_fg.interp;
    float mvs = madeira_fg_envf("MADEIRA_FRAMEGEN_MVSCALE", 1.0f);
    fi.colorTexture = g_fg.hist[ni];
    fi.prevColorTexture = g_fg.hist[g_fg.cur];
    fi.depthTexture = g_fg.depth;
    fi.motionTexture = g_fg.motion;
    fi.outputTexture = g_fg.out;
    fi.motionVectorScaleX = mvs;
    fi.motionVectorScaleY = mvs;
    fi.deltaTime = (float)g_fg.dt;
    fi.nearPlane = 0.1f;
    fi.farPlane = 1000.0f;
    fi.fieldOfView = 60.0f;
    fi.aspectRatio = (float)g_fg.w / (float)g_fg.h;
    fi.jitterOffsetX = 0; fi.jitterOffsetY = 0;
    fi.shouldResetHistory = g_fg.generated == 0;
    [fi encodeToCommandBuffer:cb];
    extra = [(CAMetalLayer *)drawable.layer nextDrawable];
  }
  g_fg.cur = ni;
  g_fg.frames++;
  if (!extra || extra.texture.width != g_fg.w || extra.texture.height != g_fg.h) {
    g_fg.no_drawable++;
    return 0;   /* the caller presents the real frame as usual */
  }
  {
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:g_fg.out toTexture:extra.texture];
    [blit endEncoding];
  }
  [cb presentDrawable:extra];
  [cb presentDrawable:drawable atTime:CACurrentMediaTime() + g_fg.dt * 0.5];
  g_fg.generated++;
  if (g_fg.generated == 1 || (g_fg.generated % 600) == 0)
    dprintf(STDERR_FILENO, "[framegen] %llu generated / %llu frames (no drawable %llu), game frame %.1f ms\n",
            g_fg.generated, g_fg.frames, g_fg.no_drawable, g_fg.dt * 1000.0);
  return 1;
}
static int madeira_fg_present(id<MTLCommandBuffer> cb, id<CAMetalDrawable> drawable) {
  int r;
  @autoreleasepool { r = madeira_fg_present_inner(cb, drawable); }
  return r;
}

'''

ANCHOR_HOOK = """  /* ml1050: this call IS the frame boundary on the encode thread. */
  if (madeira_frame_hooks_on())
    ios_frame_encode_present(0);
"""
HOOK = """  /* madeira-bcd: frame generation presents both frames itself. */
  if (mode != 2 && madeira_fg_enabled() &&
      madeira_fg_present((id<MTLCommandBuffer>)params->handle, (id<CAMetalDrawable>)params->arg))
    return STATUS_SUCCESS;
"""

ANCHOR_PROPS = "    layer.framebufferOnly = props->framebuffer_only;"
PROPS = "    layer.framebufferOnly = madeira_fg_enabled() ? NO : props->framebuffer_only;   /* madeira-bcd: frame generation reads the drawable */"


def main():
    s = PATH.read_text()
    if MARKER in s:
        print("winemetal_unix.c: frame generation already present")
        return 0
    if "static int madeira_fg_present(" in s and "MADEIRA_FRAMEGEN" in s:
        # willfaust/dxmt#15 (52e2d9a, + b106702 counting its presents)
        print("winemetal_unix.c: frame generation is upstream (willfaust/dxmt#15); nothing to do")
        return 0
    for name, anchor in (("present function", ANCHOR_MODULE), ("present hook", ANCHOR_HOOK),
                         ("layer props", ANCHOR_PROPS)):
        if s.count(anchor) != 1:
            print(f"::error::{PATH}: {name} anchor not found once -- dxmt moved, review this patch")
            return 1
    s = s.replace(ANCHOR_MODULE, MODULE + ANCHOR_MODULE)
    s = s.replace(ANCHOR_HOOK, ANCHOR_HOOK + HOOK)
    s = s.replace(ANCHOR_PROPS, PROPS)
    PATH.write_text(s)
    print("winemetal_unix.c: frame generation added (MADEIRA_FRAMEGEN=1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
