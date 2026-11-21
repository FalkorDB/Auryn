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
independent -- so a line-based containment join needs no column handling. Column-precise per-call-site
reconciliation (disambiguating multiple calls on one line) is a shared follow-up (scip-code.org v2)."""

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


def _is_callable_symbol(symbol: str) -> bool:
    """A SCIP symbol that names a function/method (its descriptor ends `().`). Excludes params
    (`...().(x)`), types (`...#`), terms/fields and arrow-function consts (`...name.`), locals
    (`local N`), and macros. (Arrow-function consts emit a term descriptor, so they're conservatively
    not deep-upgraded -- their fast name-edges are kept.)"""
    return symbol.endswith("().")


def _simple_name(symbol: str) -> str:
    """The callee's bare name from a SCIP symbol:
    `rust-analyzer cargo crate 1.0 Fast#Run#run().` -> `run`,
    `scip-typescript npm pkg 1.0 src/`lib.ts`/Fast#run().` -> `run`.
    Takes the descriptor (the 5th space-separated field), strips the `().` callable suffix, keeps the last
    `#`/`/`-delimited component, and unwraps SCIP's backtick-escaping (filenames and names with special
    characters are backtick-wrapped, e.g. `<constructor>`). A backtick'd filename holds no `#`/`/`, so the
    split is safe."""
    descriptor = symbol.split(" ", 4)[-1]
    descriptor = descriptor.removesuffix("().")
    last = re.split(r"[#/]", descriptor)[-1] if descriptor else ""
    return last.strip("`")


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _span(loc: CallableLocation) -> int:
    return loc.line_end - loc.line_start


def resolve_calls(scip_bytes: bytes, locations: list[CallableLocation]) -> DeepCalls:
    """Derive precise intra-project CALLS from a SCIP index + the fast pass's callable locations.

    A reference occurrence of a callable symbol is attributed to a containing callable (preferring one
    whose fast parse actually called that name, so same-line siblings disambiguate) and resolved to the
    in-source callable the symbol defines (`resolution:"resolved"`). A reference whose symbol isn't
    defined in this project (stdlib / other package) is counted but gets no edge -- the fast pass's
    extern:// placeholder covers it. A symbol whose definition the line-only join can't disambiguate (2+
    same-named callables on one line) is left UNRESOLVED -- no edge, not counted as identified, and the
    caller's fast edge to it is kept. `covered_ids` are the callables SCIP analyzed, so the overlay
    touches only those; `seen_by_caller` drives both the unresolved recompute and which fast name-edges
    the overlay drops."""
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
            if len(hits) >= 2 and _span(hits[0]) == _span(hits[1]):
                ambiguous_defs.add(occ.symbol)  # can't tell B.run from A.run on one line -> don't risk it
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
            caller = attribute(locs, occ.start_line + 1, callee_name)
            if caller is None:
                metrics["unattributed"] += 1
                continue
            target = symbol_to_loc.get(occ.symbol)
            if target is None:
                if occ.symbol in ambiguous_defs:
                    metrics["ambiguous"] += 1  # NOT identified: counts as unresolved, fast edge kept
                else:
                    seen_by_caller[caller.id].add(callee_name)  # identified external (stdlib / other package)
                    metrics["external"] += 1
                continue
            seen_by_caller[caller.id].add(callee_name)  # SCIP tied a concrete in-source callable to this call
            if target.id == caller.id:
                continue  # self-reference / recursion (the fast passes skip self-CALLS too)
            key = (caller.id, target.id)
            if key not in edges:
                edges[key] = GraphEdge(
                    "CALLS", caller.label, {"id": caller.id}, target.label, {"id": target.id},
                    {"resolution": "resolved"},
                )
                metrics["resolved"] += 1
    return DeepCalls(list(edges.values()), dict(seen_by_caller), covered_ids, metrics)
