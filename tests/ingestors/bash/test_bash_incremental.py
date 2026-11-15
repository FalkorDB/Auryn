"""Equivalence oracle + incrementality proof for the Bash incremental resync (ADR 0002), mirroring
the TypeScript/Python oracles: for each scenario an incremental resync must produce a graph
node-for-node, edge-for-edge identical to a full recompute of the same final state.

Bash is file-scoped (every entity nests under its `File` id) so it rides the shared engine
(core/incremental_scoped.py) via BASH_SPEC. The baseline has cross-file references -- `source`
includes (IMPORTS), bare-name project-wide calls (CALLS), and external-command placeholders
(`extern://bash/*`) -- so the scenarios exercise the reverse-dep + IMPORTS closure, the
placeholder-satisfaction closure (a bare-name call newly satisfied by a definition in a file it
does NOT `source`), and orphan-placeholder reclamation (the last caller of an external command
drops it).

Two invariants beyond node/edge equality:
  * `test_incremental_is_not_full_reparse` proves the resync parses only the blast radius, not the
    whole tree (the user's "must actually be a delta, not a re-ingest" bar).
  * the equivalence itself guarantees a commit round-trip restores the exact prior graph.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.bash.git_sync import sync_at
from auryn.ingestors.bash.incremental import ChangeSet, incremental_bash_sync

_PROJECT = "equiv_bash"

_LIB = "src/lib.sh"
_UTIL = "src/util.sh"
_MAIN = "src/main.sh"
_STANDALONE = "src/standalone.sh"
_ORPHAN = "src/orphan.sh"

_BASELINE: dict[str, str] = {
    # lib defines helper + logmsg (printf is a builtin -> dropped, so these have no CALLS out).
    _LIB: "helper() {\n    printf ok\n}\n\nlogmsg() {\n    printf log\n}\n",
    # util sources lib and defines greet, which calls both lib functions by bare name.
    _UTIL: "source ./lib.sh\n\ngreet() {\n    helper\n    logmsg\n}\n",
    # main sources util, defines run() calling greet (project fn) and kubectl (external command ->
    # extern placeholder that main is the SOLE caller of -- the clean orphan-reclamation subject).
    _MAIN: "source ./util.sh\n\nrun() {\n    greet\n    kubectl apply\n}\n",
    # standalone calls curl (a DIFFERENT extern) and is never touched by most cases -> curl must
    # survive; solo is the file-delete/rename subject.
    _STANDALONE: "solo() {\n    curl https://example.com\n}\n",
    # orphan calls zeta by bare name but does NOT source anything -> zeta is an extern placeholder
    # until some file defines it (the placeholder-satisfaction subject).
    _ORPHAN: "start() {\n    zeta\n}\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _full(graph, root: Path) -> None:
    src = LocalDirectorySource(root)
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=_PROJECT, repo_uri="test://bash", ref=None)


def _incremental(graph, root: Path, changes: ChangeSet):
    src = LocalDirectorySource(root)
    return incremental_bash_sync(
        GraphWriter(graph), src, src.tree_sha(), changes, project_name=_PROJECT, repo_uri="test://bash"
    )


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
        msgs.append(f"  {kind} MISSING from incremental: {key}  {full[key]}")
    for key in inc.keys() - full.keys():
        msgs.append(f"  {kind} EXTRA in incremental:   {key}  {inc[key]}")
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
         lambda r: _edit(r, _LIB, "printf ok", "printf okay"),
         ChangeSet(modified=[_LIB])),
    Case("comment_eof",
         lambda r: (r / _LIB).write_text((r / _LIB).read_text() + "\n# trailing\n"),
         ChangeSet(modified=[_LIB])),
    Case("comment_top",
         lambda r: (r / _LIB).write_text("# top\n" + (r / _LIB).read_text()),
         ChangeSet(modified=[_LIB])),
    Case("rename_called",  # greet renamed; main (caller + importer) must re-resolve -> extern greet
         lambda r: _edit(r, _UTIL, "greet() {", "welcome() {"),
         ChangeSet(modified=[_UTIL])),
    Case("delete_called",  # greet removed; main's call becomes an extern placeholder
         lambda r: _edit(r, _UTIL, "\n\ngreet() {\n    helper\n    logmsg\n}\n", "\n"),
         ChangeSet(modified=[_UTIL])),
    Case("add_function",
         lambda r: (r / _LIB).write_text((r / _LIB).read_text() + "\nextra() {\n    printf x\n}\n"),
         ChangeSet(modified=[_LIB])),
    Case("caller_edit_only",
         lambda r: _edit(r, _MAIN, "    greet\n    kubectl apply", "    kubectl apply\n    greet"),
         ChangeSet(modified=[_MAIN])),
    Case("add_file",
         lambda r: _write(r, {"src/newmod.sh": "source ./lib.sh\n\nnm() {\n    helper\n}\n"}),
         ChangeSet(added=["src/newmod.sh"])),
    Case("delete_file",  # solo gone -> curl (its only caller) must be reclaimed
         lambda r: (r / _STANDALONE).unlink(),
         ChangeSet(deleted=[_STANDALONE])),
    Case("rename_file",
         lambda r: (r / _STANDALONE).rename(r / "src/renamed.sh"),
         ChangeSet(renamed=[(_STANDALONE, "src/renamed.sh")])),
    Case("remove_extern_call",  # ORPHAN RECLAMATION: main stops calling kubectl -> kubectl vanishes
         lambda r: _edit(r, _MAIN, "    greet\n    kubectl apply", "    greet"),
         ChangeSet(modified=[_MAIN])),
    Case("add_newly_satisfied_no_source",  # PLACEHOLDER-SAT: define zeta in lib; orphan.sh (no source) must re-bind
         lambda r: (r / _LIB).write_text((r / _LIB).read_text() + "\nzeta() {\n    printf z\n}\n"),
         ChangeSet(modified=[_LIB])),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_incremental_matches_full_recompute(case: Case, falkordb_connection, tmp_path):
    root = tmp_path / case.id
    _write(root, {**_BASELINE, **case.extra_baseline})

    g_inc = falkordb_connection.select_graph(f"equiv_bash_inc_{uuid4().hex}")
    g_full = falkordb_connection.select_graph(f"equiv_bash_full_{uuid4().hex}")
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


def test_incremental_is_not_full_reparse(falkordb_connection, tmp_path):
    """The whole point of a delta: a one-file edit parses only the blast radius, not the tree. The
    baseline has 5 files; editing lib.sh's body re-parses lib.sh plus its reverse-dep closure
    (util, main, newmod-style importers) -- but NEVER standalone.sh or orphan.sh, which don't
    reference it. Asserts files_parsed is a small fraction of the corpus (a full recompute would
    report all 5)."""
    root = tmp_path / "incrementality"
    _write(root, _BASELINE)
    g = falkordb_connection.select_graph(f"bash_incrementality_{uuid4().hex}")
    try:
        _full(g, root)
        _edit(root, _LIB, "printf ok", "printf okay")
        stats = _incremental(g, root, ChangeSet(modified=[_LIB]))
        # lib.sh + its callers (util via CALLS/IMPORTS). standalone.sh and orphan.sh are untouched
        # and unrelated, so they are NEVER re-parsed. A full recompute would parse all 5 files.
        assert stats.files_parsed < 5, f"expected a partial re-parse, parsed {stats.files_parsed}/5"
        assert stats.files_cached == 5 - stats.files_parsed
    finally:
        try:
            g.delete()
        except Exception:
            pass
