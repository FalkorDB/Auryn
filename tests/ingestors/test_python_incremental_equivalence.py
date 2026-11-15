"""Equivalence oracle for the Python `ast` incremental resync (ADR 0002).

The whole bet behind incremental resync is that git (what changed) + the graph (what's
affected) let us re-emit ONLY the blast radius and get a graph identical to a full
recompute. This test proves exactly that: for each scenario it builds two graphs --

  * G_full: a fresh full recompute of the mutated tree, and
  * G_inc:  a full recompute of the BASELINE tree, then `incremental_ast_sync` applying the
            scenario's ChangeSet --

and asserts they are node-for-node, edge-for-edge identical. A wrong/incomplete affected
set (the failure mode that would silently leave the graph stale) fails here loudly.

The baseline project deliberately has cross-module references (pkg/caller.py imports+calls
pkg/sentinel.py) so rename/signature/delete scenarios exercise the reverse-dep closure, not
just single-file edits.

The one xfail encodes a known, documented prototype gap: a *newly-satisfiable* reference
through an import (the `ast` engine records no breadcrumb -- no IMPORTS edge -- so a module
that only now resolves a name isn't rediscovered). See incremental.py's module docstring.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.python.git_sync import GitPythonProjectConfig, sync_at
from auryn.ingestors.python.incremental import ChangeSet, incremental_ast_sync

_PROJECT = "equiv"

_BASELINE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/sentinel.py": (
        "def alpha(x):\n    return x + 1\n\n\n"
        "def beta(x):\n    return x + 2\n\n\n"
        "def gamma(x):\n    return x + 3\n"
    ),
    "pkg/caller.py": (
        "from pkg.sentinel import alpha, beta\n\n\n"
        "def use():\n    return alpha(1) + beta(2)\n"
    ),
    "standalone.py": "def solo():\n    return 0\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _config() -> GitPythonProjectConfig:
    return GitPythonProjectConfig(repo_uri="test://equiv", project_name=_PROJECT)


def _full(graph, root: Path) -> None:
    src = LocalDirectorySource(root)
    sync_at(GraphWriter(graph), _config(), src, src.tree_sha(), None, engine="ast")


def _incremental(graph, root: Path, changes: ChangeSet) -> None:
    src = LocalDirectorySource(root)
    incremental_ast_sync(GraphWriter(graph), _config(), src, src.tree_sha(), changes)


def _dump(graph) -> tuple[dict, dict]:
    nodes: dict[tuple[str, str], dict] = {}
    for label, nid, props in graph.ro_query(
        "MATCH (n) RETURN labels(n)[0], n.id, properties(n)"
    ).result_set:
        nodes[(label, nid)] = {k: v for k, v in props.items() if k != "_fingerprint"}
    edges: dict[tuple, dict] = {}
    for rel, aid, bid, props in graph.ro_query(
        "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r)"
    ).result_set:
        edges[(rel, aid, bid)] = {k: v for k, v in props.items() if k != "_fingerprint"}
    return nodes, edges


def _diff(full: dict, inc: dict, kind: str) -> list[str]:
    msgs = []
    for key in full.keys() - inc.keys():
        msgs.append(f"  {kind} MISSING from incremental: {key}")
    for key in inc.keys() - full.keys():
        msgs.append(f"  {kind} EXTRA in incremental:   {key}")
    for key in full.keys() & inc.keys():
        if full[key] != inc[key]:
            msgs.append(f"  {kind} PROP DIFF {key}:\n     full={full[key]}\n     inc ={inc[key]}")
    return msgs


# -- scenarios ---------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    mutate: Callable[[Path], None]
    changeset: ChangeSet
    extra_baseline: dict[str, str] = field(default_factory=dict)
    xfail: str | None = None


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text()
    assert old in text, f"{old!r} not in {rel}"
    p.write_text(text.replace(old, new, 1))


_SENT = "pkg/sentinel.py"

CASES: list[Case] = [
    Case("edit_body",
         lambda r: _edit(r, _SENT, "return x + 1", "return x + 111"),
         ChangeSet(modified=[_SENT])),
    Case("comment_eof",
         lambda r: (r / _SENT).write_text((r / _SENT).read_text() + "\n# trailing\n"),
         ChangeSet(modified=[_SENT])),
    Case("comment_top",
         lambda r: (r / _SENT).write_text("# top\n" + (r / _SENT).read_text()),
         ChangeSet(modified=[_SENT])),
    Case("edit_signature_called",  # alpha is called by caller -> reverse-dep must move the edge
         lambda r: _edit(r, _SENT, "def alpha(x):", "def alpha(x, y):"),
         ChangeSet(modified=[_SENT])),
    Case("rename_symbol_called",  # rename alpha -> caller's edge must drop (now unresolved)
         lambda r: _edit(r, _SENT, "def alpha(x):", "def alpha_renamed(x):"),
         ChangeSet(modified=[_SENT])),
    Case("delete_function_called",  # delete beta (caller calls it)
         lambda r: _edit(r, _SENT, "def beta(x):\n    return x + 2\n\n\n", ""),
         ChangeSet(modified=[_SENT])),
    Case("delete_function_unref",  # delete gamma (nobody calls it)
         lambda r: _edit(r, _SENT, "def gamma(x):\n    return x + 3\n", ""),
         ChangeSet(modified=[_SENT])),
    Case("add_function_unref",
         lambda r: (r / _SENT).write_text((r / _SENT).read_text() + "\n\ndef added(x):\n    return x\n"),
         ChangeSet(modified=[_SENT])),
    Case("caller_edit_only",
         lambda r: _edit(r, "pkg/caller.py", "return alpha(1) + beta(2)", "return beta(2) + alpha(1)"),
         ChangeSet(modified=["pkg/caller.py"])),
    Case("add_file",
         lambda r: _write(r, {"pkg/newmod.py": "from pkg.sentinel import alpha\n\n\ndef g():\n    return alpha(9)\n"}),
         ChangeSet(added=["pkg/newmod.py"])),
    Case("delete_file",
         lambda r: (r / "standalone.py").unlink(),
         ChangeSet(deleted=["standalone.py"])),
    Case("rename_file",
         lambda r: (r / "standalone.py").rename(r / "renamed.py"),
         ChangeSet(renamed=[("standalone.py", "renamed.py")])),
    Case("add_newly_satisfied",  # lonely references a name that only now exists; IMPORTS edge
         lambda r: (r / _SENT).write_text((r / _SENT).read_text() + "\n\ndef zeta(x):\n    return x\n"),
         ChangeSet(modified=[_SENT]),  # closure finds lonely via IMPORTS -> pkg.sentinel
         extra_baseline={"lonely.py": "from pkg.sentinel import zeta\n\n\ndef f():\n    return zeta(1)\n"}),
    # Prefix-sibling scopes: the scoped read-back/delete must bound the id range at the child
    # separator, or editing `…repocard` sweeps `…repocard_data` (its id continues with `_`, not
    # `.`) and the incremental graph silently loses the sibling. Regression for the delta-sync
    # data-loss found on huggingface_hub (repocard vs repocard_data).
    Case("edit_prefix_sibling_module",
         lambda r: _edit(r, "pkg/repocard.py", "return 1", "return 111"),
         ChangeSet(modified=["pkg/repocard.py"]),
         extra_baseline={
             "pkg/repocard.py": "def card():\n    return 1\n",
             "pkg/repocard_data.py": (
                 "class CardData:\n    def to_dict(self):\n        return {}\n\n\n"
                 "def eval_results():\n    return 2\n"
             ),
         }),
    Case("delete_prefix_sibling_module",  # deleting `…repocard` must not DETACH DELETE `…repocard_data`
         lambda r: (r / "pkg/repocard.py").unlink(),
         ChangeSet(deleted=["pkg/repocard.py"]),
         extra_baseline={
             "pkg/repocard.py": "def card():\n    return 1\n",
             "pkg/repocard_data.py": (
                 "class CardData:\n    def to_dict(self):\n        return {}\n\n\n"
                 "def eval_results():\n    return 2\n"
             ),
         }),
    # Nested sub-package: editing a parent package `__init__` must not sweep a sub-package's nodes
    # (their ids continue the parent's dotted path with the separator -> excluded by longest-prefix
    # ownership, not by the range alone).
    Case("edit_parent_pkg_init_keeps_subpackage",
         lambda r: (r / "pkg/__init__.py").write_text("__version__ = '2'\n"),
         ChangeSet(modified=["pkg/__init__.py"]),
         extra_baseline={
             "pkg/sub/__init__.py": "",
             "pkg/sub/mod.py": "class SubThing:\n    def x(self):\n        return 1\n",
         }),
]


# A `src/` layout: import-root detection (Option A) must anchor dotted paths at `pkg` (strip
# `src/`), and the full + incremental engines -- which each build the package context from the
# same complete file list -- must STILL produce byte-identical graphs. This pins that making the
# derivation context-sensitive did not break the equivalence bet.
_SRC_BASELINE: dict[str, str] = {
    "src/pkg/__init__.py": "",
    "src/pkg/sentinel.py": "def alpha(x):\n    return x + 1\n\n\ndef beta(x):\n    return x + 2\n",
    "src/pkg/caller.py": "from pkg.sentinel import alpha\n\n\ndef use():\n    return alpha(1)\n",
}


@pytest.mark.parametrize(
    "mutate,changes",
    [
        (lambda r: _edit(r, "src/pkg/sentinel.py", "return x + 1", "return x + 9"),
         ChangeSet(modified=["src/pkg/sentinel.py"])),
        (lambda r: _write(r, {"src/pkg/newmod.py": "from pkg.sentinel import beta\n\n\ndef g():\n    return beta(2)\n"}),
         ChangeSet(added=["src/pkg/newmod.py"])),
    ],
    ids=["edit_body", "add_file"],
)
def test_incremental_matches_full_for_src_layout(mutate, changes, falkordb_connection, tmp_path):
    root = tmp_path / "srclayout"
    _write(root, _SRC_BASELINE)
    g_inc = falkordb_connection.select_graph(f"equiv_src_inc_{uuid4().hex}")
    g_full = falkordb_connection.select_graph(f"equiv_src_full_{uuid4().hex}")
    try:
        _full(g_inc, root)
        mutate(root)
        _incremental(g_inc, root, changes)
        _full(g_full, root)

        nf, ef = _dump(g_full)
        ni, ei = _dump(g_inc)
        problems = _diff(nf, ni, "node") + _diff(ef, ei, "edge")
        assert not problems, "incremental != full (src layout):\n" + "\n".join(problems)
        # the dotted path is import-root-anchored -- `src/` stripped (would be `src.pkg.sentinel` before)
        assert any(nid.endswith("module/pkg.sentinel") for _, nid in nf), "expected src/ stripped from dotted path"
    finally:
        for g in (g_inc, g_full):
            try:
                g.delete()
            except Exception:
                pass


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_incremental_matches_full_recompute(case: Case, falkordb_connection, tmp_path):
    root = tmp_path / case.id
    _write(root, {**_BASELINE, **case.extra_baseline})

    g_inc = falkordb_connection.select_graph(f"equiv_inc_{uuid4().hex}")
    g_full = falkordb_connection.select_graph(f"equiv_full_{uuid4().hex}")
    try:
        _full(g_inc, root)          # baseline into the incremental graph
        case.mutate(root)           # apply the edit
        _incremental(g_inc, root, case.changeset)
        _full(g_full, root)         # full recompute of the mutated tree

        nf, ef = _dump(g_full)
        ni, ei = _dump(g_inc)
        problems = _diff(nf, ni, "node") + _diff(ef, ei, "edge")

        if case.xfail:
            if not problems:
                pytest.fail(f"xfail scenario {case.id!r} unexpectedly PASSED -- gap may be fixed: {case.xfail}")
            pytest.xfail(f"{case.xfail}; divergences:\n" + "\n".join(problems))
        assert not problems, f"incremental != full for {case.id!r}:\n" + "\n".join(problems)
    finally:
        for g in (g_inc, g_full):
            try:
                g.delete()
            except Exception:
                pass
