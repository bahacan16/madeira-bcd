/* Native shader-converter service: DXIL from the guest, metallib back.
 *
 * This is the unix half of one winemetal call. It exists because the converter
 * is a Mach-O dylib and the D3D12 runtime is an ARM64EC PE, so the bytecode has
 * to cross the same boundary every other Metal call already crosses. Riding
 * winemetal's proven unix-call path rather than inventing a second bridge keeps
 * one mechanism to reason about, and it is the bridge this DLL already uses for
 * every device call it makes.
 *
 * The conversion body is the one the M1 canary proved on macOS, on the VM and
 * on the A15. It is reproduced rather than shared because the canary is a test
 * that must stay free to check things a runtime should not do.
 *
 * Runs LOCALLY even when rendering is remote. Conversion is a pure byte
 * transform that never touches a Metal object, so there is no host handle to
 * dereference; what must follow the rendering backend is the TARGET, and that
 * arrives in the arguments rather than being read from the local device. */
#include <dlfcn.h>
#include "../../../../build/madeira_cfg.h"   /* ml1095: one config file */
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <Foundation/Foundation.h>

#include "madeira_ir_abi.h"

#define IR_PRIVATE_IMPLEMENTATION 0   /* the canary owns the one definition */
#include <metal_irconverter/metal_irconverter.h>

/* Resolved by name, so a converter that is present but incomplete is reported
 * as a missing symbol instead of crashing on a null call. */
#define IR_FUNC_LIST(X) \
    X(IRObjectCreateFromDXIL) \
    X(IRObjectDestroy) \
    X(IRObjectGetMetalIRShaderStage) \
    X(IRObjectGetMetalLibBinary) \
    X(IRObjectGetReflection) \
    X(IRCompilerCreate) \
    X(IRCompilerDestroy) \
    X(IRCompilerSetGlobalRootSignature) \
    X(IRCompilerSetEntryPointName) \
    X(IRCompilerSetMinimumDeploymentTarget) \
    X(IRCompilerSetMinimumGPUFamily) \
    X(IRCompilerSetCompatibilityFlags) \
    X(IRCompilerAllocCompileAndLink) \
    X(IRRootSignatureCreateFromDescriptor) \
    X(IRRootSignatureDestroy) \
    X(IRMetalLibBinaryCreate) \
    X(IRMetalLibBinaryDestroy) \
    X(IRMetalLibGetBytecodeSize) \
    X(IRMetalLibGetBytecode) \
    X(IRShaderReflectionCreate) \
    X(IRShaderReflectionDestroy) \
    X(IRShaderReflectionGetEntryPointFunctionName) \
    X(IRShaderReflectionCopyVertexInfo) \
    X(IRShaderReflectionCopyComputeInfo) \
    X(IRShaderReflectionReleaseComputeInfo) \
    X(IRShaderReflectionReleaseVertexInfo) \
    X(IRShaderReflectionGetResourceCount) \
    X(IRShaderReflectionGetResourceLocations) \
    X(IRCompilerEnableGeometryAndTessellationEmulation) \
    X(IRCompilerSetInputTopology) \
    X(IRCompilerSetStageInGenerationMode) \
    X(IRMetalLibSynthesizeStageInFunction) \
    X(IRShaderReflectionCopyGeometryInfo) \
    X(IRShaderReflectionReleaseGeometryInfo) \
    X(IRErrorGetCode) \
    X(IRErrorDestroy)

struct IRFns {
    void *handle;
    int   ready;
    int   status;          /* madeira_ir_status when not ready */
#define IR_DECL(n) decltype(&::n) n;
    IR_FUNC_LIST(IR_DECL)
#undef IR_DECL
};
static IRFns g_ir;
static pthread_once_t g_ir_once = PTHREAD_ONCE_INIT;

/* The dylib ships inside the app bundle, so the service finds it itself rather
 * than having the guest pass a path across. A path chosen by the guest would be
 * a guest-controlled dlopen, and the guest has no way to know where the bundle
 * landed anyway. */
static void ir_load_once(void) {
    memset(&g_ir, 0, sizeof g_ir);
    g_ir.status = MADEIRA_IR_NO_DYLIB;

#ifdef MADEIRA_IR_HOST_TEST
    /* Host-only: lets the offline round-trip test point at the converter in the
     * extracted package. Compiled out of the device build entirely, so the
     * shipping runtime has exactly one place it will load the converter from. */
    {
        const char *dev = getenv("MADEIRA_MSC_DYLIB");
        if (dev) g_ir.handle = dlopen(dev, RTLD_NOW | RTLD_LOCAL);
    }
    if (g_ir.handle) goto bind;
#endif
    @autoreleasepool {
        NSString *base = [[NSBundle mainBundle] bundlePath];
        NSString *p = [base stringByAppendingPathComponent:@"d3d12/libmetalirconverter.dylib"];
        g_ir.handle = dlopen([p UTF8String], RTLD_NOW | RTLD_LOCAL);
        if (!g_ir.handle) {
            /* Also try a plain name: a development build may have it on the
             * loader path rather than in the bundle. Named either way. */
            g_ir.handle = dlopen("libmetalirconverter.dylib", RTLD_NOW | RTLD_LOCAL);
        }
        if (!g_ir.handle) {
            fprintf(stderr, "[madeira-ir] converter not loadable at %s: %s\n",
                    [p UTF8String], dlerror());
            return;
        }
    }

#ifdef MADEIRA_IR_HOST_TEST
bind:
#endif
#define IR_BIND(n) \
    g_ir.n = (decltype(&::n))dlsym(g_ir.handle, #n); \
    if (!g_ir.n) { \
        fprintf(stderr, "[madeira-ir] converter is missing %s\n", #n); \
        g_ir.status = MADEIRA_IR_NO_SYMBOL; \
        return; \
    }
    IR_FUNC_LIST(IR_BIND)
#undef IR_BIND

    g_ir.ready = 1;
    g_ir.status = MADEIRA_IR_OK;
    fprintf(stderr, "[madeira-ir] converter ready (runtime DXIL conversion)\n");
}

static IRShaderVisibility map_visibility(uint32_t v) {
    switch (v) {
    case MADEIRA_IR_VIS_VERTEX:   return IRShaderVisibilityVertex;
    case MADEIRA_IR_VIS_HULL:     return IRShaderVisibilityHull;
    case MADEIRA_IR_VIS_DOMAIN:   return IRShaderVisibilityDomain;
    case MADEIRA_IR_VIS_GEOMETRY: return IRShaderVisibilityGeometry;
    case MADEIRA_IR_VIS_PIXEL:    return IRShaderVisibilityPixel;
    default:                      return IRShaderVisibilityAll;
    }
}

static IRDescriptorRangeType map_range(uint32_t t) {
    switch (t) {
    case MADEIRA_IR_RANGE_UAV:     return IRDescriptorRangeTypeUAV;
    case MADEIRA_IR_RANGE_CBV:     return IRDescriptorRangeTypeCBV;
    case MADEIRA_IR_RANGE_SAMPLER: return IRDescriptorRangeTypeSampler;
    default:                       return IRDescriptorRangeTypeSRV;
    }
}

/* ---------------------------------------------------------------------------
 * ml1008: the DXBC / shader-model-5.x backend.
 *
 * Apple's converter takes DXIL only. Its container parser accepts any DXBC
 * wrapper, but the compile step looks for a chunk with fourcc `DXIL` and a
 * program header of at least 0x20 bytes, and reports
 * IRErrorCodeUnrecognizedDXILHeader (code 14) when it finds none. RDR2 feeds
 * D3D12 shader-model-5.1 bytecode in a `SHEX` chunk, so all 189 of its shaders
 * were refused and nothing ever drew.
 *
 * SM 5.1 is a D3D12 shader model -- it was introduced for D3D12 and is what
 * `fxc /T cs_5_1` emits -- so a conforming runtime compiles DXBC for SM <= 5.1
 * and DXIL for SM >= 6.0. We only did the second half. These shaders go to the
 * in-tree DXBC compiler instead, which is already linked into this image.
 * ------------------------------------------------------------------------- */

/* Include the compiler's REAL header rather than restating its structs here.
 * MTL_SHADER_REFLECTION contains a union of tessellation / geometry / compute
 * reflection, and its size is not something to predict: two hand-written
 * guesses at it disagreed with the measured layout in opposite directions. The
 * header is already on this file's include path, and the compiler lives in the
 * SAME Mach-O image as this function, so no dlopen and no restated layout. */
#include "airconv_public.h"

enum mad_container { MAD_CONTAINER_BAD = 0, MAD_CONTAINER_DXIL, MAD_CONTAINER_SM5 };

/* Classify by the CHUNK DIRECTORY, never by the outer magic: DXIL uses the same
 * `DXBC` container, so the magic says nothing about which compiler can read it.
 * Sizes are validated first, so a truncated blob is reported as a bad container
 * rather than walking off the end of it. */
static enum mad_container mad_classify_container(const unsigned char *b, size_t len,
                                                 unsigned *sm_major, unsigned *sm_minor,
                                                 char *note, size_t note_cap)
{
    *sm_major = *sm_minor = 0;
    if (len < 0x20 || memcmp(b, "DXBC", 4)) {
        snprintf(note, note_cap, "not a DXBC container (%zu bytes)", len);
        return MAD_CONTAINER_BAD;
    }
    uint32_t total, count;
    memcpy(&total, b + 0x18, 4);
    memcpy(&count, b + 0x1c, 4);
    if (total != len) {
        snprintf(note, note_cap, "container says %u bytes, got %zu", total, len);
        return MAD_CONTAINER_BAD;
    }
    if (!count || count > 32 || 0x20 + 4 * (size_t)count > len) {
        snprintf(note, note_cap, "implausible chunk count %u", count);
        return MAD_CONTAINER_BAD;
    }
    int have_dxil = 0, have_sm5 = 0;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t off, csz;
        memcpy(&off, b + 0x20 + 4 * i, 4);
        if ((size_t)off + 8 > len) {
            snprintf(note, note_cap, "chunk %u offset %u out of range", i, off);
            return MAD_CONTAINER_BAD;
        }
        memcpy(&csz, b + off + 4, 4);
        if ((size_t)off + 8 + csz > len) {
            snprintf(note, note_cap, "chunk %u size %u overruns the container", i, csz);
            return MAD_CONTAINER_BAD;
        }
        if (!memcmp(b + off, "DXIL", 4)) have_dxil = 1;
        else if (!memcmp(b + off, "SHEX", 4) || !memcmp(b + off, "SHDR", 4)) {
            have_sm5 = 1;
            if (csz >= 8) {
                uint32_t ver;
                memcpy(&ver, b + off + 8, 4);
                *sm_major = (ver >> 4) & 0xf;
                *sm_minor = ver & 0xf;
            }
        }
    }
    /* A blob carrying both is ambiguous about which compiler owns it; say so
     * rather than silently preferring one. */
    if (have_dxil && have_sm5) {
        snprintf(note, note_cap, "container has both DXIL and SHEX chunks");
        return MAD_CONTAINER_BAD;
    }
    if (have_dxil) return MAD_CONTAINER_DXIL;
    if (have_sm5)  return MAD_CONTAINER_SM5;
    snprintf(note, note_cap, "no DXIL and no SHEX/SHDR chunk");
    return MAD_CONTAINER_BAD;
}

/* FNV-1a over the bytecode. The compiler NAMES the emitted Metal function from
 * the string we pass (it does not read an entry name out of the bytecode the
 * way Apple's converter does), so we choose a content-derived name: identical
 * bytecode always yields the same function name, and two different shaders
 * never collide on one. */
static void mad_air_entry_name(const unsigned char *b, size_t len, char *out, size_t cap)
{
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < len; i++) { h ^= b[i]; h *= 1099511628211ull; }
    snprintf(out, cap, "mdc_%016llx", (unsigned long long)h);
}

/* ml1011: resolving the input layout needs DXBCParser's signature reader, whose
 * header pulls in a Windows compatibility layer that redefines BOOL as int --
 * irreconcilable with Objective-C's BOOL in this translation unit. The resolver
 * therefore lives in madeira_sm5_ia.cpp (plain C++) and is reached by prototype. */
/* ml1149: madeira_ags.cpp (LLVM 15, plain C++ like the IA resolver). */
extern "C" int madeira_ags_rewrite(const void *bc, size_t len, void **out, size_t *out_len,
                                   char *note, size_t note_cap);

extern "C" int madeira_sm5_resolve_ia(const void *bc, size_t bclen,
                                      const struct madeira_ir_input_layout *L,
                                      struct SM50_IA_INPUT_ELEMENT *out, uint32_t out_cap,
                                      uint32_t *n_out, uint32_t *slot_mask_out,
                                      char *note, size_t note_cap);

/* ---------------------------------------------------------------------------
 * ml1020: a disk cache for the DXBC backend.
 *
 * Measured on rdr88: ~20 shaders/sec, 4,139 compiled in one run, and RDR2
 * parsed 15,589 root signatures -- minutes of pure LLVM per launch. Before the
 * SM5 backend existed every shader failed instantly (converter code 14), which
 * is why loading used to be fast AND black; the cost is the price of it working.
 * Nothing on this path cached, so every launch recompiled everything.
 *
 * The cache stores the REFLECTION as well as the metallib: the runtime needs the
 * bind indices, table size, threadgroup size, slot mask and the resolved range
 * records, and recovering those would mean re-running the compiler -- which is
 * the cost we are avoiding.
 *
 * KEY IDENTITY. Everything that can change the output goes into the hash, not
 * just the bytecode: the Metal version, the ABI/format revision, and -- for a
 * vertex shader -- the resolved input-layout elements, because the generated
 * vertex fetch is built from them. Getting this wrong would serve a shader
 * compiled for a different layout, which is far worse than recompiling.
 * ------------------------------------------------------------------------- */
#define MAD_SC_MAGIC   0x4353444du   /* "MDSC" */
#define MAD_SC_VERSION 3u   /* ml1083: tessellation fields appended; ml1146: v2 entries may hold range COUNTS without range data */

struct mad_sc_header {
    uint32_t magic, version;
    uint32_t backend, cb_bind, arg_bind, arg_qwords;
    uint32_t nranges, slot_mask, vs_input_count, reserved;
    uint32_t tg[3], pad;
    uint64_t metallib_len;
    char entry[MADEIRA_IR_ENTRY_MAX];
    /* ml1083: the second (hull) range set of a tessellation object function and
     * the tessellator facts, stored after the first range set. */
    uint32_t nranges2, cb_bind2, arg_bind2, arg_qwords2;
    uint32_t threads_per_patch, tess_out_prim, max_potential, reserved2;
};

static void mad_sc_hash_add(uint64_t *h, const void *p, size_t n)
{
    const unsigned char *b = (const unsigned char *)p;
    for (size_t i = 0; i < n; i++) { *h ^= b[i]; *h *= 1099511628211ull; }
}

/* Returns 0 if the cache directory is unavailable. */
static int mad_sc_path(uint64_t key, char *out, size_t cap)
{
    const char *docs = getenv( "MADEIRA_DOCS_DIR" );
    if (!docs || !*docs) return 0;
    if (snprintf(out, cap, "%s/shadercache", docs) >= (int)cap) return 0;
    mkdir(out, 0755);   /* harmless if it exists */
    if (snprintf(out, cap, "%s/shadercache/%016llx.mdsc", docs,
                 (unsigned long long)key) >= (int)cap) return 0;
    return 1;
}

static int mad_sc_load(uint64_t key, struct madeira_ir_convert_args *a,
                       struct madeira_ir_air_range *out_ranges)
{
    char path[1200];
    struct mad_sc_header h;
    int fd;
    if (!mad_sc_path(key, path, sizeof path)) return 0;
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    if (read(fd, &h, sizeof h) != (ssize_t)sizeof h ||
        h.magic != MAD_SC_MAGIC || h.version != MAD_SC_VERSION ||
        h.nranges > MADEIRA_IR_AIR_RANGE_MAX || !h.metallib_len) { close(fd); return 0; }

    a->ret_len = h.metallib_len;
    /* Sizing call: report the length and let the caller come back with a buffer,
     * exactly as the compile path does. */
    if (!a->out_buf || a->out_cap < h.metallib_len) { close(fd); a->ret_status = MADEIRA_IR_BUFFER_TOO_SMALL; return 2; }

    if (h.nranges) {
        size_t want = (size_t)h.nranges * sizeof *out_ranges;
        if (!out_ranges || h.nranges > a->air_range_cap ||
            read(fd, out_ranges, want) != (ssize_t)want) { close(fd); return 0; }
    }
    if (h.nranges2) {   /* ml1083 */
        struct madeira_ir_air_range *out2 = (struct madeira_ir_air_range *)(uintptr_t)a->out_air_ranges2;
        size_t want = (size_t)h.nranges2 * sizeof *out2;
        if (!out2 || h.nranges2 > a->air_range_cap2 ||
            read(fd, out2, want) != (ssize_t)want) { close(fd); return 0; }
    }
    if (read(fd, (void *)(uintptr_t)a->out_buf, (size_t)h.metallib_len) != (ssize_t)h.metallib_len) { close(fd); return 0; }
    close(fd);
    a->ret_air_nranges2 = h.nranges2; a->ret_cb_table_bind2 = h.cb_bind2;   /* ml1083 */
    a->ret_arg_table_bind2 = h.arg_bind2; a->ret_arg_qwords2 = h.arg_qwords2;
    a->ret_threads_per_patch = h.threads_per_patch; a->ret_tess_out_prim = h.tess_out_prim;
    a->ret_max_potential_factor = h.max_potential;

    a->ret_backend        = h.backend;
    a->ret_cb_table_bind  = h.cb_bind;
    a->ret_arg_table_bind = h.arg_bind;
    a->ret_arg_qwords     = h.arg_qwords;
    a->ret_air_nranges    = h.nranges;
    a->ret_air_slot_mask  = h.slot_mask;
    a->ret_vs_input_count = h.vs_input_count;
    a->ret_tg_size[0] = h.tg[0]; a->ret_tg_size[1] = h.tg[1]; a->ret_tg_size[2] = h.tg[2];
    if (a->out_entry) snprintf((char *)(uintptr_t)a->out_entry, MADEIRA_IR_ENTRY_MAX, "%s", h.entry);
    a->ret_status = MADEIRA_IR_OK;
    return 1;
}

/* ml1085: takes the compiled bytes directly, so the SIZING call can store the
 * entry. Before this the store happened only after the copy into the caller's
 * buffer, and the caller's protocol is size-then-fill: a cache miss therefore
 * compiled every shader TWICE (the sizing call threw its result away, the fill
 * call recompiled). Half of every cold-cache load was pure waste. */
static void mad_sc_store(uint64_t key, const struct madeira_ir_convert_args *a,
                         const struct madeira_ir_air_range *ranges, const char *entry,
                         const void *data, uint64_t len)
{
    char path[1200], tmp[1264];
    struct mad_sc_header h;
    int fd;
    if (!len || !data) return;
    /* ml1146: a conversion whose caller asked for no range list (a vertex stage
     * converted for a geometry pipeline) still COUNTS its ranges. Stored like
     * that, the header promised N ranges and none followed, so the next caller
     * that did want them read N ranges out of the metallib bytes: garbage
     * registers and spaces, and 4,766 draws skipped as "table not reported"
     * (ph-valley03). An entry is only stored when every list it counts is here. */
    if ((a->ret_air_nranges && !ranges) || (a->ret_air_nranges2 && !a->out_air_ranges2)) return;
    if (!mad_sc_path(key, path, sizeof path)) return;
    /* Write-then-rename so a crash mid-write can never leave a torn entry that
     * a later run would trust. */
    if (snprintf(tmp, sizeof tmp, "%s.tmp%u", path, (unsigned)getpid()) >= (int)sizeof tmp) return;
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return;

    memset(&h, 0, sizeof h);
    h.magic = MAD_SC_MAGIC; h.version = MAD_SC_VERSION;
    h.backend = a->ret_backend; h.cb_bind = a->ret_cb_table_bind;
    h.arg_bind = a->ret_arg_table_bind; h.arg_qwords = a->ret_arg_qwords;
    h.nranges = a->ret_air_nranges > MADEIRA_IR_AIR_RANGE_MAX ? 0 : a->ret_air_nranges;
    h.slot_mask = a->ret_air_slot_mask; h.vs_input_count = a->ret_vs_input_count;
    h.tg[0] = a->ret_tg_size[0]; h.tg[1] = a->ret_tg_size[1]; h.tg[2] = a->ret_tg_size[2];
    h.metallib_len = len;
    snprintf(h.entry, sizeof h.entry, "%s", entry ? entry : "");
    h.nranges2 = a->ret_air_nranges2 > MADEIRA_IR_AIR_RANGE_MAX ? 0 : a->ret_air_nranges2;   /* ml1083 */
    h.cb_bind2 = a->ret_cb_table_bind2; h.arg_bind2 = a->ret_arg_table_bind2; h.arg_qwords2 = a->ret_arg_qwords2;
    h.threads_per_patch = a->ret_threads_per_patch; h.tess_out_prim = a->ret_tess_out_prim;
    h.max_potential = a->ret_max_potential_factor;

    int ok = write(fd, &h, sizeof h) == (ssize_t)sizeof h;
    if (ok && h.nranges && ranges) {
        size_t n = (size_t)h.nranges * sizeof *ranges;
        ok = write(fd, ranges, n) == (ssize_t)n;
    }
    if (ok && h.nranges2 && a->out_air_ranges2) {   /* ml1083 */
        size_t n = (size_t)h.nranges2 * sizeof *ranges;
        ok = write(fd, (const void *)(uintptr_t)a->out_air_ranges2, n) == (ssize_t)n;
    }
    if (ok)
        ok = write(fd, data, (size_t)len) == (ssize_t)len;
    close(fd);
    if (ok) rename(tmp, path); else unlink(tmp);
}

/* ml1032/ml1033: VS/PS interpolant zero-fill, default ON, with a kill switch.
 *
 * ml1031 removed the 4 "Fragment input(s) mismatching" pipeline rejections, but
 * the run that shipped it went BACKWARDS against the run before it: draws/frame
 * collapsed 20 -> 1, blank frames started climbing again (they had been frozen,
 * meaning recent frames carried content), FPS 50-60 -> 1-2, and the game no
 * longer reached the interactive prompt it had reached without it.
 *
 * That is the signature of zero-filling an interpolant the vertex stage DOES
 * write: before ml1031 those pipelines were merely rejected and their draws
 * skipped; with it they run against corrupted inputs, which fails silently
 * instead of loudly. Until the matching is proven correct per shader, the safe
 * default is the behaviour that got further.
 *
 * Toggle with Documents/madeira-vsps-fill.txt = 1, no rebuild required. Both
 * settings can coexist in the shader cache, because the key only folds in the
 * vertex bytecode when this is enabled. */
static int mad_vsps_fill_enabled(void)
{
    static int cached = -1;

    if (cached < 0) {
        const char *docs = getenv("MADEIRA_DOCS_DIR");
        char path[1024];
        int fd = -1;

        /* ml1033: DEFAULT ON. Without this the G-buffer pipelines are rejected
         * outright and the main scene can never render, so defaulting it off
         * capped us permanently at "UI draws, scene black". The evidence that
         * once pointed the other way was a single run that also had a fully
         * invalidated shader cache and never reached the same stage -- not an
         * isolated comparison. ml1033 makes every zero-fill VISIBLE instead, so
         * a misfire is measured rather than guessed at. Set the file to 0 to
         * disable and A/B without a rebuild. */
        cached = (int)madeira_cfg_int("vsps-fill", 1);   /* ml1095: madeira.cfg vsps-fill = 0 disables */
        (void)docs; (void)path; (void)fd;
        fprintf(stderr, "[madeira-ir] ml1032 VS/PS interpolant zero-fill %s "
                        "(madeira.cfg vsps-fill)\n", cached ? "ENABLED" : "disabled");
    }
    return cached;
}

/* ml1083: one shader's declaration ranges, in the runtime's record format, with
 * the same refusals mad_airconv_convert has always applied (singletons in
 * register space 0 only). Returns a madeira_ir_status. */
static int mad_air_ranges(sm50_shader_t shader, struct madeira_ir_air_range *out, uint32_t cap,
                          uint32_t *n_out, char *note, size_t note_cap)
{
    struct MTL_SM50_RANGE_INFO ranges[MADEIRA_IR_AIR_RANGE_MAX];
    uint32_t n = SM50GetRangeInfo(shader, ranges, MADEIRA_IR_AIR_RANGE_MAX);
    *n_out = n;
    if (n > MADEIRA_IR_AIR_RANGE_MAX) {
        snprintf(note, note_cap, "%u declaration ranges, this build carries %u", n, MADEIRA_IR_AIR_RANGE_MAX);
        return MADEIRA_IR_TOO_MANY_RANGES;
    }
    for (uint32_t i = 0; i < n; i++) {
        static const char *cls[4] = { "b", "s", "t", "u" };
        uint32_t ty = (uint32_t)ranges[i].Type;
        const char *c = ty < 4 ? cls[ty] : "?";
        if (ranges[i].RangeSize != 1) {
            snprintf(note, note_cap, "%s%u space %u is a range of %u (only singletons are mapped)",
                     c, ranges[i].LowerBound, ranges[i].RegisterSpace, ranges[i].RangeSize);
            return MADEIRA_IR_UNSUPPORTED_RANGE;
        }
        if (ranges[i].RegisterSpace != 0) {
            snprintf(note, note_cap, "%s%u is in register space %u (only space 0 is mapped)",
                     c, ranges[i].LowerBound, ranges[i].RegisterSpace);
            return MADEIRA_IR_UNSUPPORTED_RANGE;
        }
        if (out && i < cap) {
            out[i].type        = ty;
            out[i].range_id    = ranges[i].RangeID;
            out[i].space       = ranges[i].RegisterSpace;
            out[i].lower_bound = ranges[i].LowerBound;
            out[i].size        = ranges[i].RangeSize;
            out[i].ptr_offset  = ranges[i].StructurePtrOffset;
            out[i].flags       = ranges[i].Flags;
            out[i].cb_table    = ranges[i].IsConstantBufferTable;
        }
    }
    if (out && n > cap) {
        snprintf(note, note_cap, "%u declaration ranges, caller supplied room for %u", n, cap);
        return MADEIRA_IR_TOO_MANY_RANGES;
    }
    return MADEIRA_IR_OK;
}

/* ml1083: TESSELLATION. See madeira_ir_abi.h for the two-request contract.
 * Ported from DXMT's D3D11 layer (d3d11_shader.cpp, ShaderVariantTessellation-
 * VertexHull / -Domain): the same compiler entry points, the same argument
 * chains, the same maximum-potential-factor handshake between the two
 * functions. Everything about the stage layout (table indices 27/28 for the
 * vertex shader inside the object function, 29/30 for the hull and for the
 * domain) is the compiler's convention, stated in its dxbc_converter_ts.cpp. */
static int mad_airconv_convert_tess(struct madeira_ir_convert_args *a,
                                    const unsigned char *bc, size_t bclen)
{
    const unsigned char *hs = (const unsigned char *)(uintptr_t)a->hs_bytecode;
    const unsigned char *ds = (const unsigned char *)(uintptr_t)a->ds_bytecode;
    size_t hslen = (size_t)a->hs_bytecode_len, dslen = (size_t)a->ds_bytecode_len;
    struct MTL_SHADER_REFLECTION refl_main, refl_hs, refl_ds;
    sm50_shader_t sh_main = NULL, sh_hs = NULL, sh_ds = NULL;
    sm50_error_t err = NULL;
    sm50_bitcode_t bits = NULL;
    struct madeira_ir_air_range *out_ranges  = (struct madeira_ir_air_range *)(uintptr_t)a->out_air_ranges;
    struct madeira_ir_air_range *out_ranges2 = (struct madeira_ir_air_range *)(uintptr_t)a->out_air_ranges2;
    struct SM50_IA_INPUT_ELEMENT ia_el[31];
    uint32_t ia_nel = 0, slot_mask = 0;
    const int object = a->tess_stage == 1;
    char name[MADEIRA_IR_ENTRY_MAX], hname[MADEIRA_IR_ENTRY_MAX];
    uint64_t sc_key = 0;
    int status;

    memset(&refl_main, 0, sizeof refl_main); memset(&refl_hs, 0, sizeof refl_hs); memset(&refl_ds, 0, sizeof refl_ds);
    a->ret_backend = MADEIRA_IR_BACKEND_AIRCONV;
    a->ret_cb_table_bind2 = a->ret_arg_table_bind2 = ~0u;
    a->ret_arg_qwords2 = a->ret_air_nranges2 = 0;
    a->ret_threads_per_patch = a->ret_tess_out_prim = a->ret_max_potential_factor = 0;

    if (a->tess_stage != 1 && a->tess_stage != 2) { snprintf(a->ret_note, sizeof a->ret_note, "tess_stage %u", a->tess_stage); return MADEIRA_IR_UNSUPPORTED; }
    if (!hs || !hslen || (object && (!ds || !dslen))) {
        snprintf(a->ret_note, sizeof a->ret_note, "tessellation request without the %s shader", (hs && hslen) ? "domain" : "hull");
        a->ret_status = MADEIRA_IR_BAD_DXIL; return MADEIRA_IR_BAD_DXIL;
    }
    if (object && a->tess_index_format > 2) { snprintf(a->ret_note, sizeof a->ret_note, "index format %u", a->tess_index_format); return MADEIRA_IR_UNSUPPORTED; }

    /* The vertex fetch of the object function is built from the resolved layout,
     * exactly as the plain vertex path does it. */
    if (object) {
        const struct madeira_ir_input_layout *L = (const struct madeira_ir_input_layout *)(uintptr_t)a->layout;
        if (L && L->n) {
            char why[128] = {0};
            if (!madeira_sm5_resolve_ia(bc, bclen, L, ia_el, 31, &ia_nel, &slot_mask, why, sizeof why)) {
                snprintf(a->ret_note, sizeof a->ret_note, "input layout: %s", why);
                a->ret_status = MADEIRA_IR_UNSUPPORTED_RANGE; return MADEIRA_IR_UNSUPPORTED_RANGE;
            }
        }
        a->ret_vs_input_count = ia_nel;
        a->ret_air_slot_mask = slot_mask;
    }

    mad_air_entry_name(hs, hslen, hname, sizeof hname);
    if (object) snprintf(name, sizeof name, "vshs_%u_%s", a->tess_index_format, hname + 4);
    else { char dname[MADEIRA_IR_ENTRY_MAX]; mad_air_entry_name(bc, bclen, dname, sizeof dname); snprintf(name, sizeof name, "ds_%s", dname + 4); }
    if (a->out_entry) snprintf((char *)(uintptr_t)a->out_entry, MADEIRA_IR_ENTRY_MAX, "%s", name);

    {   /* cache identity: every input that shapes the output */
        uint64_t key = 1469598103934665603ull;
        uint32_t mv = SM50_SHADER_METAL_320, ver = MAD_SC_VERSION, st[2] = { a->tess_stage, a->tess_index_format };
        const char *stamp = __DATE__ __TIME__;
        mad_sc_hash_add(&key, "tess", 4);
        mad_sc_hash_add(&key, bc, bclen);
        mad_sc_hash_add(&key, hs, hslen);
        if (object) mad_sc_hash_add(&key, ds, dslen);
        mad_sc_hash_add(&key, st, sizeof st);
        mad_sc_hash_add(&key, &mv, sizeof mv);
        mad_sc_hash_add(&key, &ver, sizeof ver);
        mad_sc_hash_add(&key, stamp, strlen(stamp));
        if (ia_nel) mad_sc_hash_add(&key, ia_el, (size_t)ia_nel * sizeof ia_el[0]);
        sc_key = key;
        int hit = mad_sc_load(key, a, out_ranges);
        if (hit) { a->ret_air_slot_mask = slot_mask; a->ret_vs_input_count = ia_nel; return (int)a->ret_status; }
    }

    if (SM50Initialize(bc, bclen, &sh_main, &refl_main, &err) != 0) {
        char msg[192] = {0};
        if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
        snprintf(a->ret_note, sizeof a->ret_note, "sm5 parse (%s): %s", object ? "vertex" : "domain", msg[0] ? msg : "(no message)");
        status = MADEIRA_IR_BAD_DXIL; goto done;
    }
    if (SM50Initialize(hs, hslen, &sh_hs, &refl_hs, &err) != 0) {
        char msg[192] = {0};
        if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
        snprintf(a->ret_note, sizeof a->ret_note, "sm5 parse (hull): %s", msg[0] ? msg : "(no message)");
        status = MADEIRA_IR_BAD_DXIL; goto done;
    }
    if (object) {
        if (SM50Initialize(ds, dslen, &sh_ds, &refl_ds, &err) != 0) {
            char msg[192] = {0};
            if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
            snprintf(a->ret_note, sizeof a->ret_note, "sm5 parse (domain): %s", msg[0] ? msg : "(no message)");
            status = MADEIRA_IR_BAD_DXIL; goto done;
        }
    } else refl_ds = refl_main;

    /* Only triangle-output tessellators become a mesh pipeline; DXMT refuses the
     * other two the same way (FinalizeTessellationRenderPipeline). */
    a->ret_tess_out_prim = (uint32_t)refl_hs.Tessellator.OutputPrimitive;
    if (refl_hs.Tessellator.OutputPrimitive != MTL_TESSELLATOR_OUTPUT_TRIANGLE_CW &&
        refl_hs.Tessellator.OutputPrimitive != MTL_TESSELLATOR_TRIANGLE_CCW) {
        snprintf(a->ret_note, sizeof a->ret_note, "tessellator output primitive %u (only triangles are emulated)",
                 (unsigned)refl_hs.Tessellator.OutputPrimitive);
        status = MADEIRA_IR_UNSUPPORTED; goto done;
    }
    a->ret_threads_per_patch = refl_hs.ThreadsPerPatch;
    a->ret_max_potential_factor = refl_ds.PostTessellator.MaxPotentialTessFactor;
    if (!a->ret_max_potential_factor) a->ret_max_potential_factor = 1;

    /* Tables. The main shader's ranges go to the first set; the object function
     * carries the hull's as a second set. The vertex shader's tables sit at
     * 27/28 inside the object function (setup_binding_table(..., 27, 28)), so
     * its reported 29/30 are re-homed here rather than at every draw. */
    status = mad_air_ranges(sh_main, out_ranges, a->air_range_cap, &a->ret_air_nranges, a->ret_note, sizeof a->ret_note);
    if (status != MADEIRA_IR_OK) goto done;
    a->ret_cb_table_bind  = refl_main.ConstanttBufferTableBindIndex;
    a->ret_arg_table_bind = refl_main.ArgumentBufferBindIndex;
    a->ret_arg_qwords     = refl_main.ArgumentTableQwords;
    if (object) {
        if (a->ret_cb_table_bind  == 29) a->ret_cb_table_bind  = 27;
        if (a->ret_arg_table_bind == 30) a->ret_arg_table_bind = 28;
        status = mad_air_ranges(sh_hs, out_ranges2, a->air_range_cap2, &a->ret_air_nranges2, a->ret_note, sizeof a->ret_note);
        if (status != MADEIRA_IR_OK) goto done;
        a->ret_cb_table_bind2  = refl_hs.ConstanttBufferTableBindIndex;
        a->ret_arg_table_bind2 = refl_hs.ArgumentBufferBindIndex;
        a->ret_arg_qwords2     = refl_hs.ArgumentTableQwords;
    }

    {
        struct SM50_SHADER_COMMON_DATA common;
        struct SM50_SHADER_PSO_TESSELLATOR_DATA tess;
        struct SM50_SHADER_IA_INPUT_LAYOUT_DATA ia;
        struct SM50_SHADER_GS_PASS_THROUGH_DATA gsp;
        int rc;
        memset(&common, 0, sizeof common); common.type = SM50_SHADER_COMMON; common.metal_version = SM50_SHADER_METAL_320;
        memset(&tess, 0, sizeof tess); tess.type = SM50_SHADER_PSO_TESSELLATOR; tess.next = &common;
        tess.max_potential_tess_factor = a->ret_max_potential_factor;
        if (object) {
            memset(&ia, 0, sizeof ia);
            ia.type = SM50_SHADER_IA_INPUT_LAYOUT; ia.next = &tess;
            ia.index_buffer_format = (enum SM50_INDEX_BUFFER_FORAMT)a->tess_index_format;
            ia.slot_mask = slot_mask; ia.num_elements = ia_nel; ia.elements = ia_el;
            rc = SM50CompileTessellationPipelineHull(sh_main, sh_hs, (struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *)&ia, name, &bits, &err);
        } else {
            /* No geometry shader: the pass-through record says "no render-target
             * or viewport array index" (both registers 255) and rasterisation on. */
            memset(&gsp, 0, sizeof gsp);
            gsp.type = SM50_SHADER_GS_PASS_THROUGH; gsp.next = &tess;
            gsp.DataEncoded = ~0u; gsp.RasterizationDisabled = false;
            rc = SM50CompileTessellationPipelineDomain(sh_hs, sh_main, (struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *)&gsp, name, &bits, &err);
        }
        if (rc != 0) {
            char msg[192] = {0};
            if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
            snprintf(a->ret_note, sizeof a->ret_note, "sm5 %s compile: %s", object ? "vertex-hull" : "domain", msg[0] ? msg : "(no message)");
            status = MADEIRA_IR_COMPILE_FAILED; goto done;
        }
    }
    {
        struct SM50_COMPILED_BITCODE out;
        memset(&out, 0, sizeof out);
        SM50GetCompiledBitcode(bits, &out);
        a->ret_len = out.Size;
        if (!out.Size || !out.Data) { snprintf(a->ret_note, sizeof a->ret_note, "sm5 compile produced no bytes"); status = MADEIRA_IR_NO_METALLIB; goto done; }
        mad_sc_store(sc_key, a, out_ranges, name, (const void *)(uintptr_t)out.Data, out.Size);   /* ml1085: on the sizing call too */
        if (!a->out_buf || a->out_cap < out.Size) { status = MADEIRA_IR_BUFFER_TOO_SMALL; goto done; }
        memcpy((void *)(uintptr_t)a->out_buf, (const void *)(uintptr_t)out.Data, (size_t)out.Size);
        status = MADEIRA_IR_OK;
        a->ret_status = MADEIRA_IR_OK;
    }
done:
    if (bits) SM50DestroyBitcode(bits);
    if (sh_main) SM50Destroy(sh_main);
    if (sh_hs) SM50Destroy(sh_hs);
    if (sh_ds) SM50Destroy(sh_ds);
    a->ret_status = (uint32_t)status;
    return status;
}

/* ml1147: GEOMETRY SHADERS. See madeira_ir_abi.h for the two-request contract.
 * Ported from DXMT's D3D11 layer (d3d11_shader.cpp): the same two compiler
 * entry points, the same argument chains (object: geometry record -> input
 * layout -> common; mesh: geometry record -> common). The compiler cannot
 * convert a geometry shader on its own ("Geometry shader cannot be
 * independently converted"), which is why every DXBC geometry-shader pipeline
 * lost its geometry stage before this. */
static int mad_airconv_convert_gs(struct madeira_ir_convert_args *a,
                                  const unsigned char *bc, size_t bclen)
{
    const unsigned char *other = (const unsigned char *)(uintptr_t)a->gs_bytecode;
    size_t olen = (size_t)a->gs_bytecode_len;
    struct MTL_SHADER_REFLECTION refl_main, refl_other;
    sm50_shader_t sh_main = NULL, sh_other = NULL;
    sm50_error_t err = NULL;
    sm50_bitcode_t bits = NULL;
    struct madeira_ir_air_range *out_ranges = (struct madeira_ir_air_range *)(uintptr_t)a->out_air_ranges;
    struct SM50_IA_INPUT_ELEMENT ia_el[31];
    uint32_t ia_nel = 0, slot_mask = 0;
    const int object = a->gs_stage == 1;
    char name[MADEIRA_IR_ENTRY_MAX], mname[MADEIRA_IR_ENTRY_MAX];
    uint64_t sc_key = 0;
    int status;

    memset(&refl_main, 0, sizeof refl_main); memset(&refl_other, 0, sizeof refl_other);
    a->ret_backend = MADEIRA_IR_BACKEND_AIRCONV;
    a->ret_cb_table_bind2 = a->ret_arg_table_bind2 = ~0u;
    a->ret_arg_qwords2 = a->ret_air_nranges2 = 0;
    a->ret_threads_per_patch = a->ret_tess_out_prim = a->ret_max_potential_factor = 0;

    if (a->gs_stage != 1 && a->gs_stage != 2) { snprintf(a->ret_note, sizeof a->ret_note, "gs_stage %u", a->gs_stage); return MADEIRA_IR_UNSUPPORTED; }
    if (!other || !olen) {
        snprintf(a->ret_note, sizeof a->ret_note, "geometry request without the %s shader", object ? "geometry" : "vertex");
        a->ret_status = MADEIRA_IR_BAD_DXIL; return MADEIRA_IR_BAD_DXIL;
    }
    if (object && a->tess_index_format > 2) { snprintf(a->ret_note, sizeof a->ret_note, "index format %u", a->tess_index_format); return MADEIRA_IR_UNSUPPORTED; }

    if (object) {   /* the object function fetches vertices itself, from the resolved layout */
        const struct madeira_ir_input_layout *L = (const struct madeira_ir_input_layout *)(uintptr_t)a->layout;
        if (L && L->n) {
            char why[128] = {0};
            if (!madeira_sm5_resolve_ia(bc, bclen, L, ia_el, 31, &ia_nel, &slot_mask, why, sizeof why)) {
                snprintf(a->ret_note, sizeof a->ret_note, "input layout: %s", why);
                a->ret_status = MADEIRA_IR_UNSUPPORTED_RANGE; return MADEIRA_IR_UNSUPPORTED_RANGE;
            }
        }
        a->ret_vs_input_count = ia_nel;
        a->ret_air_slot_mask = slot_mask;
    }

    mad_air_entry_name(bc, bclen, mname, sizeof mname);
    if (object) snprintf(name, sizeof name, "vsgs_%u%u_%s", a->tess_index_format, a->gs_strip ? 1u : 0u, mname + 4);
    else snprintf(name, sizeof name, "gs_%u_%s", a->gs_strip ? 1u : 0u, mname + 4);
    if (a->out_entry) snprintf((char *)(uintptr_t)a->out_entry, MADEIRA_IR_ENTRY_MAX, "%s", name);

    {   /* cache identity: every input that shapes the output */
        uint64_t key = 1469598103934665603ull;
        uint32_t mv = SM50_SHADER_METAL_320, ver = MAD_SC_VERSION, st[3] = { a->gs_stage, a->tess_index_format, a->gs_strip ? 1u : 0u };
        const char *stamp = __DATE__ __TIME__;
        mad_sc_hash_add(&key, "geom", 4);
        mad_sc_hash_add(&key, bc, bclen);
        mad_sc_hash_add(&key, other, olen);
        mad_sc_hash_add(&key, st, sizeof st);
        mad_sc_hash_add(&key, &mv, sizeof mv);
        mad_sc_hash_add(&key, &ver, sizeof ver);
        mad_sc_hash_add(&key, stamp, strlen(stamp));
        if (ia_nel) mad_sc_hash_add(&key, ia_el, (size_t)ia_nel * sizeof ia_el[0]);
        sc_key = key;
        int hit = mad_sc_load(key, a, out_ranges);
        if (hit) { a->ret_air_slot_mask = slot_mask; a->ret_vs_input_count = ia_nel; return (int)a->ret_status; }
    }

    if (SM50Initialize(bc, bclen, &sh_main, &refl_main, &err) != 0) {
        char msg[192] = {0};
        if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
        snprintf(a->ret_note, sizeof a->ret_note, "sm5 parse (%s): %s", object ? "vertex" : "geometry", msg[0] ? msg : "(no message)");
        status = MADEIRA_IR_BAD_DXIL; goto done;
    }
    if (SM50Initialize(other, olen, &sh_other, &refl_other, &err) != 0) {
        char msg[192] = {0};
        if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
        snprintf(a->ret_note, sizeof a->ret_note, "sm5 parse (%s): %s", object ? "geometry" : "vertex", msg[0] ? msg : "(no message)");
        status = MADEIRA_IR_BAD_DXIL; goto done;
    }

    /* Each function binds its OWN shader's tables at the indices the compiler
     * reports (29/30 for both, per DXMT's pipeline: object 16/21/29/30, mesh 29/30). */
    status = mad_air_ranges(sh_main, out_ranges, a->air_range_cap, &a->ret_air_nranges, a->ret_note, sizeof a->ret_note);
    if (status != MADEIRA_IR_OK) goto done;
    a->ret_cb_table_bind  = refl_main.ConstanttBufferTableBindIndex;
    a->ret_arg_table_bind = refl_main.ArgumentBufferBindIndex;
    a->ret_arg_qwords     = refl_main.ArgumentTableQwords;

    {
        struct SM50_SHADER_COMMON_DATA common;
        struct SM50_SHADER_IA_INPUT_LAYOUT_DATA ia;
        struct SM50_SHADER_PSO_GEOMETRY_SHADER_DATA geom;
        int rc;
        memset(&common, 0, sizeof common); common.type = SM50_SHADER_COMMON; common.metal_version = SM50_SHADER_METAL_320;
        memset(&geom, 0, sizeof geom); geom.type = SM50_SHADER_PSO_GEOMETRY_SHADER; geom.strip_topology = a->gs_strip ? true : false;
        if (object) {
            memset(&ia, 0, sizeof ia);
            ia.type = SM50_SHADER_IA_INPUT_LAYOUT; ia.next = &common;
            ia.index_buffer_format = (enum SM50_INDEX_BUFFER_FORAMT)a->tess_index_format;
            ia.slot_mask = slot_mask; ia.num_elements = ia_nel; ia.elements = ia_el;
            geom.next = &ia;
            rc = SM50CompileGeometryPipelineVertex(sh_main, sh_other, (struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *)&geom, name, &bits, &err);
        } else {
            geom.next = &common;
            rc = SM50CompileGeometryPipelineGeometry(sh_other, sh_main, (struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *)&geom, name, &bits, &err);
        }
        if (rc != 0) {
            char msg[192] = {0};
            if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); err = NULL; }
            snprintf(a->ret_note, sizeof a->ret_note, "sm5 %s compile: %s", object ? "vertex-for-geometry" : "geometry", msg[0] ? msg : "(no message)");
            status = MADEIRA_IR_COMPILE_FAILED; goto done;
        }
    }
    {
        struct SM50_COMPILED_BITCODE out;
        memset(&out, 0, sizeof out);
        SM50GetCompiledBitcode(bits, &out);
        a->ret_len = out.Size;
        if (!out.Size || !out.Data) { snprintf(a->ret_note, sizeof a->ret_note, "sm5 compile produced no bytes"); status = MADEIRA_IR_NO_METALLIB; goto done; }
        mad_sc_store(sc_key, a, out_ranges, name, (const void *)(uintptr_t)out.Data, out.Size);
        if (!a->out_buf || a->out_cap < out.Size) { status = MADEIRA_IR_BUFFER_TOO_SMALL; goto done; }
        memcpy((void *)(uintptr_t)a->out_buf, (const void *)(uintptr_t)out.Data, (size_t)out.Size);
        status = MADEIRA_IR_OK;
        a->ret_status = MADEIRA_IR_OK;
    }
done:
    if (bits) SM50DestroyBitcode(bits);
    if (sh_main) SM50Destroy(sh_main);
    if (sh_other) SM50Destroy(sh_other);
    a->ret_status = (uint32_t)status;
    return status;
}

static int mad_airconv_convert(struct madeira_ir_convert_args *a,
                               const unsigned char *bc, size_t bclen)
{
    if (a->tess_stage) return mad_airconv_convert_tess(a, bc, bclen);   /* ml1083 */
    if (a->gs_stage) return mad_airconv_convert_gs(a, bc, bclen);       /* ml1147 */
    struct MTL_SHADER_REFLECTION refl;
    sm50_shader_t shader = NULL;
    sm50_error_t err = NULL;
    sm50_bitcode_t bits = NULL;
    struct MTL_SM50_RANGE_INFO ranges[MADEIRA_IR_AIR_RANGE_MAX];
    struct madeira_ir_air_range *out_ranges =
        (struct madeira_ir_air_range *)(uintptr_t)a->out_air_ranges;
    char name[MADEIRA_IR_ENTRY_MAX];
    int status;
    uint64_t sc_key = 0;   /* ml1020 */

    memset(&refl, 0, sizeof refl);
    a->ret_backend = MADEIRA_IR_BACKEND_AIRCONV;

    if (SM50Initialize(bc, bclen, &shader, &refl, &err) != 0) {
        char msg[192] = {0};
        if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); }
        snprintf(a->ret_note, sizeof a->ret_note, "sm5 parse: %s", msg[0] ? msg : "(no message)");
        return MADEIRA_IR_BAD_DXIL;
    }

    a->ret_cb_table_bind  = refl.ConstanttBufferTableBindIndex;   /* the header's spelling */
    a->ret_arg_table_bind = refl.ArgumentBufferBindIndex;
    a->ret_arg_qwords     = refl.ArgumentTableQwords;
    a->ret_tg_size[0] = refl.ThreadgroupSize[0];
    a->ret_tg_size[1] = refl.ThreadgroupSize[1];
    a->ret_tg_size[2] = refl.ThreadgroupSize[2];

    uint32_t n = SM50GetRangeInfo(shader, ranges, MADEIRA_IR_AIR_RANGE_MAX);
    a->ret_air_nranges = n;
    if (n > MADEIRA_IR_AIR_RANGE_MAX) {
        snprintf(a->ret_note, sizeof a->ret_note,
                 "%u declaration ranges, this build carries %u", n, MADEIRA_IR_AIR_RANGE_MAX);
        status = MADEIRA_IR_TOO_MANY_RANGES;
        goto done;
    }

    /* Refuse what the compiler would silently get wrong. Its resource-lookup
     * callbacks take the dynamic index and discard it ("ignore index in
     * SM 5.0"), so a multi-element or unbounded range collapses to its first
     * descriptor -- wrong data, no diagnostic. A register space we do not map
     * would likewise bind whatever sits at the same register in space 0.
     * Named refusal, never a silent collapse. */
    for (uint32_t i = 0; i < n; i++) {
        static const char *cls[4] = { "b", "s", "t", "u" };
        uint32_t ty = (uint32_t)ranges[i].Type;
        const char *c = ty < 4 ? cls[ty] : "?";
        if (ranges[i].RangeSize != 1) {
            snprintf(a->ret_note, sizeof a->ret_note,
                     "%s%u space %u is a range of %u (only singletons are mapped)",
                     c, ranges[i].LowerBound, ranges[i].RegisterSpace, ranges[i].RangeSize);
            status = MADEIRA_IR_UNSUPPORTED_RANGE;
            goto done;
        }
        if (ranges[i].RegisterSpace != 0) {
            snprintf(a->ret_note, sizeof a->ret_note,
                     "%s%u is in register space %u (only space 0 is mapped)",
                     c, ranges[i].LowerBound, ranges[i].RegisterSpace);
            status = MADEIRA_IR_UNSUPPORTED_RANGE;
            goto done;
        }
        if (out_ranges && i < a->air_range_cap) {
            out_ranges[i].type        = (uint32_t)ranges[i].Type;
            out_ranges[i].range_id    = ranges[i].RangeID;
            out_ranges[i].space       = ranges[i].RegisterSpace;
            out_ranges[i].lower_bound = ranges[i].LowerBound;
            out_ranges[i].size        = ranges[i].RangeSize;
            out_ranges[i].ptr_offset  = ranges[i].StructurePtrOffset;
            out_ranges[i].flags       = ranges[i].Flags;
            out_ranges[i].cb_table    = ranges[i].IsConstantBufferTable;
        }
    }
    if (out_ranges && n > a->air_range_cap) {
        snprintf(a->ret_note, sizeof a->ret_note,
                 "%u declaration ranges, caller supplied room for %u", n, a->air_range_cap);
        status = MADEIRA_IR_TOO_MANY_RANGES;
        goto done;
    }

    mad_air_entry_name(bc, bclen, name, sizeof name);
    if (a->out_entry)
        snprintf((char *)(uintptr_t)a->out_entry, MADEIRA_IR_ENTRY_MAX, "%s", name);
    /* ml1106: madeira.cfg dump-shaders = mdc_x,mdc_y  -> Documents/capture/shader_<name>.dxbc,
     * written BEFORE the cache lookup so a cached shader is dumped too. */
    {
        static char list[512]; static int loaded;
        if (!loaded) { loaded = 1; if (!madeira_cfg_get("dump-shaders", list, sizeof list)) list[0] = 0; }
        if (list[0] && strstr(list, name)) {
            const char *docs = getenv("MADEIRA_DOCS_DIR");
            char path[1200];
            if (docs && *docs) {
                snprintf(path, sizeof path, "%s/capture", docs); mkdir(path, 0755);
                snprintf(path, sizeof path, "%s/capture/shader_%s.dxbc", docs, name);
                if (access(path, F_OK) != 0) {
                    FILE *f = fopen(path, "wb");
                    if (f) { fwrite(bc, 1, bclen, f); fclose(f); dprintf(2, "[madeira-ir] ml1106 dumped %s (%zu bytes) to capture/\n", name, bclen); }
                }
            }
        }
    }

    {
        struct SM50_SHADER_COMMON_DATA common;
        struct SM50_SHADER_IA_INPUT_LAYOUT_DATA ia;
        struct SM50_IA_INPUT_ELEMENT ia_el[31];
        struct SM50_SHADER_PSO_PIXEL_SHADER_DATA ps;   /* ml1023 */
        struct SM50_SHADER_PSO_VERTEX_INTERFACE_DATA vsi; /* ml1031 */
        void *chain;
        memset(&common, 0, sizeof common);
        common.type = SM50_SHADER_COMMON;
        common.metal_version = SM50_SHADER_METAL_320;
        chain = &common;

        /* ml1023: the pixel stage must be compiled against its PSO facts.
         * Without this the backend never emits the second output that dual-source
         * blending requires, and Metal rejects the pipeline with "Fragment shader
         * does not write to render target color(0), index(1)". DXMT's D3D11 path
         * passes this for every pixel shader; we passed nothing. */
        if (a->ps_valid) {
            memset(&ps, 0, sizeof ps);
            ps.type = SM50_SHADER_PSO_PIXEL_SHADER;
            ps.next = &common;
            ps.sample_mask = a->ps_sample_mask;
            ps.dual_source_blending = (a->ps_flags & MADEIRA_IR_PS_DUAL_SOURCE_BLEND) ? true : false;
            ps.disable_depth_output = (a->ps_flags & MADEIRA_IR_PS_DISABLE_DEPTH) ? true : false;
            ps.unorm_output_reg_mask = a->ps_unorm_output_mask;
            chain = &ps;
        }

        /* ml1031: name the vertex stage this pixel shader is paired with, so the
         * backend can zero-fill any interpolant that stage never writes instead
         * of declaring a stage_in entry Metal will reject the pipeline over. */
        if (a->vs_bytecode && a->vs_bytecode_len && mad_vsps_fill_enabled()) {
            memset(&vsi, 0, sizeof vsi);
            vsi.type = SM50_SHADER_PSO_VERTEX_INTERFACE;
            vsi.next = chain;
            vsi.vertex_bytecode = (const void *)(uintptr_t)a->vs_bytecode;
            vsi.vertex_bytecode_size = (uint32_t)a->vs_bytecode_len;
            chain = &vsi;
        }

        /* ml1011: vertex stage with an input layout -> explicit vertex fetch. */
        const struct madeira_ir_input_layout *L =
            (const struct madeira_ir_input_layout *)(uintptr_t)a->layout;
        uint32_t ia_nel = 0;   /* ml1020: part of the cache identity */
        if (L && L->n) {
            uint32_t nel = 0, slot_mask = 0;
            char why[128] = {0};
            if (!madeira_sm5_resolve_ia(bc, bclen, L, ia_el, 31, &nel, &slot_mask, why, sizeof why)) {
                snprintf(a->ret_note, sizeof a->ret_note, "input layout: %s", why);
                status = MADEIRA_IR_UNSUPPORTED_RANGE;
                goto done;
            }
            ia_nel = nel;
            memset(&ia, 0, sizeof ia);
            ia.type = SM50_SHADER_IA_INPUT_LAYOUT;
            ia.next = chain;   /* ml1023: preserve anything already chained */
            ia.slot_mask = slot_mask;
            ia.num_elements = nel;
            ia.elements = ia_el;
            chain = &ia;
            a->ret_vs_input_count = nel;
            a->ret_air_slot_mask = slot_mask;   /* ml1011 */
        }

        /* ml1020: the cache key covers the bytecode, the Metal version, the format
         * revision AND the resolved input-layout elements -- the generated vertex
         * fetch is built from those, so a layout-blind key would serve a shader
         * compiled for a different layout. */
        {
            uint64_t key = 1469598103934665603ull;
            uint32_t mv = SM50_SHADER_METAL_320, ver = MAD_SC_VERSION;
            mad_sc_hash_add(&key, bc, bclen);
            mad_sc_hash_add(&key, &mv, sizeof mv);
            mad_sc_hash_add(&key, &ver, sizeof ver);
            /* ml1020: bind the key to THIS BUILD. A cache entry produced by an
             * older compiler is not safe to reuse -- the airconv changes in this
             * session alone would have invalidated it -- and a silently stale
             * shader is far worse than recompiling. Rebuilding the archive
             * changes this stamp and orphans the old entries. */
            { const char *stamp = __DATE__ __TIME__;
              mad_sc_hash_add(&key, stamp, strlen(stamp)); }
            if (ia_nel) mad_sc_hash_add(&key, ia_el, (size_t)ia_nel * sizeof ia_el[0]);
            /* ml1031: the emitted pixel shader now depends on WHICH vertex stage
             * it was compiled against -- interpolants the vertex stage does not
             * write are zero-filled rather than declared. A key blind to that
             * would serve a shader built for a different vertex stage. */
            if (a->vs_bytecode && a->vs_bytecode_len && mad_vsps_fill_enabled())
                mad_sc_hash_add(&key, (const void *)(uintptr_t)a->vs_bytecode,
                                (size_t)a->vs_bytecode_len);
            /* ml1023: these change the emitted pixel shader, so they are part of
             * its identity -- a cache keyed without them would serve a shader
             * compiled for different blending or output clamping. */
            if (a->ps_valid) {
                uint32_t pv[4] = { a->ps_sample_mask, a->ps_flags, a->ps_unorm_output_mask, 1u };
                mad_sc_hash_add(&key, pv, sizeof pv);
            }
            sc_key = key;

            int hit = mad_sc_load(key, a, out_ranges);
            if (hit) {
                static unsigned hits;
                if (hits++ < 4)
                    dprintf(2, "[madeira-ir] ml1020 shader cache HIT (%s)\n",
                            hit == 2 ? "sizing call" : "loaded");
                status = (int)a->ret_status;
                goto done;
            }
        }

        if (SM50Compile(shader, (struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *)chain,
                        name, &bits, &err) != 0) {
            char msg[192] = {0};
            if (err) { SM50GetErrorMessage(err, msg, sizeof msg); SM50FreeError(err); }
            snprintf(a->ret_note, sizeof a->ret_note, "sm5 compile: %s", msg[0] ? msg : "(no message)");
            status = MADEIRA_IR_COMPILE_FAILED;
            goto done;
        }
    }
    {
        struct SM50_COMPILED_BITCODE out;
        memset(&out, 0, sizeof out);
        SM50GetCompiledBitcode(bits, &out);
        a->ret_len = out.Size;
        if (!out.Size || !out.Data) {
            snprintf(a->ret_note, sizeof a->ret_note, "sm5 compile produced no bytes");
            status = MADEIRA_IR_NO_METALLIB;
            goto done;
        }
        /* ml1020: the reflection fields are all filled by now, so the entry is
         * complete whether or not the caller brought a buffer this time.
         * ml1085: store BEFORE the size check -- the sizing call is the one
         * that compiles, and the fill call then loads what it stored. */
        mad_sc_store(sc_key, a, out_ranges, name, (const void *)(uintptr_t)out.Data, out.Size);
        /* Same size-then-fill protocol as the DXIL path: a first call with no
         * buffer reports the size, the second copies. */
        if (!a->out_buf || a->out_cap < out.Size) { status = MADEIRA_IR_BUFFER_TOO_SMALL; goto done; }
        memcpy((void *)(uintptr_t)a->out_buf, (const void *)(uintptr_t)out.Data, (size_t)out.Size);
        status = MADEIRA_IR_OK;
        a->ret_status = MADEIRA_IR_OK;
    }

done:
    if (bits) SM50DestroyBitcode(bits);
    if (shader) SM50Destroy(shader);
    a->ret_status = (uint32_t)status;
    return status;
}

extern "C" int madeira_ir_convert_impl(struct madeira_ir_convert_args *a) {
    pthread_once(&g_ir_once, ir_load_once);
    if (!a) return MADEIRA_IR_UNSUPPORTED;

    a->ret_len = 0;
    a->ret_stage = 0;
    a->ret_error_code = 0;
    if (a->out_entry) *(char *)(uintptr_t)a->out_entry = '\0';

    a->ret_backend = MADEIRA_IR_BACKEND_MSC;
    a->ret_air_nranges = 0;
    a->ret_cb_table_bind = a->ret_arg_table_bind = ~0u;
    a->ret_arg_qwords = 0;
    a->ret_note[0] = '\0';
    a->ret_air_nranges2 = a->ret_arg_qwords2 = 0;   /* ml1083 */
    a->ret_cb_table_bind2 = a->ret_arg_table_bind2 = ~0u;
    a->ret_threads_per_patch = a->ret_tess_out_prim = a->ret_max_potential_factor = 0;

    /* ml1008: pick the compiler from the container, before the DXIL converter's
     * readiness is consulted -- an SM5 shader must still compile on a device
     * where Apple's dylib is missing entirely. */
    {
        const unsigned char *bc = (const unsigned char *)(uintptr_t)a->dxil;
        size_t bclen = (size_t)a->dxil_len;
        unsigned smj = 0, smn = 0;
        char why[96] = {0};
        enum mad_container kind = bc && bclen
            ? mad_classify_container(bc, bclen, &smj, &smn, why, sizeof why)
            : MAD_CONTAINER_BAD;
        if (kind == MAD_CONTAINER_SM5) {
            static int announced;
            if (!announced) {
                announced = 1;
                dprintf(2, "[madeira-ir] ml1008 shader-model-%u.%u DXBC -> in-tree AIR compiler "
                           "(Apple's converter takes DXIL only)\n", smj, smn);
            }
            return mad_airconv_convert(a, bc, bclen);
        }
        if (kind == MAD_CONTAINER_BAD) {
            snprintf(a->ret_note, sizeof a->ret_note, "%s", why);
            a->ret_status = MADEIRA_IR_BAD_CONTAINER;
            return MADEIRA_IR_BAD_CONTAINER;
        }
    }

    if (!g_ir.ready) { a->ret_status = (uint32_t)g_ir.status; return g_ir.status; }

    const struct madeira_ir_root_param *ps =
        (const struct madeira_ir_root_param *)(uintptr_t)a->params;
    const struct madeira_ir_root_range *rs =
        (const struct madeira_ir_root_range *)(uintptr_t)a->ranges;
    const uint64_t np = a->num_params;

    /* Built here rather than by the caller because these are the converter's
     * own types; the guest side speaks only the fixed-width ABI. */
    IRRootParameter1 *irp = NULL;
    IRDescriptorRange1 *irr = NULL;
    IRStaticSamplerDescriptor *irs = NULL;
    IRRootSignature *rsig = NULL;
    IRError *err = NULL;
    IRObject *input = NULL, *output = NULL;
    IRCompiler *compiler = NULL;
    IRMetalLibBinary *lib = NULL;
    IRShaderReflection *refl = NULL;
    void *ags_buf = NULL;   /* ml1149: rewritten container, borrowed by `input` */
    int status = MADEIRA_IR_COMPILE_FAILED;
    const char *entry = (const char *)(uintptr_t)a->entry_point;
    IRShaderStage stage = IRShaderStageInvalid;
    size_t need = 0;
    const char *nm = NULL;
    IRVersionedRootSignatureDescriptor desc;

    if (np) {
        irp = (IRRootParameter1 *)calloc((size_t)np, sizeof *irp);
        if (!irp) { status = MADEIRA_IR_NO_MEMORY; goto done; }
    }
    if (a->num_ranges) {
        irr = (IRDescriptorRange1 *)calloc((size_t)a->num_ranges, sizeof *irr);
        if (!irr) { status = MADEIRA_IR_NO_MEMORY; goto done; }
        for (uint64_t i = 0; i < a->num_ranges; i++) {
            irr[i].RangeType = map_range(rs[i].range_type);
            irr[i].NumDescriptors = rs[i].num_descriptors;
            irr[i].BaseShaderRegister = rs[i].base_register;
            irr[i].RegisterSpace = rs[i].register_space;
            irr[i].OffsetInDescriptorsFromTableStart = rs[i].table_offset;
            irr[i].Flags = IRDescriptorRangeFlagNone;
        }
    }

    for (uint64_t i = 0; i < np; i++) {
        irp[i].ShaderVisibility = map_visibility(ps[i].visibility);
        switch (ps[i].type) {
        case MADEIRA_IR_PARAM_CONSTANTS:
            irp[i].ParameterType = IRRootParameterType32BitConstants;
            irp[i].Constants.ShaderRegister = ps[i].shader_register;
            irp[i].Constants.RegisterSpace = ps[i].register_space;
            irp[i].Constants.Num32BitValues = ps[i].num_constants;
            break;
        case MADEIRA_IR_PARAM_CBV:
        case MADEIRA_IR_PARAM_SRV:
        case MADEIRA_IR_PARAM_UAV:
            irp[i].ParameterType = ps[i].type == MADEIRA_IR_PARAM_CBV ? IRRootParameterTypeCBV
                                 : ps[i].type == MADEIRA_IR_PARAM_SRV ? IRRootParameterTypeSRV
                                                                      : IRRootParameterTypeUAV;
            irp[i].Descriptor.ShaderRegister = ps[i].shader_register;
            irp[i].Descriptor.RegisterSpace = ps[i].register_space;
            irp[i].Descriptor.Flags = IRRootDescriptorFlagNone;
            break;
        case MADEIRA_IR_PARAM_TABLE:
            if (!irr || ps[i].first_range + ps[i].num_ranges > a->num_ranges) {
                status = MADEIRA_IR_UNSUPPORTED; goto done;
            }
            irp[i].ParameterType = IRRootParameterTypeDescriptorTable;
            irp[i].DescriptorTable.NumDescriptorRanges = ps[i].num_ranges;
            irp[i].DescriptorTable.pDescriptorRanges = irr + ps[i].first_range;
            break;
        default:
            status = MADEIRA_IR_UNSUPPORTED; goto done;
        }
    }

    if (a->num_samplers) {
        const struct madeira_ir_static_sampler *ss = (const struct madeira_ir_static_sampler *)(uintptr_t)a->samplers;
        irs = (IRStaticSamplerDescriptor *)calloc(a->num_samplers, sizeof *irs);
        if (!irs || !ss) { status = MADEIRA_IR_NO_MEMORY; goto done; }
        for (uint32_t i = 0; i < a->num_samplers; i++) {
            irs[i].Filter = (IRFilter)ss[i].filter;
            irs[i].AddressU = (IRTextureAddressMode)ss[i].address_u;
            irs[i].AddressV = (IRTextureAddressMode)ss[i].address_v;
            irs[i].AddressW = (IRTextureAddressMode)ss[i].address_w;
            irs[i].MipLODBias = ss[i].mip_lod_bias;
            irs[i].MaxAnisotropy = ss[i].max_anisotropy;
            irs[i].ComparisonFunc = (IRComparisonFunction)ss[i].comparison;
            irs[i].BorderColor = (IRStaticBorderColor)ss[i].border_color;
            irs[i].MinLOD = ss[i].min_lod;
            irs[i].MaxLOD = ss[i].max_lod;
            irs[i].ShaderRegister = ss[i].shader_register;
            irs[i].RegisterSpace = ss[i].register_space;
            irs[i].ShaderVisibility = map_visibility(ss[i].visibility);
        }
    }
    memset(&desc, 0, sizeof desc);
    desc.version = IRRootSignatureVersion_1_1;
    desc.desc_1_1.NumParameters = (uint32_t)np;
    desc.desc_1_1.pParameters = irp;
    desc.desc_1_1.NumStaticSamplers = a->num_samplers;
    desc.desc_1_1.pStaticSamplers = irs;
    desc.desc_1_1.Flags = IRRootSignatureFlagNone;

    rsig = g_ir.IRRootSignatureCreateFromDescriptor(&desc, &err);
    if (!rsig) {
        a->ret_error_code = err ? g_ir.IRErrorGetCode(err) : 0;
        status = MADEIRA_IR_BAD_ROOTSIG;
        goto done;
    }

    /* ml1149: AMD AGS 64-bit atomics (magic UAV in space 0x7FFF0ADE) become
     * native SM6.6 64-bit atomics before the converter sees the module; it
     * refuses the magic space outright. madeira.cfg ags-rewrite = 0 turns it off. */
    {
        static int enabled = -1;
        const uint8_t *src = (const uint8_t *)(uintptr_t)a->dxil;
        size_t src_len = (size_t)a->dxil_len, ags_len = 0;
        char note[192];
        if (enabled < 0) {
            char v[16];
            enabled = !(madeira_cfg_get("ags-rewrite", v, sizeof v) && v[0] == '0');
        }
        int rc = enabled ? madeira_ags_rewrite(src, src_len, &ags_buf, &ags_len, note, sizeof note) : 0;
        if (rc != 0 && a->out_buf) {   /* the sizing pass converts too; say it once */
            char nm[MADEIRA_IR_ENTRY_MAX];
            mad_air_entry_name(src, src_len, nm, sizeof nm);
            dprintf(2, "[madeira-ir] ml1149 AGS %s: %s%s\n", nm + 4, note,
                    rc < 0 ? " -- converting the original, which the converter will refuse" : "");
        }
        input = rc == 1 ? g_ir.IRObjectCreateFromDXIL((const uint8_t *)ags_buf, ags_len, IRBytecodeOwnershipNone)
                        : g_ir.IRObjectCreateFromDXIL(src, src_len, IRBytecodeOwnershipNone);
    }
    if (!input) { status = MADEIRA_IR_BAD_DXIL; goto done; }

    compiler = g_ir.IRCompilerCreate();
    if (!compiler) { status = MADEIRA_IR_NO_MEMORY; goto done; }
    g_ir.IRCompilerSetGlobalRootSignature(compiler, rsig);
    /* The name is OPTIONAL, and D3D12 never supplies one: a shader bytecode
     * blob is already compiled for a single entry point. Passing NULL lets the
     * converter use the one that is actually in the bytecode instead of the
     * runtime asserting a name it cannot know. Setting it to NULL explicitly
     * crashes the converter, so the call is skipped entirely. */
    if (entry && entry[0]) g_ir.IRCompilerSetEntryPointName(compiler, entry);
    g_ir.IRCompilerSetMinimumDeploymentTarget(
        compiler,
        a->target_os == MADEIRA_IR_OS_MACOS ? IROperatingSystem_macOS : IROperatingSystem_iOS,
        (const char *)(uintptr_t)a->os_version);
    g_ir.IRCompilerSetMinimumGPUFamily(compiler, (IRGPUFamily)a->gpu_family);
    /* ml932: "HLSL shaders may legally treat textures as texture arrays and
     * vice-versa" (converter manual, Texture arrays). UE binds one resource
     * as a Texture2DArray slice to one kernel and as a Texture2D to the next
     * (TSR history: update declares 2darray, resolve declares 2d); Metal
     * returns zeros on the mismatch. With this flag every 1D/2D/cube texture
     * is an array in the converted shader, and the runtime allocates and
     * views every such texture as an array to match. */
    g_ir.IRCompilerSetCompatibilityFlags(compiler, IRCompatibilityFlagForceTextureArray);
    a->ret_len2 = 0; a->ret_vs_output_size = 0; a->ret_gs_max_prims = 0; a->ret_gs_payload = 0; a->ret_gs_passthrough = 0;
    if (a->gs_emulation) {   /* ml927 */
        IRInputTopology topo = IRInputTopologyTriangle;
        if (a->input_topology == 1) topo = IRInputTopologyPoint;
        else if (a->input_topology == 2) topo = IRInputTopologyLine;
        g_ir.IRCompilerEnableGeometryAndTessellationEmulation(compiler, true);
        g_ir.IRCompilerSetInputTopology(compiler, topo);
        if (a->layout) g_ir.IRCompilerSetStageInGenerationMode(compiler, IRStageInCodeGenerationModeUseSeparateStageInFunction);
    }

    output = g_ir.IRCompilerAllocCompileAndLink(compiler, (entry && entry[0]) ? entry : NULL,
                                                input, &err);
    if (!output) {
        a->ret_error_code = err ? g_ir.IRErrorGetCode(err) : 0;
        status = MADEIRA_IR_COMPILE_FAILED;
        /* ml863: keep the bytecode the converter refused, so the failure can
         * be reproduced on a Mac with the same converter and the real
         * diagnostics, instead of a code number on a phone. */
        {
            static int dumped;
            /* ml1139: the app's real Documents folder (where madeira.cfg lives).
             * Inside a Wine process HOME is the prefix (Documents/wine), so the
             * old HOME/Documents/... path never existed and nothing was saved. */
            const char *docs = getenv("MADEIRA_DOCS_DIR");
            const char *home = getenv("HOME");
            char dir[512], path[640];
            if (!home) home = getenv("CFFIXED_USER_HOME");
            if (!home) home = "/tmp";
            if (docs && docs[0]) snprintf(dir, sizeof dir, "%s/madeira-failed-shaders", docs);
            else snprintf(dir, sizeof dir, "%s/Documents/madeira-failed-shaders", home);
            if (dumped < 16) {
                FILE *f;
                mkdir(dir, 0755);
                snprintf(path, sizeof path, "%s/shader_%02d_code%u.bin", dir, dumped, a->ret_error_code);
                f = fopen(path, "wb");
                if (f) {
                    fwrite((const void *)(uintptr_t)a->dxil, 1, (size_t)a->dxil_len, f); fclose(f); dumped++;
                    snprintf(a->ret_note, sizeof a->ret_note, "bytecode saved to %s", path);
                } else {
                    snprintf(a->ret_note, sizeof a->ret_note, "could not save bytecode to %s (errno %d)", path, errno);
                }
            }
        }
        goto done;
    }

    stage = g_ir.IRObjectGetMetalIRShaderStage(output);
    a->ret_stage = (uint32_t)stage;

    lib = g_ir.IRMetalLibBinaryCreate();
    if (!lib || !g_ir.IRObjectGetMetalLibBinary(output, stage, lib)) {
        status = MADEIRA_IR_NO_METALLIB; goto done;
    }
    need = g_ir.IRMetalLibGetBytecodeSize(lib);
    a->ret_len = need;
    if (!need) { status = MADEIRA_IR_NO_METALLIB; goto done; }
    if (!a->out_buf || a->out_cap < need) { status = MADEIRA_IR_BUFFER_TOO_SMALL; goto done; }
    a->ret_len = g_ir.IRMetalLibGetBytecode(lib, (uint8_t *)(uintptr_t)a->out_buf);

    /* The converter RENAMES entry points, so the name to give Metal comes from
     * reflection rather than from what D3D called it.
     *
     * An empty name here is the converter's only signal that the entry point
     * did not exist: it does not reject an unknown name, it compiles and links
     * happily and produces a library with nothing callable in it. Treating that
     * as success would move the failure to a missing MTLFunction much later. */
    refl = g_ir.IRShaderReflectionCreate();
    if (refl && g_ir.IRObjectGetReflection(output, stage, refl)) {
        nm = g_ir.IRShaderReflectionGetEntryPointFunctionName(refl);
        if (nm && nm[0] && a->out_entry)
            snprintf((char *)(uintptr_t)a->out_entry, MADEIRA_IR_ENTRY_MAX, "%s", nm);
        a->ret_vs_input_count = 0;
        a->ret_loc_count = 0;
        if (a->out_locs && g_ir.IRShaderReflectionGetResourceCount && g_ir.IRShaderReflectionGetResourceLocations) {
            size_t n = g_ir.IRShaderReflectionGetResourceCount(refl);
            a->ret_loc_count = (uint32_t)n;
            if (n) {
                IRResourceLocation *rl = (IRResourceLocation *)calloc(n, sizeof *rl);
                if (rl) {
                    struct madeira_ir_loc *out = (struct madeira_ir_loc *)(uintptr_t)a->out_locs;
                    g_ir.IRShaderReflectionGetResourceLocations(refl, rl);
                    for (size_t i = 0; i < n && i < a->loc_cap; i++) {
                        out[i].type = (uint32_t)rl[i].resourceType; out[i].space = rl[i].space; out[i].slot = rl[i].slot;
                        out[i].offset = rl[i].topLevelOffset; out[i].size = rl[i].sizeBytes;
                    }
                    free(rl);
                }
            }
        }
        if (stage == IRShaderStageCompute && g_ir.IRShaderReflectionCopyComputeInfo && g_ir.IRShaderReflectionReleaseComputeInfo) {
            IRVersionedCSInfo csi;
            memset(&csi, 0, sizeof csi);
            if (g_ir.IRShaderReflectionCopyComputeInfo(refl, IRReflectionVersion_1_0, &csi)) {
                a->ret_tg_size[0] = csi.info_1_0.tg_size[0];
                a->ret_tg_size[1] = csi.info_1_0.tg_size[1];
                a->ret_tg_size[2] = csi.info_1_0.tg_size[2];
                g_ir.IRShaderReflectionReleaseComputeInfo(&csi);
            }
        }
        if (stage == IRShaderStageVertex && g_ir.IRShaderReflectionCopyVertexInfo && g_ir.IRShaderReflectionReleaseVertexInfo) {
            IRVersionedVSInfo vsi;
            memset(&vsi, 0, sizeof vsi);
            if (g_ir.IRShaderReflectionCopyVertexInfo(refl, IRReflectionVersion_1_0, &vsi)) {
                struct madeira_ir_vs_input *out = (struct madeira_ir_vs_input *)(uintptr_t)a->out_vs_inputs;
                size_t n = vsi.info_1_0.num_vertex_inputs;
                a->ret_vs_output_size = vsi.info_1_0.vertex_output_size_in_bytes;   /* ml927 */
                a->ret_vs_input_count = (uint32_t)n;
                for (size_t i = 0; out && i < n && i < a->vs_input_cap; i++) {
                    const IRVertexInputInfo_1_0 *vi = &vsi.info_1_0.vertex_inputs[i];
                    snprintf(out[i].name, sizeof out[i].name, "%s", vi->name ? vi->name : "");
                    out[i].attribute = vi->attributeIndex;
                }
                g_ir.IRShaderReflectionReleaseVertexInfo(&vsi);
            }
            /* ml927: the stage-in function for the object stage of a
             * geometry-emulation pipeline, synthesized from the application's
             * input layout (semantic names + formats). IRFormat numbering is
             * the DXGI numbering, so the formats pass through. */
            if (a->gs_emulation && a->layout) {
                const struct madeira_ir_input_layout *L = (const struct madeira_ir_input_layout *)(uintptr_t)a->layout;
                IRVersionedInputLayoutDescriptor il;
                IRMetalLibBinary *lib2 = g_ir.IRMetalLibBinaryCreate();
                memset(&il, 0, sizeof il);
                il.version = IRInputLayoutDescriptorVersion_1;
                il.desc_1_0.numElements = L->n > 31 ? 31 : L->n;
                for (uint32_t i = 0; i < il.desc_1_0.numElements; i++) {
                    il.desc_1_0.semanticNames[i] = L->el[i].semantic;
                    il.desc_1_0.inputElementDescs[i].semanticIndex = L->el[i].semantic_index;
                    il.desc_1_0.inputElementDescs[i].format = (IRFormat)L->el[i].format;
                    il.desc_1_0.inputElementDescs[i].inputSlot = L->el[i].slot;
                    il.desc_1_0.inputElementDescs[i].alignedByteOffset = L->el[i].offset;
                    il.desc_1_0.inputElementDescs[i].instanceDataStepRate = L->el[i].step_rate;
                    il.desc_1_0.inputElementDescs[i].inputSlotClass = L->el[i].per_instance ? IRInputClassificationPerInstanceData : IRInputClassificationPerVertexData;
                }
                if (lib2 && g_ir.IRMetalLibSynthesizeStageInFunction(compiler, refl, &il, lib2)) {
                    size_t n2 = g_ir.IRMetalLibGetBytecodeSize(lib2);
                    a->ret_len2 = n2;
                    if (a->out_buf2 && a->out_cap2 >= n2) g_ir.IRMetalLibGetBytecode(lib2, (uint8_t *)(uintptr_t)a->out_buf2);
                    else snprintf(a->ret_note, sizeof a->ret_note, "stage-in metallib needs %zu bytes", n2);
                } else snprintf(a->ret_note, sizeof a->ret_note, "stage-in function synthesis failed (%u elements)", il.desc_1_0.numElements);
                if (lib2) g_ir.IRMetalLibBinaryDestroy(lib2);
            }
        }
        if (stage == IRShaderStageGeometry && g_ir.IRShaderReflectionCopyGeometryInfo) {   /* ml927 */
            IRVersionedGSInfo gsi;
            memset(&gsi, 0, sizeof gsi);
            if (g_ir.IRShaderReflectionCopyGeometryInfo(refl, IRReflectionVersion_1_0, &gsi)) {
                a->ret_gs_max_prims = gsi.info_1_0.max_input_primitives_per_mesh_threadgroup;
                a->ret_gs_payload = gsi.info_1_0.max_payload_size_in_bytes;
                a->ret_gs_passthrough = gsi.info_1_0.is_passthrough ? 1u : 0u;
                g_ir.IRShaderReflectionReleaseGeometryInfo(&gsi);
            }
        }
    }
    if (!a->out_entry || !*(const char *)(uintptr_t)a->out_entry) {
        status = MADEIRA_IR_EMPTY_ENTRY;
        goto done;
    }

    status = MADEIRA_IR_OK;
done:
    if (refl) g_ir.IRShaderReflectionDestroy(refl);
    if (lib) g_ir.IRMetalLibBinaryDestroy(lib);
    if (output) g_ir.IRObjectDestroy(output);
    if (compiler) g_ir.IRCompilerDestroy(compiler);
    if (input) g_ir.IRObjectDestroy(input);
    if (rsig) g_ir.IRRootSignatureDestroy(rsig);
    if (err) g_ir.IRErrorDestroy(err);
    free(irs);
    free(irr);
    free(irp);
    free(ags_buf);
    a->ret_status = (uint32_t)status;
    return status;
}

/* winemetal's unix dispatch table entry. Signature matches every other slot. */
extern "C" int madeira_ir_convert(void *args) {
    madeira_ir_convert_impl((struct madeira_ir_convert_args *)args);
    return 0;   /* the call itself succeeded; ret_status carries the outcome */
}
