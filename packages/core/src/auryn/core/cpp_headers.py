"""Disambiguate `.h` headers between C and C++ by content.

`.h` is the dominant header extension in real C++ (protobuf, LLVM, Chromium, Abseil, most
Google-style C++ all use `.h`, not `.hpp`), yet it is also the C header extension. The two are
routed to different ingestors (C's tree-sitter-c vs C++'s tree-sitter-cpp). Sending a C++ header to
the C ingestor loses the whole class -- a class declared in a `.h` is mangled (the C grammar can't
parse `namespace`/`class`/`template`), its methods/fields vanish, and the declaration never joins its
`.cc` definition (different `c://` vs `cpp://` id schemes). See the demo in the C++-routing design.

The disambiguation is a cheap content sniff for C++-only tokens. It is deliberately GENEROUS toward
C++: the C++ tree-sitter grammar is a superset of C, so routing a C-compatible header to C++ parses
fine, whereas the reverse mangles -- a false positive is harmless, a false negative is a data loss.
A header with NONE of these tokens is C-compatible either way, so it defaults to C (which keeps
pure-C and vendored-C-in-C++ headers paired with their `.c` definitions). Content-based => the same
file always classifies the same way, so a header's owning ingestor never flips between a full ingest
and an incremental resync.
"""

from __future__ import annotations

import re

# Comments and string/char literals are stripped BEFORE sniffing -- otherwise `::` in a doc comment
# (`transport.c::foo()`), a `printf("%s::%s")`, or the word "namespace"/"operator" in prose all
# false-positive a pure-C header into C++ (observed on git). Standard unambiguous patterns, no backtracking.
_STRIP = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.DOTALL)

# C++-only constructs in CODE (after stripping) that a C compiler rejects. Deliberately DEFINITION-shaped
# and conservative, so a token that is also a legal C identifier/label/bitfield can't trip it: bare
# `typename`/`virtual`/`nullptr` (valid C identifiers -- git has `char *typename;`) and `public:` (a legal
# C goto label / bitfield name) are NOT markers. `::` after stripping is impossible in C and the workhorse
# signal; the rest catch a C++ header that happens to use no scope resolution.
_CPP_MARKERS = re.compile(
    r"""(?x)
      ::                                                       # scope resolution -- impossible in C
    | \btemplate\s*<                                           # template definition/use
    | \bnamespace\s+\w+\s*\{                                   # named namespace definition
    | \bnamespace\s*\{                                         # anonymous namespace
    | \busing\s+namespace\b
    | \bclass\s+\w+\s*(?:final\b\s*)?[:{]                      # class definition (`class X {` / `class X :`)
    | \bstruct\s+\w+\s*(?:final\b\s*)?:                        # struct INHERITANCE (`struct X : Base`) -- C++
                                                               #   only; `struct X {` is valid C so NOT a marker
    | \b(?:static_cast|dynamic_cast|reinterpret_cast|const_cast)\s*<
    | \boperator\s*(?:\[\]|\(\)|<=>|<<=?|>>=?|<=|>=|==|!=|&&|\|\||\+\+|--|->\*?
                     |[-+*/%^&|~!<>=]=?|new\b|delete\b)\s*\(   # operator FUNCTION: an operator token then `(`.
                                                               #   The trailing `(` is what rejects a C identifier
                                                               #   named `operator` -- `calc(int operator)` (op then
                                                               #   `)`) and `int operator = 1;` (op then `=`, no `(`).
    | \bvirtual\s+[\w~]                                        # `virtual` specifier before a return type / dtor --
                                                               #   `virtual` then whitespace+identifier can't be a C
                                                               #   decl (a C var `virtual` is `int virtual;`, no
                                                               #   trailing identifier).
    | \)\s*=\s*(?:default|delete)\b                           # =default / =delete member -- C has neither. `= 0`
                                                               #   is NOT a marker: it also matches a C function-
                                                               #   pointer init `void (*cb)(int) = 0;`. A pure-virtual
                                                               #   `virtual int f() = 0;` is caught by the `virtual`
                                                               #   marker instead, so dropping `0` loses no C++ case.
    | \bstruct\s+(\w+)\b[^{;]*\{[^{}]*\b\1\s*\(                # a struct with a CONSTRUCTOR (a member named the
                                                               #   struct itself) -- C++ only; `struct X { X(); ... }`.
                                                               #   `[^{}]*` keeps it inside the (flat) body so a later
                                                               #   `X obj(...)` construction elsewhere can't match.
    """
)

HEADER_SUFFIX = ".h"


def is_cpp_header(text: str) -> bool:
    """True if a `.h`'s content uses a C++-only construct (so it must go to the C++ ingestor). Comments
    and string/char literals are stripped first so prose/strings can't false-positive a C header."""
    return bool(_CPP_MARKERS.search(_STRIP.sub(" ", text)))


def is_cpp_header_blob(blob: bytes | str) -> bool:
    """`is_cpp_header` for a raw file blob (bytes from the source), decoded leniently."""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    return is_cpp_header(blob)


def cpp_header_paths(source, target_sha: str, excluded_dir_names: set[str]) -> frozenset[str]:
    """The `.h` paths at `target_sha` whose content is C++ (so they belong to the C++ ingestor, not C).
    Reads each header blob once; used by the ingestion orchestrator's language detection. Each ingestor's
    own walk re-applies `is_cpp_header_blob` to the blob it already reads, so no shared state is needed."""
    out: set[str] = set()
    for path, blob_sha in source.list_files(target_sha, HEADER_SUFFIX, excluded_dir_names):
        try:
            if is_cpp_header_blob(source.read_blob(blob_sha)):
                out.add(path)
        except Exception:
            continue  # unreadable blob -> leave with C (the conservative default)
    return frozenset(out)
