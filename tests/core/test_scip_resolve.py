"""The shared SCIP -> CALLS resolver (`core.scip_resolve`), driven at the core level with a synthetic
`index.scip` built by a tiny in-test encoder -- no indexer toolchain, no language fixture. This pins the
language-agnostic gate behavior the per-language deep passes rely on: the line-precise `require_call_site`
gate (value references filtered, aliased imports recovered) versus the coarser `require_syntactic_call`
name gate. Each language's own deep test covers its CallableLocation bridge against a real captured index."""

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


def _occ(symbol: str, line0: int, *, is_def: bool = False) -> bytes:
    packed = _varint(line0) + _varint(0) + _varint(0)  # [line, startCol, endCol]; only line is read
    body = _len_field(1, packed) + _len_field(2, symbol.encode())
    if is_def:
        body += _varint_field(3, 0x1)  # SYMBOL_ROLE_DEFINITION
    return body


def _index(path: str, occs: list[bytes]) -> bytes:
    doc = _len_field(1, path.encode()) + b"".join(_len_field(2, o) for o in occs)
    return _len_field(2, doc)


def _sym(name: str) -> str:
    return f"scip x pkg v {name}()."  # a callable descriptor -- ends `).`


_FILE = "src/m.ts"


def _scene() -> tuple[bytes, list[CallableLocation]]:
    """`caller` (lines 1-10) makes: a real call `target()` on line 3; an ALIASED call on line 5 whose
    fast name is `h` but which SCIP names `aliased`; and a VALUE reference `return target` on line 7
    (no call site). `target`/`aliased`/`other` are defined far below. `other` is referenced as a value
    on line 8 but never called."""
    caller = CallableLocation(
        "caller", "Function", _FILE, 1, 10, "caller",
        called_names=frozenset({"target", "h"}),
        called_sites=frozenset({("target", 3), ("h", 5)}),
    )
    defs = [CallableLocation(n, "Function", _FILE, ls, ls + 1, n, frozenset(), frozenset())
            for n, ls in (("target", 20), ("aliased", 30), ("other", 40))]
    occs = [
        _occ(_sym("caller"), 0, is_def=True),   # line 1
        _occ(_sym("target"), 2),                # line 3  -- real call
        _occ(_sym("aliased"), 4),               # line 5  -- aliased call (fast saw `h`)
        _occ(_sym("target"), 6),                # line 7  -- value reference (dedups to the line-3 edge)
        _occ(_sym("other"), 7),                 # line 8  -- value reference, never called
        _occ(_sym("target"), 19, is_def=True),
        _occ(_sym("aliased"), 29, is_def=True),
        _occ(_sym("other"), 39, is_def=True),
    ]
    return _index(_FILE, occs), [caller, *defs]


def _pairs(**kw) -> set[tuple[str, str]]:
    scip_bytes, locs = _scene()
    return {(e.from_key["id"], e.to_key["id"]) for e in resolve_calls(scip_bytes, locs, **kw).edges}


def test_require_call_site_recovers_an_aliased_call_the_name_gate_drops():
    # SCIP names the call `aliased`; the fast parse recorded the alias `h` on the same line. The name gate
    # can't see through the rename; the line gate accepts the sole call on that line.
    call_site = _pairs(require_call_site=True)
    name_gate = _pairs(require_syntactic_call=True)
    assert ("caller", "aliased") in call_site
    assert ("caller", "aliased") not in name_gate


def test_require_call_site_keeps_real_calls_and_filters_value_references():
    pairs = _pairs(require_call_site=True)
    assert ("caller", "target") in pairs        # the real call on line 3
    assert ("caller", "other") not in pairs      # `return other` -- a value, no call site on its line
    # the line-7 `return target` adds no distinct edge (it dedups onto the line-3 call), so no over-count
    assert pairs == {("caller", "target"), ("caller", "aliased")}


def test_ungated_admits_value_references_as_spurious_calls():
    # The baseline the gates exist to fix: with neither gate, every reference becomes a CALLS edge.
    assert _pairs() == {("caller", "target"), ("caller", "aliased"), ("caller", "other")}


def test_require_call_site_is_conservative_when_two_calls_share_the_aliased_line():
    # Two distinct call sites on the aliased line -> the line gate can't tell which the SCIP name maps to,
    # so it declines to recover the alias (the same-line residual that needs occurrence columns, #802).
    scip_bytes, locs = _scene()
    caller = locs[0]
    locs[0] = CallableLocation(
        caller.id, caller.label, caller.rel_path, caller.line_start, caller.line_end, caller.name,
        called_names=caller.called_names | {"g"},
        called_sites=caller.called_sites | {("g", 5)},  # a second call now shares line 5
    )
    pairs = {(e.from_key["id"], e.to_key["id"])
             for e in resolve_calls(scip_bytes, locs, require_call_site=True).edges}
    assert ("caller", "aliased") not in pairs
    assert ("caller", "target") in pairs  # the unambiguous call is unaffected
