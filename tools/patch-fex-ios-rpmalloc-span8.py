#!/usr/bin/env python3
"""Use coherent 4 or 8 MiB rpmalloc spans in the ARM64EC build copy.

The span mask, large page size and size-class ceiling must change together.
Blocks at the page size cannot fit beside its header. Route sizes above
3.5 or 7 MiB through the existing exact-size huge path. Leave the
non-FEX_IOS_HOST configuration, ownership, locks and band limits unchanged.
This is a Madeira build overlay, not a contribution to the pinned submodule.
"""
from pathlib import Path
import argparse
import sys


MARKER = "madeira-bcd: coherent 8 MiB spans"
EDITS = (
    (
        "#define LARGE_BLOCK_SIZE_LIMIT (8 * 1024 * 1024)",
        "#ifdef FEX_IOS_HOST\n"
        "/* madeira-bcd: the largest class that fits beside an 8 MiB page's header. */\n"
        "#define LARGE_BLOCK_SIZE_LIMIT (7 * 1024 * 1024)\n"
        "#else\n#define LARGE_BLOCK_SIZE_LIMIT (8 * 1024 * 1024)\n#endif",
    ),
    (
        "#define MEDIUM_SIZE_CLASS_COUNT 24\n#define LARGE_SIZE_CLASS_COUNT 20",
        "#define MEDIUM_SIZE_CLASS_COUNT 24\n"
        "#ifdef FEX_IOS_HOST\n#define LARGE_SIZE_CLASS_COUNT 19\n"
        "#else\n#define LARGE_SIZE_CLASS_COUNT 20\n#endif",
    ),
    (
        "#define LARGE_PAGE_SIZE_SHIFT 24\n#else",
        "/* madeira-bcd: paired with the 8 MiB span and its 7 MiB class ceiling. */\n"
        "#define LARGE_PAGE_SIZE_SHIFT 23\n#else",
    ),
    (
        'static const char ios_span16_marker[] __attribute__((used)) = "rpmalloc-span16 rev=ml436";\n'
        "#define SPAN_SIZE (16 * 1024 * 1024)",
        "/* " + MARKER + ": halve both reservation size and alignment.\n"
        " * Keeping a 16 MiB mask/alignment with smaller reservations still\n"
        " * exhausts aligned slots. All page/header lookup masks derive below;\n"
        " * live-span ownership and the protected FEX band are unchanged. */\n"
        'static const char ios_span8_marker[] __attribute__((used)) = "rpmalloc-span8 madeira-bcd";\n'
        "#define SPAN_SIZE (8 * 1024 * 1024)",
    ),
    (
        "LCLASS(262144), LCLASS(327680), LCLASS(393216), LCLASS(458752), LCLASS(524288)};",
        "LCLASS(262144), LCLASS(327680), LCLASS(393216), LCLASS(458752)\n"
        "#ifndef FEX_IOS_HOST\n    , LCLASS(524288)\n#endif\n};",
    ),
)


def edits_for(span_mb):
    if span_mb == 8:
        return MARKER, EDITS
    if span_mb != 4:
        raise ValueError("supported span sizes are 4 and 8 MiB")
    marker = "madeira-bcd: coherent 4 MiB spans"
    edits = list(EDITS)
    edits[0] = (edits[0][0], edits[0][1].replace('(7 * 1024 * 1024)', '(7 * 512 * 1024)').replace('8 MiB', '4 MiB'))
    edits[1] = (edits[1][0], edits[1][1].replace('LARGE_SIZE_CLASS_COUNT 19', 'LARGE_SIZE_CLASS_COUNT 15'))
    edits[2] = (edits[2][0], edits[2][1].replace('8 MiB', '4 MiB').replace('7 MiB', '3.5 MiB').replace('SHIFT 23', 'SHIFT 22'))
    edits[3] = (edits[3][0], edits[3][1].replace(MARKER, marker).replace('halve both', 'reduce both').replace('span8', 'span4').replace('8 * 1024', '4 * 1024'))
    edits[4] = (
        "LCLASS(81920),  LCLASS(98304),  LCLASS(114688), LCLASS(131072), LCLASS(163840), LCLASS(196608), LCLASS(229376),\n"
        "    LCLASS(262144), LCLASS(327680), LCLASS(393216), LCLASS(458752), LCLASS(524288)};",
        "LCLASS(81920),  LCLASS(98304),  LCLASS(114688), LCLASS(131072), LCLASS(163840), LCLASS(196608), LCLASS(229376)\n"
        "#ifndef FEX_IOS_HOST\n"
        "    , LCLASS(262144), LCLASS(327680), LCLASS(393216), LCLASS(458752), LCLASS(524288)\n"
        "#endif\n};",
    )
    return marker, tuple(edits)


def patch(source, span_mb=8):
    marker, edits = edits_for(span_mb)
    if "madeira-bcd: coherent " in source:
        if marker in source and all(source.count(after) == 1 for _, after in edits):
            return source
        raise ValueError("partial, changed or different span overlay")
    for before, _ in edits:
        if source.count(before) != 1:
            raise ValueError("rpmalloc anchor changed: " + before[:100])
    for before, after in edits:
        source = source.replace(before, after, 1)
    return source


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--span-mb", type=int, choices=(4, 8), default=8)
    args = parser.parse_args()
    path = args.source
    source = path.read_text()
    try:
        result = patch(source, args.span_mb)
    except ValueError as error:
        sys.exit("patch-fex-ios-rpmalloc-span8: " + str(error))
    if result != source:
        path.write_text(result)
    print(f"rpmalloc iOS: span/alignment/large page={args.span_mb}MiB, "
          f"classes<={args.span_mb * 7 / 8:g}MiB; larger requests use the huge path")
