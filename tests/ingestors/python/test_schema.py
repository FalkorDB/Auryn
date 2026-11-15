"""Well-formedness of the Python ingestor's graph schema (ingestors/python/schema.py).

Pure/offline -- no FalkorDB, no extractor. Guards the invariants the delta-sync
pipeline relies on (unique labels, id-keyed nodes) and keeps schema.py honest as the
model grows.
"""

from __future__ import annotations

from auryn.ingestors.python import schema


def test_node_labels_are_unique():
    labels = [s.label for s in schema.NODE_SCHEMAS]
    assert len(labels) == len(set(labels)), "duplicate node label in NODE_SCHEMAS"


def test_edge_rel_types_are_unique():
    rels = [s.rel_type for s in schema.EDGE_SCHEMAS]
    assert len(rels) == len(set(rels)), "duplicate rel_type in EDGE_SCHEMAS"


def test_every_node_is_id_keyed():
    # read_fingerprints/compute_delta key every node by `id`; a non-id key would make
    # the whole delta-sync mis-diff this project's subgraph.
    assert all(s.key_property == "id" for s in schema.NODE_SCHEMAS)


def test_type_labels_are_declared_node_labels():
    node_labels = {s.label for s in schema.NODE_SCHEMAS}
    assert set(schema.TYPE_LABELS) <= node_labels


def test_project_root_label_present():
    # git_sync swaps in a canonical Project root (python://<project>); the label must
    # exist so setup_schema indexes it.
    assert "Project" in {s.label for s in schema.NODE_SCHEMAS}


def test_backbone_and_semantic_edges_present():
    rels = {s.rel_type for s in schema.EDGE_SCHEMAS}
    # containment backbone
    assert {"HAS_PACKAGE", "HAS_MODULE", "HAS_CLASS", "HAS_FUNCTION", "HAS_METHOD",
            "HAS_ATTRIBUTE", "HAS_PARAMETER", "HAS_NESTED_CLASS"} <= rels
    # semantic layer
    assert {"EXTENDS", "IMPLEMENTS", "CALLS", "RETURNS"} <= rels
