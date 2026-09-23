# Storage-backed guest memory (opt-in)

Settings → Experimental → **Storage-backed memory** (`MADEIRA_SWAP=1`).

## What it does

iOS kills an app when its `phys_footprint` crosses the jetsam limit. That
figure counts anonymous ("internal") memory, not file-backed ("external")
pages, which the kernel can write back to their file and drop under pressure.

With the setting on, every guest `NtAllocateVirtualMemory` of at least
`MADEIRA_SWAP_MIN_MB` (default 128) that is not executable, write-watched,
DOS memory or a placeholder is re-mapped `MAP_SHARED | MAP_FIXED` over a
sparse, already-unlinked file in the app's Caches directory
(`ios_swap_back_view` in `build/ntdll-unix/virtual_ios.c`). It is skipped
when under 4 GB is free on disk; a failed remap puts a fresh anonymous
mapping back.

The swap directory comes from `MADEIRA_SWAP_DIR` (the app exports its
container Caches path), then `_CS_DARWIN_USER_CACHE_DIR`, then `TMPDIR`. Not
`$HOME`: the app repoints `HOME` at the Wine prefix, which is why the first
device build (ml796) failed its self-test with `ENOENT` (fixed in ml797).

## The self-test

On the first eligible allocation the process measures its own premise, once:

```
[swap-selftest] ml796 256 MB ANONYMOUS, dirtied: footprint 944 -> 1200 MB (+256)
[swap-selftest] ml796 256 MB FILE-BACKED, dirtied: footprint 944 -> 944 MB (+0),
                internal +0 MB, external +256 MB -- iOS does NOT charge it to the
                jetsam footprint: offload works
```

That is the result on iPhone 17 Pro Max, iOS 27.0 (2026-09-23, build 113).
The same run storage-backed FEX's 256 MB arena probe and went on to run the
x64 D3D11 cube normally at ~60 FPS; the footprint stayed at ~1.26 GB.

## What it does not cover

- The 896 MB JIT pool: it is pinned and remapped through the debugger at
  launch, and every `[no-footprint]` variant for it is refused. It is most of
  the cube's footprint.
- Allocations below the threshold, executable memory, and anything Metal
  allocates on the host side.
- Stored pages are slower to fault back in than RAM. It is a way to survive
  large commits, not a speedup.
