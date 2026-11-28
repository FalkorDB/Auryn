"""Cross-repo package-root / project-layout resolution in `reconcile_linkages`. When a `resolved:false`
placeholder matches a real definition whose linkage key differs ONLY by a leading module prefix (the same
file resolving under two dotted identities -- a package root nested under / on sys.path beside its longer
file-path spelling), it is AUTO-RESOLVED when the match is unique across the GRAPH (exactly one such
definition). Candidates come from a name-indexed graph-wide seek, so a definition under an ambiguous key, or
from a prior ingest, still counts -- the uniqueness gate is the safety ADR 0004 decision 6 called for.

Pure-unit over a fake writer that serves both the `linkage_key IN` and the `name IN` seek from canned rows.
"""

from __future__ import annotations

import logging

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.linkage import reconcile_linkages


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class _FakeWriter:
    """GraphWriter stand-in. Rows are (linkage_key, id, resolved, defined, origin, name). Serves:
    `CALL db.labels()`; the `linkage_key IN $ls` seek (-> linkage_key,id,resolved,defined,origin); the
    graph-wide canonical `name IN $names` seek (-> name,linkage_key,id, only canonical rows, direction A);
    and the direction-B `name IN $names ... resolved = false` seek (-> linkage_key,id, placeholders only,
    #852). Records `query` writes."""

    def __init__(self, rows, labels=("Function",)):
        self._rows = rows
        self._labels = list(labels)
        self.writes: list[tuple[str, dict]] = []

    def ro_query(self, cypher, params=None):
        if "db.labels()" in cypher:
            return _Result([[lb] for lb in self._labels])
        if "n.name IN $names" in cypher:
            names = set((params or {}).get("names", []))
            if "coalesce(n.resolved, true) = false" in cypher:  # direction B: pre-existing placeholders by name
                return _Result([[r[0], r[1]] for r in self._rows if r[5] in names and r[2] is False])
            # direction A: graph-wide, name-indexed canonical candidates (-> name, linkage_key, id)
            return _Result([[r[5], r[0], r[1]] for r in self._rows
                            if r[5] in names and r[2] is not False and r[3] is not False])
        sel = set((params or {}).get("ls", []))  # the linkage_key IN seek
        return _Result([[r[0], r[1], r[2], r[3], r[4]] for r in self._rows if r[0] in sel])

    def query(self, cypher, params=None):
        self.writes.append((cypher, params or {}))
        return _Result([])


def test_unique_symbol_package_root_mismatch_auto_resolves(caplog):
    # `import acme_payments.core` mints a placeholder differing from the real def ONLY by a wrapper-dir
    # prefix; exactly one def matches graph-wide -> auto-resolve.
    ph = "extern://python/acme_payments.core::charge"
    real = "extern://python/acme-payments.acme_payments.core::charge"
    writer = _FakeWriter([
        (real, "py://real/charge", None, None, "local", "charge"),
        (ph, ph, False, None, "external", "charge"),
    ])
    with caplog.at_level(logging.INFO, logger="auryn.core.linkage"):
        stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[ph])
    assert stats.absorbed == 1
    assert stats.resolved_near_misses == [(ph, real)]
    assert stats.unresolved == [] and stats.near_misses == []
    assert writer.writes
    assert any("auto-resolved" in r.message for r in caplog.records)


def test_module_import_package_root_mismatch_auto_resolves():
    # The reported repro: `import llm_client` resolves as `backend.llm_client`; the module file indexes as
    # `plugins.…backend.backend.llm_client`. MODULE keys have NO `::`, so this only works now that
    # _parse_linkage splits the dotted path (last component = name).
    ph = "extern://python/backend.llm_client"
    real = "extern://python/plugins.com.esl.feltfixinator.backend.backend.llm_client"
    writer = _FakeWriter([
        (real, "py://real/mod", None, None, "local", "llm_client"),
        (ph, ph, False, None, "external", "llm_client"),
    ], labels=("Module",))
    stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[ph])
    assert stats.absorbed == 1
    assert stats.resolved_near_misses == [(ph, real)]
    assert stats.unresolved == []


def test_graph_wide_uniqueness_counts_a_prior_ingest_definition():
    # Two suffix-compatible defs exist in the GRAPH (one not even touched by this ingest). A touched-keys-only
    # check would merge; the graph-wide name seek sees both -> ambiguous, left unresolved.
    ph = "extern://python/backend.core::charge"
    real_touched = "extern://python/plugins.backend.core::charge"
    real_prior = "extern://python/vendor.wrapped.backend.core::charge"   # already in the graph, NOT touched
    writer = _FakeWriter([
        (real_touched, "py://a/charge", None, None, "local", "charge"),
        (real_prior, "py://b/charge", None, None, "local", "charge"),
        (ph, ph, False, None, "external", "charge"),
    ])
    stats = reconcile_linkages(writer, real_linkages=[real_touched], placeholder_ids=[ph])
    assert stats.absorbed == 0                 # two graph-wide candidates -> not merged
    assert stats.unresolved == [ph]
    assert len(stats.near_misses) == 1 and stats.near_misses[0][0] == ph


def test_ambiguous_key_definition_still_blocks_auto_resolution():
    # A single ambiguous linkage key holds TWO distinct def nodes (both named `charge`, suffix-compatible).
    # The name seek returns both -> two distinct targets -> ambiguous, never merged (the gap the fix closes).
    ph = "extern://python/backend.core::charge"
    dup = "extern://python/plugins.backend.core::charge"   # one key, two def nodes
    writer = _FakeWriter([
        (dup, "py://a/charge", None, None, "local", "charge"),
        (dup, "py://b/charge", None, None, "local", "charge"),
        (ph, ph, False, None, "external", "charge"),
    ])
    stats = reconcile_linkages(writer, real_linkages=[dup], placeholder_ids=[ph])
    assert stats.absorbed == 0
    assert stats.unresolved == [ph]
    assert len(stats.near_misses) == 1


def test_module_placeholder_does_not_bind_to_a_same_named_symbol():
    # A Module import `backend.core` must NOT absorb into a Function `plugins.backend::core` (same name
    # `core`) -- different symbol KINDS (module key has no `::`, symbol key does).
    ph = "extern://python/backend.core"                 # `import backend.core` (module placeholder)
    fn = "extern://python/plugins.backend::core"        # a function named `core` in plugins.backend
    writer = _FakeWriter([
        (fn, "py://a/core", None, None, "local", "core"),
        (ph, ph, False, None, "external", "core"),
    ], labels=("Module", "Function"))
    stats = reconcile_linkages(writer, real_linkages=[fn], placeholder_ids=[ph])
    assert stats.absorbed == 0 and stats.unresolved == [ph] and stats.resolved_near_misses == []


def test_declared_namespace_language_is_not_auto_resolved():
    # Java keys use DECLARED package/type names, not file paths: `acme.billing.Charger#charge` and
    # `com.acme.billing.Charger#charge` are DIFFERENT types, not a package-root leak -> never auto-merged.
    ph = "extern://java/acme.billing.Charger#charge"
    real = "extern://java/com.acme.billing.Charger#charge"
    writer = _FakeWriter([
        (real, "java://a/charge", None, None, "local", "charge"),
        (ph, ph, False, None, "external", "charge"),
    ], labels=("Method",))
    stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[ph])
    assert stats.absorbed == 0 and stats.unresolved == [ph] and stats.resolved_near_misses == []


def test_exact_match_still_absorbs_via_linkage_key():
    key = "extern://python/acme.core::charge"
    writer = _FakeWriter([
        (key, "py://real/charge", None, None, "local", "charge"),
        (key, key, False, None, "external", "charge"),
    ])
    stats = reconcile_linkages(writer, real_linkages=[key], placeholder_ids=[key])
    assert stats.absorbed == 1 and stats.unresolved == [] and stats.near_misses == []


def test_builtin_placeholder_is_not_counted_as_unresolved():
    key = "extern://python/os.path::join"
    writer = _FakeWriter([(key, key, False, None, "stdlib", "join")])
    stats = reconcile_linkages(writer, real_linkages=[], placeholder_ids=[key])
    assert stats.unresolved == [] and stats.near_misses == []


def test_unrelated_same_name_symbol_is_not_a_near_miss(caplog):
    # Same name `run` but qualifiers share no tail -> a genuine unresolved external, not a layout mismatch.
    ph = "extern://python/foo::run"
    real = "extern://python/bar::run"
    writer = _FakeWriter([
        (real, "py://bar/run", None, None, "local", "run"),
        (ph, ph, False, None, "external", "run"),
    ])
    with caplog.at_level(logging.WARNING, logger="auryn.core.linkage"):
        stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[ph])
    assert stats.absorbed == 0
    assert stats.unresolved == [ph] and stats.near_misses == []


def test_module_import_resolves_end_to_end_on_a_real_graph(graph):
    # The exact reported repro shape, against a real FalkorDB: `import llm_client` (resolving as
    # `backend.llm_client`) must bind to the module indexed as `plugins.…backend.backend.llm_client`.
    w = GraphWriter(graph)
    real = "extern://python/plugins.com.esl.feltfixinator.backend.backend.llm_client"
    ph = "extern://python/backend.llm_client"
    w.merge_node(GraphNode("Module", {"id": "real_mod"}, {"name": "llm_client", "linkage_key": real}))
    w.merge_node(GraphNode("Module", {"id": "ph_mod"},
                           {"name": "llm_client", "linkage_key": ph, "resolved": False, "origin": "external"}))
    w.merge_node(GraphNode("Module", {"id": "caller"}, {"name": "feltfixinator_bridge"}))
    w.merge_edge(GraphEdge("IMPORTS", "Module", {"id": "caller"}, "Module", {"id": "ph_mod"}))

    stats = reconcile_linkages(w, real_linkages=[real], placeholder_ids=[ph])

    assert stats.absorbed == 1 and (ph, real) in stats.resolved_near_misses
    # the placeholder is folded away and the import now points at the real module
    assert graph.query("MATCH (m:Module {id:'ph_mod'}) RETURN count(m)").result_set == [[0]]
    assert graph.query(
        "MATCH (:Module {id:'caller'})-[:IMPORTS]->(:Module {id:'real_mod'}) RETURN count(*)"
    ).result_set == [[1]]


def test_definition_first_resolves_a_preexisting_placeholder():
    # #852 (direction B): the def arrives in a LATER ingest that touches ONLY the def's key; the
    # placeholder was ingested earlier under a prefix-mismatched key. Direction A alone misses it
    # (this ingest's unresolved set is empty); direction B discovers it by name and resolves it.
    real = "extern://python/plugins.com.esl.feltfixinator.backend.backend.llm_client"  # def, this ingest
    ph = "extern://python/backend.llm_client"  # pre-existing placeholder from an earlier, separate ingest
    writer = _FakeWriter([
        (real, "py://real/mod", None, None, "local", "llm_client"),
        (ph, ph, False, None, "external", "llm_client"),
    ], labels=("Module",))
    stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[])  # only the def this pass
    assert stats.absorbed == 1
    assert stats.resolved_near_misses == [(ph, real)]
    assert stats.unresolved == []  # the pre-existing placeholder is not re-reported as this ingest's miss


def test_definition_first_ambiguous_match_is_not_resolved():
    # Two canonical defs suffix-match the pre-existing placeholder -> ambiguous -> never auto-merged,
    # same safety as direction A.
    real_new = "extern://python/plugins.backend.core::charge"       # def ingested this pass
    other = "extern://python/vendor.wrapped.backend.core::charge"   # another existing def (the ambiguity)
    ph = "extern://python/backend.core::charge"                     # pre-existing placeholder
    writer = _FakeWriter([
        (real_new, "py://a/charge", None, None, "local", "charge"),
        (other, "py://b/charge", None, None, "local", "charge"),
        (ph, ph, False, None, "external", "charge"),
    ])
    stats = reconcile_linkages(writer, real_linkages=[real_new], placeholder_ids=[])
    assert stats.absorbed == 0
    assert stats.near_misses and stats.near_misses[0][0] == ph


def test_definition_first_declared_namespace_language_not_resolved():
    # Java keys are declared names, not file paths -> a suffix mismatch is a DIFFERENT type, never a
    # package-root leak. Direction B must respect the same file-path-keyed-lang guard as direction A.
    real = "extern://java/com.acme.billing.Charger#charge"  # def this pass
    ph = "extern://java/acme.billing.Charger#charge"        # pre-existing placeholder
    writer = _FakeWriter([
        (real, "java://a/charge", None, None, "local", "charge"),
        (ph, ph, False, None, "external", "charge"),
    ], labels=("Method",))
    stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[])
    assert stats.absorbed == 0 and stats.resolved_near_misses == []


def test_definition_first_resolves_preexisting_placeholder_on_a_real_graph(graph):
    # The reported repro shape in the OPPOSITE ingest order, against a real FalkorDB: the consumer
    # (placeholder) was ingested first; now the defining repo is ingested and must bind the earlier
    # import to the real module.
    w = GraphWriter(graph)
    real = "extern://python/plugins.com.esl.feltfixinator.backend.backend.llm_client"
    ph = "extern://python/backend.llm_client"
    w.merge_node(GraphNode("Module", {"id": "ph_mod"},
                           {"name": "llm_client", "linkage_key": ph, "resolved": False, "origin": "external"}))
    w.merge_node(GraphNode("Module", {"id": "caller"}, {"name": "feltfixinator_bridge"}))
    w.merge_edge(GraphEdge("IMPORTS", "Module", {"id": "caller"}, "Module", {"id": "ph_mod"}))
    # The defining repo is ingested later (a separate pass that touches only the def's key):
    w.merge_node(GraphNode("Module", {"id": "real_mod"}, {"name": "llm_client", "linkage_key": real}))

    stats = reconcile_linkages(w, real_linkages=[real], placeholder_ids=[])

    assert stats.absorbed == 1 and (ph, real) in stats.resolved_near_misses
    assert graph.query("MATCH (m:Module {id:'ph_mod'}) RETURN count(m)").result_set == [[0]]
    assert graph.query(
        "MATCH (:Module {id:'caller'})-[:IMPORTS]->(:Module {id:'real_mod'}) RETURN count(*)"
    ).result_set == [[1]]
