"""Generic SCIP -> precise CALLS derivation, shared by every language's deep pass (rust-analyzer,
scip-typescript, and future scip-java/scip-go/...). The fast tree-sitter passes resolve CALLS *by
name*, so a method on an untyped/`dyn`/generic receiver, or a cross-module/crate callee, is dropped or
guessed. A SCIP indexer resolves them precisely; this module turns a SCIP index + the fast pass's
callable locations into resolved CALLS edges by **occurrence containment**: a *reference* occurrence of
a callable symbol that falls inside a callable's line span is a call from that callable to the referenced
one. Node ids come from the fast pass (`CallableLocation`) -- ids embed only syntactic signatures, so a
deep edge lands on the exact Function/Method node the fast pass authored, with no type inference here.

Language-agnostic by construction: it reads only the standardized SCIP symbol grammar (scip-code.org) --
the 5-part `<scheme> <manager> <package> <version> <descriptor>` string, callables' descriptors ending
`().` -- which every SCIP indexer emits. The per-language runner (producing the `index.scip` bytes) and
the `CallableLocation` bridge live in each language's ingestor; everything here is shared.

The join is (file, line)-granular: SCIP occurrence columns are indexer-specific (scip-typescript emits
UTF-16 code units, rust-analyzer UTF-8), but the first range element is a plain line number, encoding-
independent -- so a line-based containment join needs no column handling. A language that records each
call site's name+line (`CallableLocation.called_sites`) additionally tells a real call from a value
reference via `resolve_calls(require_call_site=True)` -- a reference to a callable that sits on a line with
no syntactic call of that name (`return helper`) is a value use, not a call. What a line alone CANNOT do
is recognize an aliased call (`import {helper as h}; h()` -- SCIP names it `helper`, the fast parse `h`)
or disambiguate two calls to the same name on one line: both need occurrence COLUMNS to tie a SCIP
occurrence to a specific call site (scip-code.org v2, tracked in #802)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from auryn.core import scip
from auryn.core.graph_model import GraphEdge


@dataclass(frozen=True)
class CallableLocation:
    """Where a project callable lives, keyed by its graph node id -- the join the deep (SCIP) pass needs:
    SCIP reports a resolved call by (file, line); this maps that back to the fast pass's Function/Method
    node, and its [line_start, line_end] span attributes a call site to the enclosing callable by
    containment. A language may subclass this to carry extra per-callable signal it needs when recomputing
    its own honesty metrics (e.g. Rust's macro call count) -- `resolve_calls` reads only the fields here."""

    id: str
    label: str  # "Function" | "Method" -- the node label the deep CALLS edge must point at
    rel_path: str
    line_start: int  # 1-based inclusive (as the fast parsers record them)
    line_end: int
    name: str
    called_names: frozenset[str]  # callee simple names the fast parse saw as CALLS in this body -- lets the
    # deep pass disambiguate same-line callers and recompute unresolved_calls from what SCIP resolved
    called_sites: frozenset[tuple[str, int]] = frozenset()
    """(callee name, callee-identifier line) for EVERY syntactic call the fast parse saw in this body,
    1-based -- the line-precise upgrade of `called_names`. Lets `resolve_calls(require_call_site=True)` tell a
    real call from a value reference by the exact (name, line): `return helper` carries no call site on its
    line, so SCIP's reference to `helper` there is not emitted as a call. Must record every call site's line
    (a call to the same name on two lines needs both). Empty when a language hasn't adopted call-site
    recording -- then the coarser `require_syntactic_call` name gate applies instead. The line is the callee
    *identifier's* line (what SCIP anchors an occurrence to), not the call expression's first line, so a
    multi-line call matches. (Recognizing an ALIASED call, where the SCIP name differs from the fast name,
    needs occurrence columns -- see the module docstring and #802.)"""


@dataclass
class DeepCalls:
    """The result of resolving CALLS from a SCIP index against the fast pass's callables."""

    edges: list[GraphEdge]               # resolved intra-project CALLS (resolution="resolved")
    seen_by_caller: dict[str, set[str]]  # caller id -> callee names SCIP IDENTIFIED (resolved in-source OR
    # external). Drives unresolved_calls AND tells the overlay which fast name-CALLS to drop: an identified
    # call is either replaced by a deep edge (in-source) or was a wrong local guess for an external callee
    # (drop it) -- while a NON-identified call (ambiguous same-line def / fn-pointer) keeps its fast edge.
    covered_ids: set[str]                # callable ids SCIP defined (analyzed) -- the overlay scope
    metrics: dict


# A method/function descriptor in SCIP is `<name> '(' <disambiguator>? ')' '.'` -- it always ends `).`.
# The disambiguator is empty for rust-analyzer / scip-typescript (`name().`) but scip-clang packs an
# overload hash into it (`name(9b79fb6aee4c0440).`). Params end `)` (no dot), terms/arrow-consts `.`,
# types `#`, locals `local N` -- all correctly excluded by the `).` test / stripped by the regex.
_METHOD_SUFFIX = re.compile(r"\([^()]*\)\.$")


def _is_callable_symbol(symbol: str) -> bool:
    """A SCIP symbol that names a function/method: its descriptor ends `).` (an optional overload
    disambiguator sits inside the parens). Excludes parameters (`...(x)` -- ends `)`, no dot), types
    (`...#`), terms/fields and arrow-function consts (`...name.`), and locals (`local N`). (Arrow-function
    consts emit a term descriptor, so they're conservatively not deep-upgraded -- their fast name-edges are
    kept.)"""
    return symbol.endswith(").")


def _simple_name(symbol: str) -> str:
    """The callee's bare name from a SCIP symbol, across indexers:
    `rust-analyzer cargo crate 1.0 Fast#Run#run().`          -> `run`
    `scip-typescript npm pkg 1.0 src/`lib.ts`/Fast#run().`   -> `run`
    `cxx . pkg v0$ arithmetic/multiply(9b79fb6aee4c0440).`   -> `multiply`
    `scip-typescript npm pkg 1.0 src/`m.ts`/C#`#priv`().`     -> `#priv`  (TS private member)
    Takes the descriptor (the 5th space-separated field), strips the trailing `(<disambiguator>?).` method
    group, then returns the last `#`/`/`-delimited component with SCIP's backtick-escaping unwrapped. The
    split respects backticks: a quoted name (a filename, `<constructor>`, `#priv`, `operator()`) may itself
    contain `#`/`/`/`.`, so it is never split."""
    descriptor = _METHOD_SUFFIX.sub("", symbol.split(" ", 4)[-1])
    return _last_descriptor_component(descriptor)


def _last_descriptor_component(descriptor: str) -> str:
    """The final `#`/`/`-delimited component of a SCIP descriptor, honoring backtick-escaping (a quoted
    name is wrapped in backticks, with internal backticks doubled, and may contain the separators)."""
    if not descriptor:
        return ""
    if descriptor.endswith("`"):  # a backtick-quoted final component: find its matching opening quote
        i = len(descriptor) - 1
        j = i - 1
        while j >= 0:
            if descriptor[j] == "`":
                k = j
                while k >= 0 and descriptor[k] == "`":
                    k -= 1
                if (j - k) % 2 == 1:  # odd run of backticks ending here -> the opening quote
                    return descriptor[k + 2 : i].replace("``", "`")
                j = k
            else:
                j -= 1
        return descriptor.strip("`")  # malformed -- best-effort
    for idx in range(len(descriptor) - 1, -1, -1):  # bare final component: tail after the last #/ /
        if descriptor[idx] in "#/":
            return descriptor[idx + 1 :]
    return descriptor


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _span(loc: CallableLocation) -> int:
    return loc.line_end - loc.line_start


def resolve_calls(scip_bytes: bytes, locations: list[CallableLocation],
                  *, require_syntactic_call: bool = False, require_call_site: bool = False,
                  emit_self_calls: bool = False) -> DeepCalls:
    """Derive precise intra-project CALLS from a SCIP index + the fast pass's callable locations.

    A reference occurrence of a callable symbol is attributed to a containing callable (preferring one
    whose fast parse actually called that name, so same-line siblings disambiguate) and resolved to the
    in-source callable the symbol defines (`resolution:"resolved"`). A reference whose symbol isn't
    defined in this project (stdlib / other package) is counted but gets no edge -- the fast pass's
    extern:// placeholder covers it. A symbol whose definition the line-only join can't disambiguate (2+
    same-named callables on one line) is left UNRESOLVED -- no edge, not counted as identified, and the
    caller's fast edge to it is kept. `covered_ids` are the callables SCIP analyzed, so the overlay
    touches only those; `seen_by_caller` drives both the unresolved recompute and which fast name-edges
    the overlay drops.

    `require_call_site`: the line-precise successor to `require_syntactic_call`. When True, only emit an edge
    when the fast parse recorded a syntactic call of that exact name on the reference's line
    (`(callee_name, ref_line) in caller.called_sites`). This filters value references precisely -- `return
    helper` on its own line carries no call site there, so SCIP's reference to `helper` is not a call -- and,
    unlike the body-wide name gate, it drops a value reference to a callable whose name happens to match a
    call elsewhere in the body. It does NOT recognize an aliased call (SCIP's name differs from the fast
    name) -- that needs occurrence columns (#802); such a reference is simply left to the fast pass. Requires
    each caller to populate `called_sites`; it takes precedence over `require_syntactic_call`. A gated-out
    reference is a value use, so it is dropped entirely -- not counted, and NOT added to `seen_by_caller`
    (which would otherwise make the overlay delete the caller's fast edge of that name with no replacement).

    `require_syntactic_call`: the coarser name-only gate (pre-`called_sites`). When True, only emit an edge
    when the callee's name is one the fast parse recorded as a CALL anywhere in that caller's body
    (`callee in caller.called_names`). A SCIP reference is NOT always a call -- `return helper` /
    `callback(helper)` / `typeof helper` reference a callable as a VALUE -- so without a gate those become
    spurious CALLS edges. Languages that rely on SCIP to surface calls the fast pass can't see at all (Rust
    macro expansions) leave both gates False -- then every reference is treated as a call (nothing is gated,
    so `seen_by_caller` records them all).

    `emit_self_calls`: when True, a call resolved to the ENCLOSING callable yields a self-edge (recursion).
    Languages whose fast pass emits self-CALLS (TypeScript) pass True so the overlay can replace a fast
    recursive edge with a precise one AND so real recursion survives even when a duplicate-named fast guess
    was dropped; languages whose fast pass omits self-CALLS (Rust) leave it False to stay consistent."""
    documents = scip.parse_index(scip_bytes)
    by_path: dict[str, list[CallableLocation]] = defaultdict(list)
    for loc in locations:
        by_path[_norm_path(loc.rel_path)].append(loc)

    def attribute(locs, line_1based, callee_name):  # a reference -> its enclosing CALLER
        hits = [loc for loc in locs if loc.line_start <= line_1based <= loc.line_end]
        # prefer a containing callable that the fast parse saw call this name -- disambiguates callers
        # sharing a line; fall back to innermost so a call the fast parse missed still attributes.
        pool = [loc for loc in hits if callee_name in loc.called_names] or hits
        return min(pool, key=_span) if pool else None

    symbol_to_loc: dict[str, CallableLocation] = {}
    ambiguous_defs: set[str] = set()  # same-line same-name defs we couldn't pin -> leave to the fast pass
    for doc in documents:
        locs = by_path.get(_norm_path(doc.relative_path), [])
        for occ in doc.occurrences:
            if not (occ.is_definition and _is_callable_symbol(occ.symbol)):
                continue
            hits = sorted((loc for loc in locs if loc.line_start <= occ.start_line + 1 <= loc.line_end
                           and loc.name == _simple_name(occ.symbol)), key=_span)
            if not hits:
                continue
            if len(hits) >= 2:
                # 2+ same-named callables span this definition line -- the line-only join can't tell which
                # symbol defines which (whatever their body lengths), so leave them to the fast pass rather
                # than risk mapping both symbols to the shortest-span location.
                ambiguous_defs.add(occ.symbol)
            else:
                symbol_to_loc[occ.symbol] = hits[0]
    covered_ids = {loc.id for loc in symbol_to_loc.values()}

    edges: dict[tuple[str, str], GraphEdge] = {}
    seen_by_caller: dict[str, set[str]] = defaultdict(set)
    metrics = {"resolved": 0, "external": 0, "ambiguous": 0, "unattributed": 0}
    for doc in documents:
        locs = by_path.get(_norm_path(doc.relative_path), [])
        for occ in doc.occurrences:
            if occ.is_definition or not _is_callable_symbol(occ.symbol):
                continue
            callee_name = _simple_name(occ.symbol)
            ref_line = occ.start_line + 1
            caller = attribute(locs, ref_line, callee_name)
            if caller is None:
                metrics["unattributed"] += 1
                continue
            # Gate value references out BEFORE any side effect. `seen_by_caller` drives which fast name-edges
            # the overlay DROPS, so a value use (`return helper`) that reached it would delete a real fast
            # edge -- with no deep replacement if SCIP couldn't resolve the actual call elsewhere. A filtered
            # reference must therefore not be seen, counted, or emitted.
            if require_call_site:
                if (callee_name, ref_line) not in caller.called_sites:
                    continue  # no syntactic call to this name on this line -> a value reference, not a call
            elif require_syntactic_call and callee_name not in caller.called_names:
                continue  # a reference, not a call (value use: `return helper` / `callback(helper)`)
            target = symbol_to_loc.get(occ.symbol)
            if target is None:
                if occ.symbol in ambiguous_defs:
                    metrics["ambiguous"] += 1  # NOT identified: counts as unresolved, fast edge kept
                else:
                    seen_by_caller[caller.id].add(callee_name)  # identified external (stdlib / other package)
                    metrics["external"] += 1
                continue
            seen_by_caller[caller.id].add(callee_name)  # SCIP tied a concrete in-source callable to this call
            if target.id == caller.id and not emit_self_calls:
                continue  # self-reference / recursion -- skipped only where the fast pass omits self-CALLS
            key = (caller.id, target.id)
            if key not in edges:
                edges[key] = GraphEdge(
                    "CALLS", caller.label, {"id": caller.id}, target.label, {"id": target.id},
                    {"resolution": "resolved"},
                )
                metrics["resolved"] += 1
    return DeepCalls(list(edges.values()), dict(seen_by_caller), covered_ids, metrics)
