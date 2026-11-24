"""Parse a SCIP symbol's descriptor into its ordered components -- the reliable, cross-indexer way to learn
a symbol's KIND, NAME, and CONTAINMENT hierarchy for the generic importer (`core.scip_import`).

A SCIP symbol is `<scheme> <manager> <package> <version> <descriptor>` (the first four are space-delimited;
the descriptor is the rest). The descriptor is a sequence of components, each a name + a suffix that encodes
its kind (scip-code.org's symbol grammar):

    namespace      name '/'                  package / module / file-as-module
    type           name '#'                  class / interface / struct / enum
    term           name '.'                  field / const / variable / (Go interface method spec)
    method         name '(' <disambig> ')' '.'   function / method (an overload hash may fill the parens)
    type-parameter '[' name ']'
    parameter      '(' name ')'
    meta           name ':'
    macro          name '!'

`name` is a plain identifier OR a backtick-escaped one (``\\`other.ts\\```, ``\\`~Shape\\```, ``\\`<file>\\```),
which may itself contain the suffix characters -- so parsing must respect backtick quoting (an internal
backtick is doubled). We decode kind + name from the descriptor rather than `SymbolInformation.kind` /
`display_name`, which indexers populate inconsistently (scip-clang omits kind; scip-typescript leaves
display_name empty). Each returned component carries the descriptor OFFSET just past it, so the importer can
rebuild the full symbol of any ANCESTOR (the parent type of a method, say) to key a CONTAINS edge onto the
exact node the ancestor's own definition authored."""

from __future__ import annotations

from dataclasses import dataclass

# component kinds (the suffix grammar above)
NAMESPACE = "namespace"
TYPE = "type"
TERM = "term"
METHOD = "method"
TYPE_PARAMETER = "type_parameter"
PARAMETER = "parameter"
META = "meta"
MACRO = "macro"

_SUFFIX_CHARS = "/#(.:![])"  # a bare name ends at the first of these (incl. the CLOSING `]`/`)` so a
#                              type-parameter `[T]` / parameter `(x)` name doesn't swallow its delimiter)


@dataclass(frozen=True)
class Descriptor:
    name: str   # the component's name, backtick-escaping unwrapped
    kind: str   # one of the module constants above
    end: int    # offset in the DESCRIPTOR string just past this component (for ancestor-symbol rebuilding)


def is_local(symbol: str) -> bool:
    """A `local <id>` symbol -- function-scoped, not a project-global definition; the importer skips these."""
    return symbol.startswith("local ")


def split_symbol(symbol: str) -> tuple[str, str]:
    """(scheme prefix, descriptor): the prefix is the four header fields + the trailing separator space, so
    `prefix + descriptor == symbol` (the prefix is returned verbatim, so `ancestor_symbol` stays exact).
    SCIP escapes a literal space inside a header field by DOUBLING it, so only every UNescaped space is a
    field separator -- a package name like `my  package` must not be mis-split. Fewer than four separators
    (malformed / bare, e.g. `local 0`) yields ("", symbol)."""
    seps, i, n = 0, 0, len(symbol)
    while i < n:
        if symbol[i] == " ":
            if i + 1 < n and symbol[i + 1] == " ":  # doubled -> an escaped literal space, not a separator
                i += 2
                continue
            seps += 1
            if seps == 4:
                return symbol[: i + 1], symbol[i + 1 :]
        i += 1
    return "", symbol


def parse(symbol: str) -> list[Descriptor]:
    """The descriptor components of `symbol`, outermost first. [] for a local or an empty/malformed symbol."""
    if not symbol or is_local(symbol):
        return []
    _, descriptor = split_symbol(symbol)
    return _parse_descriptor(descriptor)


def ancestor_symbol(symbol: str, component: Descriptor) -> str:
    """The full SCIP symbol of an ancestor `component` of `symbol` -- the scheme prefix + the descriptor up to
    and including that component. Keys a CONTAINS/parent edge onto the exact node the ancestor's own definition
    created (whose id is the ancestor's full symbol)."""
    prefix, descriptor = split_symbol(symbol)
    return prefix + descriptor[: component.end]


def _read_name(d: str, i: int) -> tuple[str, int]:
    """Read a name at offset i: a backtick-escaped name (internal backtick doubled) or a bare run up to the
    next suffix character. Returns (name, offset after the name)."""
    if i < len(d) and d[i] == "`":
        j = i + 1
        buf: list[str] = []
        while j < len(d):
            if d[j] == "`":
                if j + 1 < len(d) and d[j + 1] == "`":  # doubled -> a literal backtick inside the name
                    buf.append("`")
                    j += 2
                    continue
                return "".join(buf), j + 1  # closing backtick consumed
            buf.append(d[j])
            j += 1
        return "".join(buf), j  # malformed: no closing backtick
    j = i
    while j < len(d) and d[j] not in _SUFFIX_CHARS:
        j += 1
    return d[i:j], j


def _find_unquoted(d: str, i: int, close: str) -> int:
    """Index of the next `close` char at/after i that is NOT inside a backtick-quoted name (where an internal
    backtick is doubled) -- so a closing `]`/`)` embedded in a quoted name doesn't end the component early.
    -1 if none."""
    while i < len(d):
        if d[i] == "`":
            i += 1
            while i < len(d):
                if d[i] == "`":
                    if i + 1 < len(d) and d[i + 1] == "`":  # doubled -> literal backtick inside the name
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if d[i] == close:
            return i
        i += 1
    return -1


def _parse_descriptor(d: str) -> list[Descriptor]:
    out: list[Descriptor] = []
    i, n = 0, len(d)
    while i < n:
        if d[i] == "[":  # type-parameter `[name]` -- a standalone component, no preceding name
            j = _find_unquoted(d, i + 1, "]")
            end = (j + 1) if j != -1 else n
            name, _ = _read_name(d, i + 1)
            out.append(Descriptor(name, TYPE_PARAMETER, end))
            i = end
            continue
        if d[i] == "(":  # parameter `(name)` -- standalone, no preceding name
            j = _find_unquoted(d, i + 1, ")")
            end = (j + 1) if j != -1 else n
            name, _ = _read_name(d, i + 1)
            out.append(Descriptor(name, PARAMETER, end))
            i = end
            continue
        name, i = _read_name(d, i)
        if i >= n:
            if name:  # trailing name with no suffix -> best-effort term
                out.append(Descriptor(name, TERM, n))
            break
        c = d[i]
        if c == "/":
            out.append(Descriptor(name, NAMESPACE, i + 1))
            i += 1
        elif c == "#":
            out.append(Descriptor(name, TYPE, i + 1))
            i += 1
        elif c == "(":  # method `name(<disambiguator>).` -- the parens may hold an overload hash
            close = _find_unquoted(d, i + 1, ")")
            i = (close + 1) if close != -1 else n
            if i < n and d[i] == ".":  # the mandatory trailing dot
                i += 1
            out.append(Descriptor(name, METHOD, i))
        elif c == ".":
            out.append(Descriptor(name, TERM, i + 1))
            i += 1
        elif c == ":":
            out.append(Descriptor(name, META, i + 1))
            i += 1
        elif c == "!":
            out.append(Descriptor(name, MACRO, i + 1))
            i += 1
        else:  # unexpected -- skip one char, stay robust on malformed input
            i += 1
    return out
