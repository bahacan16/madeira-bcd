#!/usr/bin/env python3
"""Make FEX's bad-link-target diagnostic fire on the case that matters.

The dispatcher calls ExitFunctionLink and then branches to whatever it
returns (Dispatcher.cpp:423 -- `br(TMP1)`). On 2026-09-15 the device log
caught that branch landing on 0 while x2 held a perfectly good function
pointer, so the call went through and the RETURN VALUE was null.

JIT.cpp already has a diagnostic for a suspicious link target, but it is
written as

    if (HostCode) { ...scream about a bad first instruction... }

so it says nothing when HostCode is null -- the one case that crashes
immediately and the one we have been chasing. Give it an else.

Applied as a CI patch rather than a submodule commit, like the suspend
patch: reverting is deleting one line from the workflow.
"""
import sys

OLD = """                        GuestRip, HostCode, FirstInsn);
    }
  }"""

NEW = """                        GuestRip, HostCode, FirstInsn);
    }
  } else {
    LogMan::Msg::EFmt("[fex-link] NULL TARGET: rip=0x{:x} -- ExitFunctionLink returns 0 "
                      "and the dispatcher will br to it", GuestRip);
  }"""


def main(path):
    s = open(path).read()
    if "NULL TARGET" in s:
        print("::notice::fex-link null-target log already patched")
        return 0
    n = s.count(OLD)
    if n != 1:
        print(f"::error::fex-link anchor not found ({n} matches) -- upstream changed, review needed")
        return 1
    open(path, "w").write(s.replace(OLD, NEW))
    print("::notice::fex-link NULL TARGET log added")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
