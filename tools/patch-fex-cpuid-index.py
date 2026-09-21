#!/usr/bin/env python3
"""Bounds-check FEX's per-CPU CPUID metadata before indexing it.

NOT OUR FIX. This is the Madeira Build 79 corresponding-source package's
`02-fex-cpuid-fix/cpuid-metadata-index.patch`, written by Madeira's developer
and applied here verbatim to the two files it touches.

PerCPUData holds one record per processor the host described. The iOS host
currently provides a single generic MIDR while reporting more logical CPUs,
so `PerCPUData[CPU]` reads past the end -- and the leaves that do it are the
ones a guest calls early to print its own CPU name:

    Function_1Ah          hybrid big/little topology
    Function_8000_0002h   brand string, bytes 0-15
    Function_8000_0003h   brand string, bytes 16-31
    Function_8000_0004h   brand string, bytes 32-47

RunFunctionName already wraps with `CPU % PerCPUData.size()` before calling
the 8000_000xh helpers, so the helpers disagreed with their only caller. The
fix applies that same convention inside each one, returns an empty result
when there are no records at all, and guards a null ProductName. The brand
string arithmetic also becomes unsigned-safe: strlen() - 16 on a shorter
name wrapped instead of clamping, and the old std::max<ssize_t> only hid it
for the length, not for the pointer.

Applied as a CI patch because the FEX submodule points at willfaust/FEX,
which we cannot push to.
"""
import sys

CPP_EDITS = [
    (
        """  if (Hybrid) {
    uint32_t CPU = GetCPUID();
    auto& Data = PerCPUData[CPU];""",
        """  if (Hybrid && !PerCPUData.empty()) {
    uint32_t CPU = GetCPUID();
    auto& Data = PerCPUData[CPU % PerCPUData.size()];""",
    ),
    (
        """  FEXCore::CPUID::FunctionResults Res {};
  auto& Data = PerCPUData[CPU];
  memcpy(&Res, Data.ProductName, std::min(strlen(Data.ProductName), sizeof(FEXCore::CPUID::FunctionResults)));""",
        """  FEXCore::CPUID::FunctionResults Res {};
  // Personal Madeira integration: host processor IDs may outnumber the
  // metadata records (the iOS host currently provides one generic MIDR).
  // Match RunFunctionName's existing indexing convention without changing
  // processor counts or scheduler affinity.
  if (PerCPUData.empty()) return Res;
  auto& Data = PerCPUData[CPU % PerCPUData.size()];
  if (!Data.ProductName) return Res;
  memcpy(&Res, Data.ProductName, std::min(strlen(Data.ProductName), sizeof(FEXCore::CPUID::FunctionResults)));""",
    ),
    (
        """  auto& Data = PerCPUData[CPU];
  const auto RemainingStringSize = std::max<ssize_t>(0, strlen(Data.ProductName) - 16);
  memcpy(&Res, Data.ProductName + 16, std::min<size_t>(RemainingStringSize, sizeof(FEXCore::CPUID::FunctionResults)));""",
        """  if (PerCPUData.empty()) return Res;
  auto& Data = PerCPUData[CPU % PerCPUData.size()];
  if (!Data.ProductName) return Res;
  const size_t Length = strlen(Data.ProductName);
  if (Length <= 16) return Res;
  memcpy(&Res, Data.ProductName + 16, std::min(Length - 16, sizeof(FEXCore::CPUID::FunctionResults)));""",
    ),
    (
        """  auto& Data = PerCPUData[CPU];
  const auto RemainingStringSize = std::max<ssize_t>(0, strlen(Data.ProductName) - 32);
  memcpy(&Res, Data.ProductName + 32, std::min<size_t>(RemainingStringSize, sizeof(FEXCore::CPUID::FunctionResults)));""",
        """  if (PerCPUData.empty()) return Res;
  auto& Data = PerCPUData[CPU % PerCPUData.size()];
  if (!Data.ProductName) return Res;
  const size_t Length = strlen(Data.ProductName);
  if (Length <= 32) return Res;
  memcpy(&Res, Data.ProductName + 32, std::min(Length - 32, sizeof(FEXCore::CPUID::FunctionResults)));""",
    ),
]

H_EDITS = [
    (
        """  FEXCore::CPUID::FunctionResults RunFunctionName(uint32_t Function, uint32_t Leaf, uint32_t CPU) const {
    if (Function == 0x8000'0002U) {""",
        """  FEXCore::CPUID::FunctionResults RunFunctionName(uint32_t Function, uint32_t Leaf, uint32_t CPU) const {
    if (PerCPUData.empty()) return {};
    if (Function == 0x8000'0002U) {""",
    ),
]

DONE_MARKER = "Match RunFunctionName's existing indexing convention"


def patch(path, edits, label):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        s = f.read()
    for old, new in edits:
        if new in s:
            continue
        n = s.count(old)
        if n != 1:
            print(f"::error::CPUID anchor not found ({n} matches) in {label} -- pinned FEX changed, review needed")
            return None
        s = s.replace(old, new)
    return s


def main(cpp_path, h_path):
    with open(cpp_path, encoding="utf-8", errors="surrogateescape") as f:
        if DONE_MARKER in f.read():
            print("::notice::FEX CPUID per-CPU indexing already bounds-checked")
            return 0
    cpp = patch(cpp_path, CPP_EDITS, "CPUID.cpp")
    if cpp is None:
        return 1
    h = patch(h_path, H_EDITS, "CPUID.h")
    if h is None:
        return 1
    with open(cpp_path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(cpp)
    with open(h_path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(h)
    print("::notice::FEX CPUID per-CPU indexing bounds-checked (4 sites in CPUID.cpp, 1 in CPUID.h)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
