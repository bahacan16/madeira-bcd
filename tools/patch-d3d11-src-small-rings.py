#!/usr/bin/env python3
"""Small staging rings for named programs in d3d11-src.dll (DXMT_SMALL_RINGS).

DXMT's command queue keeps its staging ring (the data a draw or an
UpdateSubresource uploads) and its resource initializer keeps an upload heap
in 32 MB Metal buffers. A ring whose current block fills while the GPU still
reads it takes a second block, and frees a block only after 300 later
submissions; a device that stops submitting keeps its blocks for good. In the
Red Dead Redemption 2 session of 2026-10-09 13:09 the Rockstar Games
Launcher's device held 132 MB of them (staging-ring 68 MB, init-upload 64 MB)
and Social Club's CEF 68 MB, of the 253 MB Metal had allocated before the
game started, and they stayed until the session was killed.

DXMT_SMALL_RINGS = <program>;<program> (program file names, case-insensitive,
';' or ','; 1 or * = every program) gives the rings whose blocks are larger
than 4 MB 4 MB blocks in the named programs: the staging ring, the copy-temp
ring and the upload heap. An upload larger than a block still gets a block of
its own, released the way upstream releases such blocks (DXMT's deferred
contexts run on 2 MB blocks that way). Argument-buffer, command-data and
deferred-context rings (4 MB or less) are unchanged. Unset, empty or 0: 32 MB
blocks as upstream.

The first ring of a named program logs "[small-rings] madeira-bcd: <exe> uses
4 MB staging and upload ring blocks (DXMT_SMALL_RINGS)" once;
tools/build-d3d11-dll.sh looks for that string in the DLL it builds.

Applied by tools/build-d3d11-dll.sh to its COPY of dxmt/src/dxmt; the
submodule is not modified, and the name stays outside the i386 farm's
tools/patch-dxmt-*.py cache key (that build never sees this patch). Every
use of the ring's BlockSize must go through ring_block_size(): an anchor that
moved, or a use of BlockSize this script does not know, exits 1 with the file
left as it was. Idempotent.

Usage: patch-d3d11-src-small-rings.py <copy of dxmt/src/dxmt>
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dxmt/src/dxmt")
FILE = "dxmt_ring_bump_allocator.hpp"
MARKER = "madeira-bcd: small staging rings"

HELPERS = r'''constexpr size_t kStagingBlockLifetime = 300;

/* madeira-bcd: small staging rings (tools/patch-d3d11-src-small-rings.py).
 * DXMT_SMALL_RINGS = <program>;<program> (or 1 / * for every program) gives
 * every ring whose blocks are larger than 4 MB -- the command queue's staging
 * and copy-temp rings, the resource initializer's upload heap -- 4 MB blocks
 * in the named programs. A ring keeps a block until 300 later submissions and
 * an idle device keeps it for good: the Rockstar Games Launcher held 132 MB of
 * 32 MB blocks and Social Club's CEF 68 MB through a whole RDR2 session
 * (2026-10-09 13:09). An upload larger than a block still gets a block of its
 * own. Unset, empty or 0: BlockSize as upstream. Read once per process. */
constexpr size_t kMadeiraSmallRingBlockSize = 0x400000; // 4MB

inline bool
madeiraSmallRingsHere() {
  static const bool here = [] {
    const std::string list = env::getEnvVar("DXMT_SMALL_RINGS");
    if (list.empty() || list == "0")
      return false;
    auto lower = [](std::string s) {
      for (auto &c : s)
        if (c >= 'A' && c <= 'Z')
          c += 'a' - 'A';
      return s;
    };
    const std::string exe = env::getExeName();
    const std::string want = lower(exe);
    bool hit = false;
    for (size_t i = 0; !hit && i <= list.size();) {
      size_t j = list.find_first_of(";,", i);
      if (j == std::string::npos)
        j = list.size();
      std::string item = list.substr(i, j - i);
      const size_t a = item.find_first_not_of(" \t");
      item = a == std::string::npos ? std::string() : item.substr(a, item.find_last_not_of(" \t") - a + 1);
      hit = item == "1" || item == "*" || (!item.empty() && lower(item) == want);
      i = j + 1;
    }
    if (hit)
      WARN("[small-rings] madeira-bcd: ", exe, " uses ", kMadeiraSmallRingBlockSize >> 20,
           " MB staging and upload ring blocks (DXMT_SMALL_RINGS)");
    return hit;
  }();
  return here;
}

inline size_t
madeiraRingBlockSize(size_t natural) {
  return natural > kMadeiraSmallRingBlockSize && madeiraSmallRingsHere() ? kMadeiraSmallRingBlockSize : natural;
}
'''

EDITS = [
    ("helpers after kStagingBlockLifetime",
     "constexpr size_t kStagingBlockLifetime = 300;\n", HELPERS),
    ("ring_block_size() next to block_size",
     "  static constexpr size_t block_size = BlockSize;\n",
     "  static constexpr size_t block_size = BlockSize;\n"
     "\n"
     "  /* madeira-bcd: small staging rings -- BlockSize, or 4 MB for a larger\n"
     "   * ring in a program DXMT_SMALL_RINGS names (madeiraRingBlockSize). */\n"
     "  static size_t\n"
     "  ring_block_size() {\n"
     "    return madeiraRingBlockSize(BlockSize);\n"
     "  }\n"),
    ("preallocate: block size",
     "          .total_size = BlockSize,\n",
     "          .total_size = ring_block_size(),\n"),
    ("preallocate: allocation",
     "          .block = allocator_.allocate(BlockSize),\n",
     "          .block = allocator_.allocate(ring_block_size()),\n"),
    ("allocate: new block size",
     "std::max(size, BlockSize) // in case required size is larger than block size",
     "std::max(size, ring_block_size()) // in case required size is larger than block size"),
    ("free_blocks: ad-hoc test",
     "    auto adhoc = front.total_size != BlockSize && !front.reusable_oversize;\n",
     "    auto adhoc = front.total_size != ring_block_size() && !front.reusable_oversize;\n"),
    ("allocate_or_reuse_block: ad-hoc test",
     "      if (front.total_size != BlockSize && !front.reusable_oversize) {\n",
     "      if (front.total_size != ring_block_size() && !front.reusable_oversize) {\n"),
    ("allocate_or_reuse_block: oversize test",
     "ringOversizeReuseEnabled() && block_size > BlockSize &&",
     "ringOversizeReuseEnabled() && block_size > ring_block_size() &&"),
]

# What may still name BlockSize once every edit is in: the template parameter
# lists, the out-of-class member definitions, block_size and ring_block_size().
ALLOWED = [
    r"template <typename Allocator, size_t BlockSize = kStagingBlockSize, class mutex = dxmt::mutex>",
    r"template <typename Allocator, size_t BlockSize, class mutex>",
    r"RingBumpState<Allocator, BlockSize, mutex>::",
    r"static constexpr size_t block_size = BlockSize;",
    r"return madeiraRingBlockSize\(BlockSize\);",
]


def main():
    path = ROOT / FILE
    if not path.is_file():
        sys.exit(f"patch-d3d11-src-small-rings: {path} not found")
    s = path.read_text()
    if MARKER in s:
        print(f"patch-d3d11-src-small-rings: {FILE} already patched")
        return 0
    for what, old, new in EDITS:
        if s.count(old) != 1:
            sys.exit(f"patch-d3d11-src-small-rings: anchor for {what} found {s.count(old)} times (want 1) "
                     f"in {FILE}; nothing written")
        s = s.replace(old, new, 1)
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", s, flags=re.S)
    for allowed in ALLOWED:
        code = re.sub(allowed, "", code)
    if re.search(r"\bBlockSize\b", code):
        line = next(l for l in code.split("\n") if re.search(r"\bBlockSize\b", l))
        sys.exit(f"patch-d3d11-src-small-rings: {FILE} uses BlockSize where this script does not expect it "
                 f"({line.strip()[:80]!r}); nothing written")
    if MARKER not in s:
        sys.exit("patch-d3d11-src-small-rings: marker missing after editing; nothing written")
    path.write_text(s)
    print(f"patch-d3d11-src-small-rings: patched {FILE} (DXMT_SMALL_RINGS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
