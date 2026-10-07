#!/usr/bin/env python3
"""Lean SM50 shaders: do not keep every D3D10/11 shader's parsed program in memory.

God of War dies on a loading screen (owner, 2026-10-01 11:05, build 271, swap
tier 6 GB + memory pool 2 GB from its game settings): the swap tier and the
pool worked (file-backed 2.1 GB, pool ~450 MB), yet the footprint jumped from
5.1 to 7.5 GB in two seconds (11:04:15-11:04:17) while the game's four
DxShaderCache threads were busy, and stayed there. The growth was all native
malloc -- DefaultMallocZone went from 489 MB / 0.89 M blocks to 2887 MB /
2.93 M blocks -- which no swap setting can move (the tier only moves guest
memory). DXMT's pipeline cache (d3d11_pipeline_cache.cpp) keeps every created
shader forever, keyed by its SHA-1, and each one holds airconv's parsed
program (SM50Initialize: basic blocks of decoded instructions and the
signature handlers) so it can compile variants later. That parsed program is
only needed while a variant is compiled.

With this patch SM50Initialize still parses (the reflection and argument
tables are answered from it), keeps a copy of the DXBC bytecode (a few KB)
and drops the heavy parts (bbs, signature_handlers). Each compile entry point
(SM50Compile, the tessellation and geometry pipeline ones) parses the
bytecode again into a temporary for the duration of that call. The parse is
deterministic and compilation only reads the shader (it copies
func_signature), so the output is the same; the cost is one extra parse per
compiled variant, small next to the LLVM and Metal compiles that follow.
Native airconv only: it reaches the committed 64-bit PE d3d11.dll and the
32-bit farm build alike, and the shader cache needs no new version.

MADEIRA_SM50_LEAN=0 keeps the parsed programs (the old behaviour).
[sm50-lean] lines: the mode once, then every 1000 shaders the live count, the
bytecode kept, an estimate of what the dropped parts held (sampled malloc
deltas, every 64th shader; other threads make it noisy) and malloc in use.

Idempotent; fails by name if an anchor moves. Run from the repository root.
"""
import pathlib
import sys

ROOT = pathlib.Path("dxmt/src/airconv")
MARKER = "madeira-bcd: lean SM50 shaders"

# willfaust/dxmt#6 (d4c38a2, dxmt db546ee): the same change upstream
# (SM50Initialize keeps `bytecode`, with_parsed_program re-parses per compile),
# without the MADEIRA_SM50_LEAN=0 switch and the [sm50-lean] counters. Checked
# before any edit, so an upstream pin is left untouched.
_hpp = (ROOT / "dxbc_converter.hpp").read_text()
if MARKER not in _hpp and "with_parsed_program(" in _hpp:
    print("dxbc_converter: lean SM50 shaders are upstream (willfaust/dxmt#6); nothing to do")
    sys.exit(0)


def edit(rel, pairs):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        print(f"{rel}: already patched")
        return
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"patch-airconv-sm50-lean: anchor found {s.count(old)} times (want 1) in {rel}:\n{old}")
        s = s.replace(old, new)
    if MARKER not in s:
        sys.exit(f"patch-airconv-sm50-lean: marker missing after editing {rel}")
    path.write_text(s)
    print(f"{rel}: patched")


edit("dxbc_converter.hpp", [
    ("""  std::vector<std::function<void(SignatureContext &)>> signature_handlers;
""",
     """  std::vector<std::function<void(SignatureContext &)>> signature_handlers;
  /* madeira-bcd: lean SM50 shaders (tools/patch-airconv-sm50-lean.py) --
   * when set, bbs and signature_handlers were dropped after SM50Initialize and
   * every compile parses madeira_bytecode again. */
  std::vector<uint8_t> madeira_bytecode;
  bool madeira_lean = false;
"""),
])


def compile_entry(signature, names):
    """Make each named sm50_shader_t parameter point at a full parse for the call."""
    lines = "".join(
        f"  std::unique_ptr<dxbc::SM50ShaderInternal> madeira_hold_{n};\n"
        f"  {n} = madeira_sm50_full({n}, madeira_hold_{n});\n"
        for n in names)
    old = signature + "{\n  using namespace llvm;\n  using namespace dxmt;\n"
    new = (signature + "{\n  using namespace llvm;\n  using namespace dxmt;\n"
           "  /* madeira-bcd: lean SM50 shaders -- compile from a fresh parse */\n" + lines)
    return (old, new)


edit("dxbc_converter.cpp", [
    ("#include <bit>\n", "#include <bit>\n#include <atomic>\n#include <cstdio>\n#include <cstdlib>\n"
     "#ifdef __APPLE__\n#include <malloc/malloc.h>\n#endif\n"),
    # The original parser keeps its body; SM50Initialize becomes a wrapper below.
    ("""AIRCONV_API int SM50Initialize(
  const void *pBytecode, size_t BytecodeSize, sm50_shader_t *ppShader,
  MTL_SHADER_REFLECTION *pRefl, sm50_error_t *ppError
) {""",
     """/* madeira-bcd: lean SM50 shaders -- the unmodified parser */
static int madeira_sm50_parse(
  const void *pBytecode, size_t BytecodeSize, sm50_shader_t *ppShader,
  MTL_SHADER_REFLECTION *pRefl, sm50_error_t *ppError
) {"""),
    ("""  *ppShader = sm50_shader;
  return 0;
};

AIRCONV_API void SM50GetArgumentsInfo(""",
     """  *ppShader = sm50_shader;
  return 0;
};

/* madeira-bcd: lean SM50 shaders (tools/patch-airconv-sm50-lean.py). */
static std::atomic<long> madeira_lean_live{0}, madeira_lean_lean{0}, madeira_lean_created{0};
static std::atomic<long long> madeira_lean_bytecode{0}, madeira_lean_freed{0}, madeira_lean_sampled{0};

static size_t madeira_malloc_in_use() {
#ifdef __APPLE__
  malloc_statistics_t st = {};
  malloc_zone_statistics(nullptr, &st);
  return st.size_in_use;
#else
  return 0;
#endif
}

static bool madeira_lean_enabled() {
  static const int mode = [] {
    const char *e = getenv("MADEIRA_SM50_LEAN");
    int on = !(e && e[0] == '0');
    fprintf(stderr, "[sm50-lean] madeira-bcd lean SM50 shaders %s\\n",
            on ? "on (parsed programs dropped after creation, re-parsed per compile)"
               : "off (MADEIRA_SM50_LEAN=0)");
    return on;
  }();
  return mode;
}

AIRCONV_API int SM50Initialize(
  const void *pBytecode, size_t BytecodeSize, sm50_shader_t *ppShader,
  MTL_SHADER_REFLECTION *pRefl, sm50_error_t *ppError
) {
  int ret = madeira_sm50_parse(pBytecode, BytecodeSize, ppShader, pRefl, ppError);
  if (ret || !ppShader || !*ppShader)
    return ret;
  auto sm50_shader = (dxmt::dxbc::SM50ShaderInternal *)*ppShader;
  long n = ++madeira_lean_created;
  madeira_lean_live++;
  if (madeira_lean_enabled()) {
    bool sample = (n & 63) == 1;
    size_t before = sample ? madeira_malloc_in_use() : 0;
    sm50_shader->madeira_bytecode.assign(
      (const uint8_t *)pBytecode, (const uint8_t *)pBytecode + BytecodeSize);
    sm50_shader->madeira_lean = true;
    decltype(sm50_shader->bbs)().swap(sm50_shader->bbs);
    decltype(sm50_shader->signature_handlers)().swap(sm50_shader->signature_handlers);
    if (sample) {
      size_t after = madeira_malloc_in_use();
      if (before > after)
        madeira_lean_freed += (long long)(before - after);
      madeira_lean_sampled++;
    }
    madeira_lean_lean++;
    madeira_lean_bytecode += (long long)BytecodeSize;
  }
  if (n == 1 || n % 1000 == 0) {
    long long sampled = madeira_lean_sampled.load();
    fprintf(stderr,
            "[sm50-lean] madeira-bcd shaders created=%ld live=%ld lean=%ld bytecode kept=%lld KB, "
            "dropped ~%lld KB per shader (%lld samples), malloc in use %zu MB\\n",
            n, madeira_lean_live.load(), madeira_lean_lean.load(),
            madeira_lean_bytecode.load() >> 10,
            sampled ? (madeira_lean_freed.load() / sampled) >> 10 : 0LL, sampled,
            madeira_malloc_in_use() >> 20);
  }
  return 0;
}

/* A shader whose parsed program was dropped is parsed again into `hold` for
 * one compile call; anything else is returned as it is. */
static sm50_shader_t madeira_sm50_full(
  sm50_shader_t pShader, std::unique_ptr<dxmt::dxbc::SM50ShaderInternal> &hold
) {
  auto lean = (dxmt::dxbc::SM50ShaderInternal *)pShader;
  if (!lean || !lean->madeira_lean)
    return pShader;
  sm50_shader_t full = nullptr;
  sm50_error_t err = nullptr;
  MTL_SHADER_REFLECTION refl;
  if (madeira_sm50_parse(lean->madeira_bytecode.data(), lean->madeira_bytecode.size(),
                         &full, &refl, &err) || !full) {
    static std::atomic<int> logged{0};
    if (logged++ < 4)
      fprintf(stderr, "[sm50-lean] madeira-bcd re-parse FAILED (%s); compiling the lean shader as it is\\n",
              err ? SM50GetErrorMessageString(err).c_str() : "no error object");
    if (err)
      SM50FreeError(err);
    return pShader;
  }
  hold.reset((dxmt::dxbc::SM50ShaderInternal *)full);
  return full;
}

AIRCONV_API void SM50GetArgumentsInfo("""),
    ("""AIRCONV_API void SM50Destroy(sm50_shader_t pShader) {
""",
     """AIRCONV_API void SM50Destroy(sm50_shader_t pShader) {
  if (pShader) { /* madeira-bcd: lean SM50 shaders */
    madeira_lean_live--;
    if (((dxmt::dxbc::SM50ShaderInternal *)pShader)->madeira_lean) {
      madeira_lean_lean--;
      madeira_lean_bytecode -=
        (long long)((dxmt::dxbc::SM50ShaderInternal *)pShader)->madeira_bytecode.size();
    }
  }
"""),
    compile_entry("""AIRCONV_API int SM50Compile(
  sm50_shader_t pShader, SM50_SHADER_COMPILATION_ARGUMENT_DATA *pArgs,
  const char *FunctionName, sm50_bitcode_t *ppBitcode, sm50_error_t *ppError
) """, ["pShader"]),
    compile_entry("""AIRCONV_API int SM50CompileTessellationPipelineHull(
  sm50_shader_t pVertexShader, sm50_shader_t pHullShader,
  struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *pHullShaderArgs,
  const char *FunctionName, sm50_bitcode_t *ppBitcode, sm50_error_t *ppError
) """, ["pVertexShader", "pHullShader"]),
    compile_entry("""AIRCONV_API int SM50CompileTessellationPipelineDomain(
  sm50_shader_t pHullShader, sm50_shader_t pDomainShader,
  struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *pDomainShaderArgs,
  const char *FunctionName, sm50_bitcode_t *ppBitcode, sm50_error_t *ppError
) """, ["pHullShader", "pDomainShader"]),
    compile_entry("""AIRCONV_API int SM50CompileGeometryPipelineVertex(
  sm50_shader_t pVertexShader, sm50_shader_t pGeometryShader,
  struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *pVertexShaderArgs,
  const char *FunctionName, sm50_bitcode_t *ppBitcode, sm50_error_t *ppError
) """, ["pVertexShader", "pGeometryShader"]),
    compile_entry("""AIRCONV_API int SM50CompileGeometryPipelineGeometry(
  sm50_shader_t pVertexShader, sm50_shader_t pGeometryShader,
  struct SM50_SHADER_COMPILATION_ARGUMENT_DATA *pGeometryShaderArgs,
  const char *FunctionName, sm50_bitcode_t *ppBitcode, sm50_error_t *ppError
) """, ["pVertexShader", "pGeometryShader"]),
])

# The command-line converter calls airconv's internals straight after
# SM50Initialize, so it needs the parsed programs.
edit("airconv_cli.cpp", [
    ("""int main(int argc, char **argv) {
  InitLLVM X(argc, argv);
""",
     """int main(int argc, char **argv) {
  InitLLVM X(argc, argv);
  setenv("MADEIRA_SM50_LEAN", "0", 1); /* madeira-bcd: lean SM50 shaders */
"""),
])
