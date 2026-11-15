"""Regression: a scope/file whose id is a STRING PREFIX of a sibling's must not be swept when the
sibling is resynced. The scoped read-back/delete bound the id range at the child separator; without
that, editing ``…repocard`` deletes ``…repocard_data`` (Tier-0, id-prefix engine) and editing
``mod.c`` deletes ``mod.config.c`` (authored/Tier-2 engine) -- a silent incremental != full-recompute
data loss. These were found resyncing huggingface_hub (python) and reproduced for C.

Both engines are covered: python exercises `read_scoped_fingerprints`, C exercises
`read_authored_fingerprints` + `_delete_subgraph`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter

from ._helpers import GitRepo, assert_graphs_equal, dump, fresh_graph, ingest_repo


def _run(conn, tmp_path, files: dict[str, str], edit: dict[str, str], project: str):
    repo = GitRepo.init(tmp_path / "repo")
    repo.write(files)
    s1 = repo.commit("c1")
    cache = tmp_path / "cache"
    incr = fresh_graph(conn, "sib_incr")
    full = fresh_graph(conn, "sib_full")
    try:
        ingest_repo(GraphWriter(incr), repo, s1, cache, project)   # baseline into the incremental graph
        repo.write(edit)
        s2 = repo.commit("c2")
        ingest_repo(GraphWriter(incr), repo, s2, cache, project)   # incremental resync c1 -> c2
        ingest_repo(GraphWriter(full), repo, s2, cache, project)   # full recompute of c2
        assert_graphs_equal(dump(full), dump(incr), f"{project} prefix-sibling")
    finally:
        for g in (incr, full):
            try:
                g.delete()
            except Exception:
                pass


def test_tier0_prefix_sibling_module_not_swept(falkordb_connection, tmp_path):
    # `pkg.repocard` is a string prefix of `pkg.repocard_data`; editing the former must not delete the
    # latter's module/class/method/param nodes.
    _run(
        falkordb_connection, tmp_path,
        files={
            "pkg/__init__.py": "",
            "pkg/repocard.py": "def card():\n    return 1\n",
            "pkg/repocard_data.py": "class CardData:\n    def to_dict(self):\n        return {}\n",
        },
        edit={"pkg/repocard.py": "def card():\n    return 111\n"},
        project="sib_py",
    )


def test_authored_prefix_sibling_file_not_swept(falkordb_connection, tmp_path):
    # Authored (C) engine: the file id `…/mod.c` is a string prefix of `…/mod.config.c`; resyncing
    # mod.c must not read+delete mod.config.c's File/static nodes.
    _run(
        falkordb_connection, tmp_path,
        files={
            "mod.c": "static int modhelper(void){ return 1; }\nint mod_entry(void){ return modhelper(); }\n",
            "mod.config.c": "static int cfghelper(void){ return 2; }\nint cfg_entry(void){ return cfghelper(); }\n",
        },
        edit={"mod.c": "static int modhelper(void){ return 100; }\nint mod_entry(void){ return modhelper(); }\n"},
        project="sib_c",
    )
