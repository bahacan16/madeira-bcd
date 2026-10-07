#!/usr/bin/env python3
"""Shared D3D11 textures on iOS: hand back no mach port, so DXMT keeps them unshared.

Crysis Remastered (64-bit) loads a level to 100% and dies (log 2026-09-30,
build 258): 15 x "DeviceTexture: Failed to register mach port for shared
texture" -- CreateTexture2D with a SHARED misc flag returns E_FAIL because
WMTBootstrapRegister (bootstrap_register2) is not allowed for an iOS app --
and then the game's RenderThread clears a render-target view it never got:
ClearRenderTargetView(NULL) reads address 0 in d3d11.dll (rva 0x885ac).

DXMT already has the right fallback (ml866 in d3d11_texture_device.cpp): when
the allocation has no mach port, the texture is kept and the call succeeds
"unshared". Sharing across processes cannot work on iOS anyway (no bootstrap
registration), so on iOS the native side now creates the texture as before
but does not make a mach port for it. This is native code, so it reaches the
committed 64-bit PE d3d11.dll too. God of War never creates shared textures
(its 2026-09-30 19:01 log has none). MADEIRA_SHARED_TEXTURE_PORT=1 restores
the old behaviour; [shared-tex] lines record the first few.

Idempotent; fails by name if the anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: shared texture without mach port"

OLD = """    id<MTLTexture> ret = [device newSharedTextureWithDescriptor:desc];
    MTLSharedTextureHandle *handle = [ret newSharedTextureHandle];
    params->ret = (obj_handle_t)ret;
    info->gpu_resource_id = [ret gpuResourceID]._impl;
    info->mach_port = [handle createMachPort]; // implicitly add ref to underlying IOSurface
    [handle release];
    [desc release];
"""
NEW = """    id<MTLTexture> ret = [device newSharedTextureWithDescriptor:desc];
    params->ret = (obj_handle_t)ret;
    info->gpu_resource_id = [ret gpuResourceID]._impl;
#if TARGET_OS_IOS
    /* madeira-bcd: shared texture without mach port (tools/patch-winemetal-ios-shared-texture.py).
     * An iOS app cannot bootstrap-register the port, so the caller's E_FAIL
     * path was the only outcome; no port makes DXMT keep the texture unshared. */
    static int madeira_port = -1;
    static unsigned madeira_logged;
    if (madeira_port < 0) {
      const char *e = getenv("MADEIRA_SHARED_TEXTURE_PORT");
      madeira_port = e && e[0] == '1';
    }
    if (!madeira_port) {
      if (++madeira_logged <= 4)
        fprintf(stderr, "[shared-tex] madeira-bcd shared texture %ux%u created without a mach port (unshared, #%u)\\n",
                (unsigned)info->width, (unsigned)info->height, madeira_logged);
      info->mach_port = 0;
      [desc release];
      return STATUS_SUCCESS;
    }
#endif
    MTLSharedTextureHandle *handle = [ret newSharedTextureHandle];
    info->mach_port = [handle createMachPort]; // implicitly add ref to underlying IOSurface
    [handle release];
    [desc release];
"""


def main():
    s = PATH.read_text()
    if MARKER in s:
        print("winemetal_unix.c: already patched")
        return 0
    if "An iOS app cannot bootstrap-register the port" in s:
        # willfaust/dxmt#10 (39aa198); MADEIRA_SHARED_TEXTURE_PORT=1 is gone with it
        print("winemetal_unix.c: shared texture without mach port is upstream (willfaust/dxmt#10); nothing to do")
        return 0
    if s.count(OLD) != 1:
        sys.exit(f"patch-winemetal-ios-shared-texture: anchor found {s.count(OLD)} times in {PATH}")
    PATH.write_text(s.replace(OLD, NEW))
    print("winemetal_unix.c: patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
