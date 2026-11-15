"""Unit coverage for the authored-engine co-member closure + foreign-child read-back filter
(core/incremental_scoped), the fix for the real-repo Rust data loss: a type's methods/`impl` blocks
can be authored in a DIFFERENT file than the type's declaration, so resyncing a file that merely
references the type must NOT delete those cross-file methods.

Mechanism recap: the provenance-scoped read-back reads a struct's children via HAS_METHOD; if a
method's impl file isn't in the emit set, the struct's re-emit can't reproduce it and compute_delta
deletes it. `_comember_file_ids` pulls the method-definer files into the emit set (so the type is
re-authored whole), and `_foreign_defined` keeps the read-back from claiming a child owned by a
non-emit file. Verified end-to-end on huggingface/tokenizers (a 32-node/328-edge divergence → exact).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge
from auryn.core.graph_sync import edge_key
from auryn.core.incremental_scoped import (
    _comember_file_ids,
    _foreign_defined,
    read_authored_fingerprints,
)
from auryn.ingestors.rust.incremental import RUST_SPEC

# A struct S declared in file a.rs; its trait-impl method M authored in a DIFFERENT file b.rs.
_F1 = "rust://p/crate/c/file/a.rs"
_F2 = "rust://p/crate/c/file/b.rs"
_S = "rust://p/crate/c/lib.c.struct.s"
_M = "rust://p/crate/c/lib.c.struct.s.method.m()"
_PARAM = "rust://p/crate/c/lib.c.struct.s.method.m().param.0.self"


@pytest.fixture()
def graph(falkordb_connection):
    g = falkordb_connection.select_graph(f"comember_{uuid4().hex}")
    w = GraphWriter(g)
    w.query(
        "CREATE (:File:Entity {id:$f1}), (:File:Entity {id:$f2}), "
        "(:Struct:Entity {id:$s}), (:Method:Entity {id:$m}), (:Parameter:Entity {id:$p})",
        {"f1": _F1, "f2": _F2, "s": _S, "m": _M, "p": _PARAM},
    )
    w.query("MATCH (f:File {id:$f1}),(s:Struct {id:$s}) CREATE (f)-[:DEFINES]->(s)", {"f1": _F1, "s": _S})
    w.query("MATCH (f:File {id:$f2}),(m:Method {id:$m}) CREATE (f)-[:DEFINES]->(m)", {"f2": _F2, "m": _M})
    w.query("MATCH (s:Struct {id:$s}),(m:Method {id:$m}) CREATE (s)-[:HAS_METHOD]->(m)", {"s": _S, "m": _M})
    w.query("MATCH (m:Method {id:$m}),(p:Parameter {id:$p}) CREATE (m)-[:HAS_PARAMETER]->(p)", {"m": _M, "p": _PARAM})
    try:
        yield w
    finally:
        g.delete()


def test_comember_pulls_cross_file_method_definer(graph):
    # emit = {a.rs} (declares S) must pull b.rs (authors S's method) so S is re-authored whole.
    assert _comember_file_ids(graph, RUST_SPEC, {_F1}) == {_F2}
    # once b.rs is already in the emit set there is nothing left to pull.
    assert _comember_file_ids(graph, RUST_SPEC, {_F1, _F2}) == set()


def test_foreign_defined_flags_only_non_emit_owned_children(graph):
    # M is DEFINED by b.rs -> foreign to an emit set of just a.rs (keep it out of a.rs's read-back).
    assert _foreign_defined(graph, ("DEFINES",), [_M], {_F1}) == {_M}
    # ...but not foreign once its definer b.rs is in the emit set.
    assert _foreign_defined(graph, ("DEFINES",), [_M], {_F1, _F2}) == set()
    # a pure child (a Parameter: no DEFINES edge of its own) is never foreign -- always owned by its
    # parent's file, so the read-back keeps it.
    assert _foreign_defined(graph, ("DEFINES",), [_PARAM], {_F1}) == set()


# A struct U (file b.rs) with a METHODLESS `impl Marker for U {}` whose trait Marker lives in c.rs. The
# IMPLEMENTS edge is authored by c.rs (its `_src_file`) and has no method node, so no closure can pull
# c.rs. Instead the read-back keeps the edge in place when its authoring file isn't being re-emitted.
_FU = "rust://p/crate/c/file/b.rs"
_FT = "rust://p/crate/c/file/c.rs"
_U = "rust://p/crate/c/lib.c.struct.u"
_MARKER = "rust://p/crate/c/lib.c.trait.marker"


@pytest.fixture()
def impl_graph(falkordb_connection):
    g = falkordb_connection.select_graph(f"coimpl_{uuid4().hex}")
    w = GraphWriter(g)
    w.query(
        "CREATE (:File:Entity {id:$fu}), (:File:Entity {id:$ft}), "
        "(:Struct:Entity {id:$u, _fingerprint:'u'}), (:Trait:Entity {id:$t, _fingerprint:'t'})",
        {"fu": _FU, "ft": _FT, "u": _U, "t": _MARKER},
    )
    w.query("MATCH (f:File {id:$fu}),(u:Struct {id:$u}) CREATE (f)-[:DEFINES]->(u)", {"fu": _FU, "u": _U})
    w.query("MATCH (f:File {id:$ft}),(t:Trait {id:$t}) CREATE (f)-[:DEFINES]->(t)", {"ft": _FT, "t": _MARKER})
    # the IMPLEMENTS edge is authored by c.rs (the trait's file) and stamped with that _src_file.
    w.query(
        "MATCH (u:Struct {id:$u}),(t:Trait {id:$t}) CREATE (u)-[:IMPLEMENTS {_fingerprint:'i', _src_file:$ft}]->(t)",
        {"u": _U, "t": _MARKER, "ft": _FT},
    )
    try:
        yield w
    finally:
        g.delete()


def test_readback_excludes_foreign_authored_impl_edge(impl_graph):
    impl_edge = edge_key(GraphEdge("IMPLEMENTS", "Struct", {"id": _U}, "Trait", {"id": _MARKER}, {}))
    # emit = {b.rs} only: U is owned (b.rs DEFINES it), but its IMPLEMENTS edge is authored by c.rs
    # (_src_file) which is NOT in the emit set -> excluded from the read-back, so compute_delta, seeing
    # the emit set never re-author it, cannot delete it.
    _, edges = read_authored_fingerprints(impl_graph, RUST_SPEC, [_FU])
    assert impl_edge not in edges
    # emit = {b.rs, c.rs}: the impl's own file IS re-emitted -> the edge is claimed and diffed normally
    # (so a genuine edit/removal of the impl is still reconciled).
    _, edges = read_authored_fingerprints(impl_graph, RUST_SPEC, [_FU, _FT])
    assert impl_edge in edges
