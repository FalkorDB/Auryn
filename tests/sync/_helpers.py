"""Shared harness for the graph-sync test suite (tests/sync).

Covers the four freshness triggers against the real ingest path:
  * git commit   -- advance a branch, re-ingest at the new sha
  * git checkout -- re-ingest at a different branch's tip
  * file save    -- an uncommitted edit in a git working tree (ingest_directory)
  * project save -- a plain (non-git) directory re-ingest (ingest_directory)

Every test builds real repos, applies a change (simple or a mixture), fires a trigger, and
checks two things: correctness (the resulting graph is node-for-node / edge-for-edge equal
to a full recompute of the same final state) and, where an incremental path exists, that it
was actually taken (only the blast radius parsed) plus its wall-clock.

Deliberately independent of the ingestors' internals -- it drives ingest_repository /
ingest_directory (deep="off", so the fast `ast` engine, which is what incremental resync
covers) exactly as the MCP tools do.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory, ingest_repository


# ── git repo builder ────────────────────────────────────────────────────────────────
def run_git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class GitRepo:
    """A throwaway git repo you can script: write/rm/mv files, commit, branch, checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def init(cls, root: Path) -> "GitRepo":
        root.mkdir(parents=True, exist_ok=True)
        run_git(root, "init", "-q", "-b", "main")
        run_git(root, "config", "user.email", "t@example.com")
        run_git(root, "config", "user.name", "Test")
        return cls(root)

    def write(self, files: dict[str, str]) -> "GitRepo":
        for rel, content in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return self

    def rm(self, *paths: str) -> "GitRepo":
        for rel in paths:
            (self.root / rel).unlink()
        return self

    def mv(self, src: str, dst: str) -> "GitRepo":
        d = self.root / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        (self.root / src).rename(d)
        return self

    def commit(self, msg: str = "c") -> str:
        run_git(self.root, "add", "-A")
        run_git(self.root, "commit", "-q", "-m", msg)
        return self.head()

    def head(self) -> str:
        return run_git(self.root, "rev-parse", "HEAD")

    def checkout(self, branch: str, create: bool = False) -> None:
        run_git(self.root, "checkout", "-q", *(["-b"] if create else []), branch)


# ── ingest wrappers (the two production entry points, fast/ast path) ─────────────────
def ingest_repo(writer: GraphWriter, repo: GitRepo, ref: str, cache_root: Path, project: str):
    """Ingest a git repo at `ref` (branch / tag / sha) -- the git-commit/checkout path."""
    return ingest_repository(writer, str(repo.root), ref=ref, project_name=project,
                             cache_root=cache_root, deep="off", auto_install=False)


def ingest_dir(writer: GraphWriter, path: Path, project: str):
    """Ingest a working directory -- the file-save / project-save path."""
    return ingest_directory(writer, str(path), project_name=project, deep="off", auto_install=False)


def python_report(report):
    """The python LanguageReport from a RepoIngestReport, or None if python didn't run."""
    return next((r for r in report.languages if r.language == "python"), None)


def lang_report(report, lang: str):
    """The LanguageReport for `lang` from a RepoIngestReport, or None if it didn't run."""
    return next((r for r in report.languages if r.language == lang), None)


# ── graph dump + comparison ──────────────────────────────────────────────────────────
def dump(graph, id_prefix: str = "") -> tuple[dict, dict]:
    """(nodes, edges) as plain dicts, `_fingerprint` stripped, optionally scoped to ids under
    `id_prefix` (for per-project isolation checks). Order-independent (keyed by label+id /
    type+endpoints)."""
    nodes = {
        (label, nid): {k: v for k, v in props.items() if k != "_fingerprint"}
        for label, nid, props in graph.ro_query(
            "MATCH (n) WHERE n.id STARTS WITH $p RETURN labels(n)[0], n.id, properties(n)",
            {"p": id_prefix},
        ).result_set
    }
    edges = {
        (rel, aid, bid): {k: v for k, v in props.items() if k != "_fingerprint"}
        for rel, aid, bid, props in graph.ro_query(
            "MATCH (a)-[r]->(b) WHERE a.id STARTS WITH $p RETURN type(r), a.id, b.id, properties(r)",
            {"p": id_prefix},
        ).result_set
    }
    return nodes, edges


def graph_diff(expected: tuple[dict, dict], actual: tuple[dict, dict]) -> list[str]:
    """Human-readable differences between two (nodes, edges) dumps; empty list == identical."""
    msgs: list[str] = []
    for kind, exp, act in (("node", expected[0], actual[0]), ("edge", expected[1], actual[1])):
        for key in exp.keys() - act.keys():
            msgs.append(f"{kind} missing from actual: {key}")
        for key in act.keys() - exp.keys():
            msgs.append(f"{kind} unexpected in actual: {key}")
        for key in exp.keys() & act.keys():
            if exp[key] != act[key]:
                msgs.append(f"{kind} prop diff {key}:\n     expected={exp[key]}\n     actual  ={act[key]}")
    return msgs


def assert_graphs_equal(expected: tuple[dict, dict], actual: tuple[dict, dict], label: str = "") -> None:
    problems = graph_diff(expected, actual)
    assert not problems, f"{label} incremental != full recompute:\n" + "\n".join(problems[:40])


# ── timing ────────────────────────────────────────────────────────────────────────────
@dataclass
class Timed:
    result: object
    seconds: float


def timed(fn) -> Timed:
    t0 = time.perf_counter()
    r = fn()
    return Timed(r, time.perf_counter() - t0)


# ── graph fixtures ──────────────────────────────────────────────────────────────────
def fresh_graph(conn, tag: str = "sync"):
    return conn.select_graph(f"{tag}_{uuid4().hex}")


def drop(*graphs) -> None:
    for g in graphs:
        try:
            g.delete()
        except Exception:
            pass


# ── differential delta-sync harness ───────────────────────────────────────────────────
def walk_and_assert(conn, repo: GitRepo, cache_root: Path, refs, *, project: str, label: str = ""):
    """Drive ONE incremental graph through `refs` in order; at every hop assert it is
    node-for-node / edge-for-edge equal to a *fresh full* ingest at that same ref.

    This exercises the core delta-sync invariant — apply_delta(full(A), diff A→B) == full(B)
    — across arbitrary commit sequences: forward advances, *backward* jumps, long leaps, and
    repeats. `refs` may revisit commits; each hop is compared independently, so residue left
    behind on a backward switch (a stale node/edge the delta failed to remove) fails here.

    Returns the per-hop incremental RepoIngestReport list so callers can also assert the
    incremental path was actually taken (how little re-parsed).

    IMPORTANT: every ref in `refs` must be syntactically valid in every language. The
    last-known-good behaviour on broken files deliberately makes delta != full at a broken
    commit (the delta keeps the prior good symbols; a fresh full ingest has none to keep), so
    broken commits belong in a dedicated last-known-good test (test_broken_code.py), never in
    this equality oracle.
    """
    expected: dict[str, tuple[dict, dict]] = {}  # ref -> full-ingest dump, computed once per ref
    incr = fresh_graph(conn, "walk_incr")
    reports = []
    try:
        for i, ref in enumerate(refs):
            reports.append(ingest_repo(GraphWriter(incr), repo, ref, cache_root, project))
            if ref not in expected:  # the full-recompute oracle for this commit (revisits are free)
                full = fresh_graph(conn, "walk_full")
                try:
                    ingest_repo(GraphWriter(full), repo, ref, cache_root, project)
                    expected[ref] = dump(full)
                finally:
                    drop(full)
            assert_graphs_equal(expected[ref], dump(incr), f"{label} hop {i} → {str(ref)[:12]}")
    finally:
        drop(incr)
    return reports
