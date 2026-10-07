#!/usr/bin/env python3
"""madeira-bcd: let an older Swift read the prebuilt StikJIT.xcframework.

Upstream builds the framework with Swift 6.4, whose module interfaces name
types with module selectors (``Swift::String``, ``StikJIT::DDIPaths``). The
CI runners carry Swift 6.3 at most, which stops at the first ``::`` with
"expected '{' in struct". This rewrites the interface text into the dotted
form older compilers read; the binary and its ABI are untouched (library
evolution keeps the client side the same).

  * ``StikJIT::X``          -> ``X``  (inside its own module the bare name
    resolves; ``StikJIT.StikJIT`` would look for a type nested in the enum
    StikJIT, which shadows the module name)
  * ``Swift::X`` etc.       -> ``Swift.X``

Run in CI on a scratch checkout only; the committed framework stays as
upstream ships it. Exits non-zero if any selector is left.
"""
import pathlib
import re
import sys

MODULE = "StikJIT"


def convert(text: str) -> str:
    text = re.sub(r"\b%s::" % MODULE, "", text)
    text = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)::(?=[A-Za-z_`])", r"\1.", text)
    return text


def main(argv):
    root = pathlib.Path(argv[1] if len(argv) > 1 else "app/Frameworks/StikJIT.xcframework")
    files = sorted(root.rglob("*.swiftinterface"))
    if not files:
        print(f"no .swiftinterface under {root}", file=sys.stderr)
        return 1
    for f in files:
        old = f.read_text()
        new = convert(old)
        # Comment lines (the "// swift-module-flags" headers) never carry selectors.
        left = [l for l in new.splitlines() if not l.startswith("//") and re.search(r"\w::\w", l)]
        if left:
            print(f"{f}: module selectors left:\n  " + "\n  ".join(left), file=sys.stderr)
            return 1
        if new != old:
            f.write_text(new)
            print(f"{f}: {old.count('::')} module selectors rewritten")
        else:
            print(f"{f}: nothing to rewrite")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
