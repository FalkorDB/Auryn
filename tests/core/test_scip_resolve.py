"""The shared SCIP -> CALLS resolver (`core.scip_resolve`), driven at the core level with a synthetic
`index.scip` built by a tiny in-test encoder -- no indexer toolchain, no language fixture. This pins the
language-agnostic gate behavior the per-language deep passes rely on: the line-precise `require_call_site`
gate (a reference is a call only on a line that has a syntactic call of that name) versus the coarser
`require_syntactic_call` name gate. Each language's own deep test covers its CallableLocation bridge
against a real captured index."""

from __future__ import annotations

from auryn.core.scip_resolve import CallableLocation, resolve_calls

# -- a minimal SCIP protobuf encoder (inverse of core.scip's reader) -----------------------------
# Mirrors the wire layout core.scip decodes: Index.documents(2) / Document{relative_path(1),
# occurrences(2)} / Occurrence{range(1) packed [line,startCol,endCol], symbol(2), symbol_roles(3)}.


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _len_field(field_num: int, payload: bytes) -> bytes:
    return _varint((field_num << 3) | 2) + _varint(len(payload)) + payload


def _varint_field(field_num: int, value: int) -> bytes:
    return _varint((field_num << 3) | 0) + _varint(value)


def _occ(symbol: str, line0: int, *, is_def: bool = False, col: int = 0) -> bytes:
    packed = _varint(line0) + _varint(col) + _varint(0)  # [line, startCol, endCol]
    body = _len_field(1, packed) + _len_field(2, symbol.encode())
    if is_def:
        body += _varint_field(3, 0x1)  # SYMBOL_ROLE_DEFINITION
    return body


def _index(path: str, occs: list[bytes]) -> bytes:
    doc = _len_field(1, path.encode()) + b"".join(_len_field(2, o) for o in occs)
    return _len_field(2, doc)


def _sym(name: str, pkg_path: str = "") -> str:
    # a callable descriptor -- ends `).`; pkg_path lets two symbols share a simple name (`dup` vs `sub/dup`)
    return f"scip x pkg v {pkg_path}{name}()."


_FILE = "src/m.ts"


def _loc(node_id: str, name: str, line_start: int, line_end: int,
         sites: set[tuple[str, int]] = frozenset(),
         cols: set[tuple[int, int]] = frozenset()) -> CallableLocation:
    return CallableLocation(node_id, "Function", _FILE, line_start, line_end, name,
                            frozenset(n for n, _ in sites), frozenset(sites), frozenset(cols))


def _edges(locations: list[CallableLocation], occs: list[bytes], **kw) -> set[tuple[str, str]]:
    return {(e.from_key["id"], e.to_key["id"])
            for e in resolve_calls(_index(_FILE, occs), locations, **kw).edges}


def test_require_call_site_keeps_real_calls_and_filters_value_references():
    # `caller` calls `target()` on line 3 and references `other` as a value on line 10 (never called).
    caller = _loc("caller", "caller", 1, 12, {("target", 3)})
    target = _loc("target", "target", 20, 21)
    other = _loc("other", "other", 30, 31)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("target"), 2),            # line 3 -- real call
        _occ(_sym("other"), 9),             # line 10 -- `return other`, a value use
        _occ(_sym("target"), 19, is_def=True),
        _occ(_sym("other"), 29, is_def=True),
    ]
    locs = [caller, target, other]
    assert _edges(locs, occs, require_call_site=True) == {("caller", "target")}
    # the baseline the gate fixes: ungated, the value reference becomes a spurious edge
    assert _edges(locs, occs) == {("caller", "target"), ("caller", "other")}


def test_require_call_site_does_not_emit_a_function_valued_argument_as_a_call():
    # `callback(helper)` on line 5: the fast parse records the CALL `callback`, and `helper` is a value
    # argument. SCIP reports both on line 5 -> `callback` is the call, `helper` must NOT become an edge.
    caller = _loc("caller", "caller", 1, 10, {("callback", 5)})
    callback = _loc("callback", "callback", 20, 21)
    helper = _loc("helper", "helper", 30, 31)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("callback"), 4),          # line 5 -- the call
        _occ(_sym("helper"), 4),            # line 5 -- a function-valued argument, not a call
        _occ(_sym("callback"), 19, is_def=True),
        _occ(_sym("helper"), 29, is_def=True),
    ]
    pairs = _edges([caller, callback, helper], occs, require_call_site=True)
    assert ("caller", "callback") in pairs      # the real call
    assert ("caller", "helper") not in pairs     # the value argument -- not a call


def test_require_call_site_is_stricter_than_the_body_wide_name_gate():
    # `caller` calls `dup` (-> dupA) on line 4, and references a DIFFERENT same-named `dup` (-> dupB) as a
    # value on line 8. The body-wide name gate admits both (the name `dup` is called somewhere); the
    # line-precise gate admits only the real call -- line 8 has no `dup` call site.
    caller = _loc("caller", "caller", 1, 10, {("dup", 4)})
    dup_a = _loc("dupA", "dup", 20, 21)
    dup_b = _loc("dupB", "dup", 30, 31)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("dup"), 3),                       # line 4 -- real call -> dupA
        _occ(_sym("dup", "sub/"), 7),               # line 8 -- value use of a same-named dupB
        _occ(_sym("dup"), 19, is_def=True),
        _occ(_sym("dup", "sub/"), 29, is_def=True),
    ]
    locs = [caller, dup_a, dup_b]
    assert _edges(locs, occs, require_call_site=True) == {("caller", "dupA")}
    assert _edges(locs, occs, require_syntactic_call=True) == {("caller", "dupA"), ("caller", "dupB")}


def test_require_call_site_needs_every_call_site_line_recorded():
    # A call to `target` only on line 8. If the fast parse recorded that site's line, the SCIP reference on
    # line 8 resolves; if a name-only dedup had dropped it (keeping only an earlier line), it would be lost.
    target = _loc("target", "target", 20, 21)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("target"), 7),            # line 8 -- the call
        _occ(_sym("target"), 19, is_def=True),
    ]
    recorded = _loc("caller", "caller", 1, 12, {("target", 8)})
    assert _edges([recorded, target], occs, require_call_site=True) == {("caller", "target")}
    dropped = _loc("caller", "caller", 1, 12, {("target", 3)})  # the site's real line 8 was lost
    assert _edges([dropped, target], occs, require_call_site=True) == set()


def test_require_call_site_does_not_record_a_gated_value_reference_as_seen():
    # `seen_by_caller` drives which fast name-edges the overlay DROPS. A value use of `run` (resolved by
    # SCIP to an in-source method, but on a line with no `run` call site) must NOT enter seen_by_caller --
    # otherwise the overlay would delete the caller's fast `run` edge with no deep replacement.
    caller = _loc("caller", "caller", 1, 10, set())  # records NO call sites
    run = _loc("run", "run", 20, 21)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("run"), 4),               # line 5 -- `const m = obj.run` value use, not a call
        _occ(_sym("run"), 19, is_def=True),
    ]
    deep = resolve_calls(_index(_FILE, occs), [caller, run], require_call_site=True)
    assert deep.edges == []
    assert "run" not in deep.seen_by_caller.get("caller", set())


def test_resolve_calls_is_empty_without_matching_locations():
    assert resolve_calls(_index(_FILE, [_occ(_sym("x"), 0)]), []).edges == []


# -- column-precise gate (call_cols): aliased calls + same-line disambiguation (#802 v2) ----------

def test_column_precise_resolves_an_aliased_call_that_line_precise_cannot():
    # `import {real as alias}; alias()` -- the fast parse recorded the call as `alias`, scip-ts names the
    # occurrence `real` at `alias`'s position. (name, line) can never match; (line, col) position does.
    real = _loc("real", "real", 20, 25)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("real"), 2, col=5),       # line 3, col 5 -- the aliased call site, symbol named `real`
        _occ(_sym("real"), 19, is_def=True),
    ]
    # column-precise: the fast call site's position (3, 5) matches the occurrence -> edge to the real symbol.
    with_cols = _loc("caller", "caller", 1, 10, sites={("alias", 3)}, cols={(3, 5)})
    assert _edges([with_cols, real], occs, require_call_site=True) == {("caller", "real")}
    # line-precise only (no columns recorded): (real, 3) is not in called_sites={("alias",3)} -> no edge.
    no_cols = _loc("caller", "caller", 1, 10, sites={("alias", 3)})
    assert _edges([no_cols, real], occs, require_call_site=True) == set()


def test_column_precise_splits_a_call_from_a_same_line_value_reference():
    # `a.run(); return b.run` on one line, where a and b have different types -> two DIFFERENT `run` symbols.
    # Only the call (a.run, col 5) is a recorded call site; the value reference (b.run, col 20) is not.
    a_run = _loc("a_run", "run", 20, 21)
    b_run = _loc("b_run", "run", 30, 31)
    occs = [
        _occ(_sym("caller"), 0, is_def=True),
        _occ(_sym("run", "A/"), 2, col=5),    # line 3, col 5 -- the call a.run()
        _occ(_sym("run", "B/"), 2, col=20),   # line 3, col 20 -- the value reference b.run
        _occ(_sym("run", "A/"), 19, is_def=True),
        _occ(_sym("run", "B/"), 29, is_def=True),
    ]
    # column-precise: only (3, 5) is a call site -> the b.run value reference at col 20 is dropped.
    with_cols = _loc("caller", "caller", 1, 10, sites={("run", 3)}, cols={(3, 5)})
    assert _edges([with_cols, a_run, b_run], occs, require_call_site=True) == {("caller", "a_run")}
    # line-precise only: (run, 3) in called_sites passes BOTH refs -> the value reference spuriously resolves.
    no_cols = _loc("caller", "caller", 1, 10, sites={("run", 3)})
    assert _edges([no_cols, a_run, b_run], occs, require_call_site=True) == {("caller", "a_run"), ("caller", "b_run")}
