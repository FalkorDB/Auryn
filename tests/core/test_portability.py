"""Graph portability via RDB (core.portability): export = SAVE + copy the RDB; import = validate +
copy it into a backend's db path. The full round-trip (copy a store.db into a fresh AURYN_HOME
and see every graph via `auryn list`) is the design's whole point -- these lock the file mechanics."""

from __future__ import annotations

import pytest

from auryn.core import portability


def test_export_produces_a_loadable_rdb(falkordb_connection, graph, tmp_path):
    graph.query("CREATE (:Function {id: 'f1', name: 'connect'})")
    dest = tmp_path / "shared.rdb"
    try:
        out = portability.export_graph(falkordb_connection, dest)
    except FileNotFoundError:
        pytest.skip("backend RDB not on this filesystem (remote server) -- export needs a local RDB")
    assert out == dest and dest.exists()
    assert dest.read_bytes()[:5] == b"REDIS"  # a genuine Redis RDB, not an ad-hoc dump
    assert dest.stat().st_size > 0


def test_import_copies_a_valid_rdb_into_the_target_db_path(tmp_path):
    src = tmp_path / "src.rdb"
    src.write_bytes(b"REDIS0011" + b"\x00" * 32)
    dst = tmp_path / "home" / "store.db"  # parent doesn't exist yet -- import must create it
    out = portability.import_graph(src, dst)
    assert out == dst and dst.read_bytes() == src.read_bytes()


def test_import_rejects_a_non_rdb_file(tmp_path):
    bad = tmp_path / "graph.json"
    bad.write_text('{"nodes": [], "edges": []}')
    with pytest.raises(ValueError, match="not a Redis RDB"):
        portability.import_graph(bad, tmp_path / "store.db")


def test_import_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        portability.import_graph(tmp_path / "nope.rdb", tmp_path / "store.db")
