#!/usr/bin/env python3
"""Check that the block cache hands back a block entry, not an address inside a block's tail.

The 2026-09-22 evening run pins the failure to a pointer, and the pointer is
not random -- it is a field address inside FEX's own block metadata:

    [jit-tail] PC is inside a JIT block's tail (PC = tail+8) -- tail=0x153b7edf8
               guest_rip=0x71fe487470 block_size=132 rip_entries=2 single_inst=1
    [mach_exc] insn_stream PC-12..PC+8: 00000001 00000084 00000000 [fe487470] 00000071

tail+0 is JITCodeTail::Size (0x84 = 132, the block's own size) and tail+8 is
JITCodeTail::RIP, whose value is 0x71fe487470 -- the same guest RIP the block
was compiled for. The CPU executed that field as an instruction, which is why
the exception is c000001d. This is the third sighting: tail+8 on 2026-09-15,
tail+24 (NumberOfRIPEntries) this morning, tail+8 again now. Three different
field addresses of the same structure is not a wild pointer.

The caller is now identified too. LR is dispatcher+0x3d0 and the instruction
before it is `blr x4` with x4 taken from the dispatcher's literal pool -- the
CompileBlock call on the NoBlock path. The dispatcher branches to whatever
CompileBlock returns, and ContextImpl::CompileBlock's first act is to return
LookupCache::FindBlock's answer when the cache has one. So the question is
whether that cache entry is the block entry or the tail field.

It is checkable without guessing: FEX puts a JITCodeHeader immediately before
every block entry, holding OffsetToBlockTail. For the faulting block that word
reads 0x58 and entry+0x58 lands exactly on the tail, so a genuine entry always
has a small, plausible offset at entry-4. An address inside the tail does not.

So log it. The first 24 hits go out unconditionally, and any hit that fails the
header check goes out however late it happens, capped. One run then says
whether the cache is the producer -- and if every hit is sane, the cache is
cleared and the search moves to what CompileBlock returns after compiling.

Logging only; no control flow changes.
"""
import sys

OLD = """    if (auto HostCode = Thread->LookupCache->FindBlock(Thread, GuestRIP)) {
      return HostCode;
    }"""

NEW = """    if (auto HostCode = Thread->LookupCache->FindBlock(Thread, GuestRIP)) {
      /* iOS-Madeira ml791: see tools/patch-fex-cachehit-trace.py. A block entry
       * always has its JITCodeHeader (OffsetToBlockTail) in the word before it;
       * an address inside a block's tail does not. */
      {
        static std::atomic<int> IosHitTrace {0};
        static std::atomic<int> IosHitBad {0};
        const int IosHitN = IosHitTrace.fetch_add(1, std::memory_order_relaxed);
        const uint32_t IosHdr = *reinterpret_cast<const uint32_t*>(HostCode - 4);
        const bool IosSane = IosHdr >= 8 && IosHdr < (1u << 20);
        if (!IosSane && IosHitBad.fetch_add(1, std::memory_order_relaxed) < 8) {
          LogMan::Msg::EFmt("[cache-hit] BAD #{} rip={:#x} host={:#x} hdr@host-4={:#x} insn0={:#x} "
                            "<== NOT A BLOCK ENTRY, the cache is handing back an address inside a "
                            "block's tail rev=ml791",
                            IosHitN, GuestRIP, static_cast<uint64_t>(HostCode), IosHdr,
                            *reinterpret_cast<const uint32_t*>(HostCode));
        } else if (IosHitN < 24) {
          LogMan::Msg::EFmt("[cache-hit] #{} rip={:#x} host={:#x} hdr@host-4={:#x} insn0={:#x} rev=ml791",
                            IosHitN, GuestRIP, static_cast<uint64_t>(HostCode), IosHdr,
                            *reinterpret_cast<const uint32_t*>(HostCode));
        }
      }
      return HostCode;
    }"""


def main(path):
    s = open(path).read()
    if "[cache-hit] BAD" in s:
        print("::notice::fex cache-hit trace already patched")
        return 0
    n = s.count(OLD)
    if n != 1:
        print(f"::error::cache-hit anchor not found ({n} matches) -- upstream changed, review needed")
        return 1
    open(path, "w").write(s.replace(OLD, NEW, 1))
    print("::notice::fex block-cache hit trace added (first 24 hits, plus any bad entry)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
