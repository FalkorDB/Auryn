"""Equivalence oracle for the TypeScript incremental resync (ADR 0002), mirroring the Python
one: for each scenario, an incremental resync must produce a graph node-for-node,
edge-for-edge identical to a full recompute of the same final state.

The baseline has cross-module references (imports + import-bound calls + inheritance) so
rename/signature/delete scenarios exercise the reverse-dep + IMPORTS closure. The
newly-satisfiable-import case passes here (unlike Python's original xfail) because TS has an
IMPORTS edge from the start.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.typescript.git_sync import sync_at
from auryn.ingestors.typescript.incremental import ChangeSet, incremental_ts_sync

_PROJECT = "equiv_ts"
_UTIL = "src/util.ts"
_SERVICE = "src/service.ts"

_BASELINE: dict[str, str] = {
    "src/models.ts": "export class Base {}\n\n\nexport class User extends Base {\n  label(): number {\n    return 1;\n  }\n}\n",
    _UTIL: "export function helper(x: number): number {\n  return x + 1;\n}\n",
    _SERVICE: (
        "import { helper } from './util';\n"
        "import { User } from './models';\n\n\n"
        "export function make(): User {\n  return new User();\n}\n\n\n"
        "export function greet(v: number): number {\n  return helper(v);\n}\n"
    ),
    "src/main.ts": (
        "import { make, greet } from './service';\n"
        "import { helper } from './util';\n\n\n"
        "export function run(): number {\n  return greet(1) + helper(2);\n}\n"
    ),
    "src/standalone.ts": "export function solo(): number {\n  return 0;\n}\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _full(graph, root: Path) -> None:
    src = LocalDirectorySource(root)
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=_PROJECT, repo_uri="test://ts", ref=None)


def _incremental(graph, root: Path, changes: ChangeSet) -> None:
    src = LocalDirectorySource(root)
    incremental_ts_sync(GraphWriter(graph), src, src.tree_sha(), changes, project_name=_PROJECT, repo_uri="test://ts")


def _dump(graph) -> tuple[dict, dict]:
    nodes = {
        (label, nid): {k: v for k, v in props.items() if k != "_fingerprint"}
        for label, nid, props in graph.ro_query("MATCH (n) RETURN labels(n)[0], n.id, properties(n)").result_set
    }
    edges = {
        (rel, aid, bid): {k: v for k, v in props.items() if k != "_fingerprint"}
        for rel, aid, bid, props in graph.ro_query(
            "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r)"
        ).result_set
    }
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


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text()
    assert old in text, f"{old!r} not in {rel}"
    p.write_text(text.replace(old, new, 1))


@dataclass
class Case:
    id: str
    mutate: Callable[[Path], None]
    changeset: ChangeSet
    extra_baseline: dict[str, str] = field(default_factory=dict)


CASES: list[Case] = [
    Case("edit_body",
         lambda r: _edit(r, _UTIL, "return x + 1;", "return x + 111;"),
         ChangeSet(modified=[_UTIL])),
    Case("comment_eof",
         lambda r: (r / _UTIL).write_text((r / _UTIL).read_text() + "\n// trailing\n"),
         ChangeSet(modified=[_UTIL])),
    Case("comment_top",
         lambda r: (r / _UTIL).write_text("// top\n" + (r / _UTIL).read_text()),
         ChangeSet(modified=[_UTIL])),
    Case("edit_signature_called",
         lambda r: _edit(r, _UTIL, "export function helper(x: number): number {",
                         "export function helper(x: number, y: number): number {"),
         ChangeSet(modified=[_UTIL])),
    Case("rename_symbol_called",
         lambda r: _edit(r, _SERVICE, "export function greet(", "export function welcome("),
         ChangeSet(modified=[_SERVICE])),
    Case("delete_function_called",
         lambda r: _edit(r, _SERVICE, "\n\nexport function greet(v: number): number {\n  return helper(v);\n}\n", "\n"),
         ChangeSet(modified=[_SERVICE])),
    Case("add_function",
         lambda r: (r / _UTIL).write_text((r / _UTIL).read_text() + "\n\nexport function extra(z: number): number {\n  return z;\n}\n"),
         ChangeSet(modified=[_UTIL])),
    Case("caller_edit_only",
         lambda r: _edit(r, "src/main.ts", "return greet(1) + helper(2);", "return helper(2) + greet(1);"),
         ChangeSet(modified=["src/main.ts"])),
    Case("add_file",
         lambda r: _write(r, {"src/newmod.ts": "import { helper } from './util';\n\n\nexport function nm(): number {\n  return helper(9);\n}\n"}),
         ChangeSet(added=["src/newmod.ts"])),
    Case("delete_file",
         lambda r: (r / "src/standalone.ts").unlink(),
         ChangeSet(deleted=["src/standalone.ts"])),
    Case("rename_file",
         lambda r: (r / "src/standalone.ts").rename(r / "src/renamed.ts"),
         ChangeSet(renamed=[("src/standalone.ts", "src/renamed.ts")])),
    Case("add_newly_satisfied",  # lonely imports a name that only now exists -> IMPORTS closure
         lambda r: (r / _UTIL).write_text((r / _UTIL).read_text() + "\n\nexport function zeta(n: number): number {\n  return n;\n}\n"),
         ChangeSet(modified=[_UTIL]),
         extra_baseline={"src/lonely.ts": "import { zeta } from './util';\n\n\nexport function f(): number {\n  return zeta(1);\n}\n"}),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_incremental_matches_full_recompute(case: Case, falkordb_connection, tmp_path):
    root = tmp_path / case.id
    _write(root, {**_BASELINE, **case.extra_baseline})

    g_inc = falkordb_connection.select_graph(f"equiv_ts_inc_{uuid4().hex}")
    g_full = falkordb_connection.select_graph(f"equiv_ts_full_{uuid4().hex}")
    try:
        _full(g_inc, root)          # baseline into the incremental graph
        case.mutate(root)
        _incremental(g_inc, root, case.changeset)
        _full(g_full, root)         # full recompute of the mutated tree

        nf, ef = _dump(g_full)
        ni, ei = _dump(g_inc)
        problems = _diff(nf, ni, "node") + _diff(ef, ei, "edge")
        assert not problems, f"incremental != full for {case.id!r}:\n" + "\n".join(problems)
    finally:
        for g in (g_inc, g_full):
            try:
                g.delete()
            except Exception:
                pass
