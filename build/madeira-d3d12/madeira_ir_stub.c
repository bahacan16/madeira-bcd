/* madeira-bcd: stand-in for madeira_ir_unix.mm when the build has no Metal
 * Shader Converter headers.
 *
 * winemetal's unix dispatch table references madeira_ir_convert (call 127)
 * unconditionally, so without this the app does not link at all. The real
 * conversion service needs the converter's public headers, which upstream
 * resolves from Apple's installer package on the developer's Mac and does not
 * track. With this stub every D3D12 pipeline creation fails with a named
 * status instead of the app failing to build, and D3D11 is untouched.
 *
 * Provide the headers (MADEIRA_MSC_INCLUDE, or the package deps.sh expects)
 * and build/dxmt-ios/build.sh compiles the real service instead. */
#include <stdint.h>
#include <stdio.h>
#include "../../research/madeira-d3d12/src/madeira_ir_abi.h"

int madeira_ir_convert(void *args)
{
    static int said;
    struct madeira_ir_convert_args *a = args;
    if (!said++)
        fprintf(stderr, "[d3d12] madeira-bcd: built WITHOUT the Metal Shader Converter headers -- "
                        "shader conversion is unavailable, every D3D12 pipeline will fail (status %d)\n",
                MADEIRA_IR_NO_DYLIB);
    if (a)
    {
        a->ret_len = 0;
        a->ret_status = MADEIRA_IR_NO_DYLIB;
        a->ret_error_code = 0;
    }
    return 0;   /* the call itself succeeded; ret_status carries the outcome */
}

/* The in-app converter canary (research/madeira-d3d12/tests/native/msc_canary.mm)
 * needs the same headers. ContentView's canary button links against it, so
 * answer here: one failed check, and say why. */
int madeira_d3d12_canary_run_log(const char *fixture_dir, const char *dylib_path,
                                 void (*sink)(const char *), const char *log_path,
                                 const char *build_id)
{
    (void)fixture_dir; (void)dylib_path; (void)build_id;
    const char *msg = "[canary] madeira-bcd: this build has no Metal Shader Converter headers, "
                      "so the converter canary was not compiled in -- 1 check failed by construction";
    if (sink) sink(msg);
    if (log_path && *log_path)
    {
        FILE *f = fopen(log_path, "w");
        if (f) { fprintf(f, "%s\n", msg); fclose(f); }
    }
    return 1;
}

int madeira_d3d12_canary_run(const char *fixture_dir, const char *dylib_path,
                             void (*sink)(const char *))
{
    return madeira_d3d12_canary_run_log(fixture_dir, dylib_path, sink, NULL, NULL);
}
