#!/usr/bin/env python3
"""Guard two FEX diagnostic probes that do not compile for the iOS static libs.

FEX 0f8edf8 (ios-port-2607), as pinned by upstream Madeira:

- Core.cpp: the [ffs-bypass] and [cb-entry] blocks in CompileBlock sit after
  the #endif that closes the FEX_IOS_HOST section, but the buffers they read
  (IosFfsBypassLog, IosCbEntryLog) are declared only inside it.
- Arm64.cpp: the [caspal128] probe calls VirtualQuery with a
  MEMORY_BASIC_INFORMATION, which exist on the ARM64EC/PE target only.

Both only print logs, so wrap them in the guard their declarations live under.
Idempotent; fails by name if the anchors move (the pin changed).

Run from the repository root.
"""
import pathlib, sys

def patch(path, start, end, guard, label):
    p = pathlib.Path(path)
    s = p.read_text()
    if guard + "\n" + start in s:
        print(f"{label}: already patched"); return
    if s.count(start) != 1 or s.count(end) != 1:
        print(f"::error::{label}: patch anchors not unique -- upstream changed, review needed")
        sys.exit(1)
    s = s.replace(start, guard + "\n" + start)
    s = s.replace(end, end + ("#endif\n" if end.endswith("\n") else "\n#endif"))
    p.write_text(s)
    print(f"{label}: patched")

patch("FEX/FEXCore/Source/Interface/Core/Core.cpp",
      "  {\n    static uint64_t FfsLastCount = 0;",
      "IosCbEntryLog[4], IosCbEntryLog[5], IosCbEntryLog[7]);\n    }\n  }\n",
      "#ifdef FEX_IOS_HOST", "Core.cpp")
patch("FEX/FEXCore/Source/Utils/ArchHelpers/Arm64.cpp",
      "  MEMORY_BASIC_INFORMATION mbi {};",
      "                    mbi.Protect, type, mbi.State);",
      "#ifndef __APPLE__", "Arm64.cpp")
