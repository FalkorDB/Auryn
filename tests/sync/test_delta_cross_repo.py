"""Cross-repo differential oracle for authored-shared delta-sync (ADR 0005).

The single-repo oracle (test_delta_differential.py) proves `apply_delta(full(A), diff) == full(B)`
for one project. This file proves the same invariant when MULTIPLE repos share one graph and the
edited repo has cross-repo edges in BOTH directions — it EXPORTS a symbol another repo calls, and
IMPORTS a symbol another repo defines. That is exactly where an incremental resync can silently
corrupt a *neighbour* repo's edges: deleting an exported definition must demote the consumer's edge
to a placeholder (not drop it), and re-adding it must re-bind — all while never re-parsing the
neighbour. Each hop re-ingests ONLY the edited repo (incrementally) and asserts the WHOLE graph
(both repos + any cross-repo placeholders) equals a from-scratch ingest of both repos at that state.

Scenarios:
  * test_cross_repo_bidirectional — repo A evolves (body change, export delete/re-add, import
    drop/add) while repo B is stable and linked to A both ways; forward AND a backward leap.
  * test_multi_repo_isolated — two unrelated C repos in one graph; editing one must leave the
    other's subgraph byte-identical.
"""

from __future__ import annotations

import pytest

from ._helpers import GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_repo


# ── C cross-repo corpus ─────────────────────────────────────────────────────────────────
def _build_c_repo_b(repo: GitRepo) -> str:
    """Stable neighbour: defines `b_export` (imported by A) and calls `a_export` (exported by A).
    The cross-repo call is bare (no local prototype), so it resolves to an `extern://c/…`
    placeholder that reconciliation folds onto A's definition — the clean cross-repo path."""
    repo.write({
        "b.h": "int b_export(int x);\n",
        "b.c": '#include "b.h"\n'
               "int b_export(int x) { return x + 1; }\n"
               "int b_uses_a(void) { return a_export(); }\n",
    })
    return repo.commit("b1: define b_export; b_uses_a calls A's a_export")


def _build_c_repo_a(repo: GitRepo) -> list[str]:
    """Evolving repo: exports `a_export` (called by B) and imports `b_export` (defined by B)."""
    shas = []

    repo.write({
        "a.h": "int a_export(void);\n",
        "a.c": '#include "a.h"\n'
               "int a_uses_b(void) { return b_export(2); }\n"
               "int a_export(void) { return 5; }\n",
    })
    shas.append(repo.commit("a1: a_uses_b calls B's b_export; define a_export"))

    # a2: change a_uses_b's body, still imports b_export (import edge must survive a body edit)
    repo.write({"a.c": '#include "a.h"\n'
                       "int a_uses_b(void) { return b_export(2) + 7; }\n"
                       "int a_export(void) { return 5; }\n"})
    shas.append(repo.commit("a2: a_uses_b body change, still calls b_export"))

    # a3: REMOVE the exported a_export entirely -> B's b_uses_a->a_export must demote to a
    #     placeholder (B is never re-parsed), matching a from-scratch ingest where B's bare call
    #     is an unresolved extern.
    repo.write({
        "a.h": "/* a_export removed */\n",
        "a.c": '#include "a.h"\nint a_uses_b(void) { return b_export(2) + 7; }\n',
    })
    shas.append(repo.commit("a3: remove a_export (export deleted -> consumer demotes)"))

    # a4: RE-ADD a_export -> B's placeholder must re-bind (upgrade) onto A's definition.
    repo.write({
        "a.h": "int a_export(void);\n",
        "a.c": '#include "a.h"\n'
               "int a_uses_b(void) { return b_export(2) + 7; }\n"
               "int a_export(void) { return 9; }\n",
    })
    shas.append(repo.commit("a4: re-add a_export (export restored -> consumer re-binds)"))

    # a5: a_uses_b STOPS importing b_export -> A->B edge dropped (B's b_export keeps its other caller)
    repo.write({"a.c": '#include "a.h"\n'
                       "int a_uses_b(void) { return 0; }\n"
                       "int a_export(void) { return 9; }\n"})
    shas.append(repo.commit("a5: a_uses_b stops calling b_export (import dropped)"))

    # a6: add a new function that imports b_export -> a fresh A->B edge
    repo.write({"a.c": '#include "a.h"\n'
                       "int a_uses_b(void) { return 0; }\n"
                       "int a_export(void) { return 9; }\n"
                       "int a_new(void) { return b_export(3); }\n"})
    shas.append(repo.commit("a6: add a_new calling b_export (import re-added)"))

    # a7: RENAME a.c -> impl.c while it still defines the exported a_export and imports b_export --
    #     the DEFINES edge moves file, but both cross-repo edges (B->a_export, A->b_export) must
    #     survive (rename composed with live cross-repo links, both directions).
    repo.mv("a.c", "impl.c")
    shas.append(repo.commit("a7: rename a.c->impl.c (cross-repo definer moves file)"))

    return shas


# ── Go cross-repo corpus (same battery, qualified-package imports) ───────────────────────
def _build_go_repo_b(repo: GitRepo) -> str:
    """Stable Go neighbour (package b): exports `BExport` (imported by A) and calls A's `AExport`
    via a qualified import of package a -> an `extern://go/a.AExport` placeholder reconciliation
    folds onto A's definition."""
    repo.write({
        "b.go": 'package b\n\nimport "a"\n\n'
                "func BExport(x int) int {\n\treturn x + 1\n}\n\n"
                "func BUsesA() int {\n\treturn a.AExport()\n}\n",
    })
    return repo.commit("b1: define BExport; BUsesA calls A's a.AExport")


def _build_go_repo_a(repo: GitRepo) -> list[str]:
    """Evolving Go repo (package a): exports `AExport` (called by B) and imports B's `BExport`."""
    shas = []
    repo.write({"a.go": 'package a\n\nimport "b"\n\n'
                        "func AUsesB() int {\n\treturn b.BExport(2)\n}\n\n"
                        "func AExport() int {\n\treturn 5\n}\n"})
    shas.append(repo.commit("a1: AUsesB calls B's b.BExport; define AExport"))

    repo.write({"a.go": 'package a\n\nimport "b"\n\n'
                        "func AUsesB() int {\n\treturn b.BExport(2) + 7\n}\n\n"
                        "func AExport() int {\n\treturn 5\n}\n"})
    shas.append(repo.commit("a2: AUsesB body change, still calls b.BExport"))

    repo.write({"a.go": 'package a\n\nimport "b"\n\nfunc AUsesB() int {\n\treturn b.BExport(2) + 7\n}\n'})
    shas.append(repo.commit("a3: remove AExport (export deleted -> consumer demotes)"))

    repo.write({"a.go": 'package a\n\nimport "b"\n\n'
                        "func AUsesB() int {\n\treturn b.BExport(2) + 7\n}\n\n"
                        "func AExport() int {\n\treturn 9\n}\n"})
    shas.append(repo.commit("a4: re-add AExport (export restored -> consumer re-binds)"))

    repo.write({"a.go": "package a\n\nfunc AUsesB() int {\n\treturn 0\n}\n\nfunc AExport() int {\n\treturn 9\n}\n"})
    shas.append(repo.commit("a5: AUsesB stops calling b.BExport (import dropped)"))

    repo.write({"a.go": 'package a\n\nimport "b"\n\n'
                        "func AUsesB() int {\n\treturn 0\n}\n\n"
                        "func AExport() int {\n\treturn 9\n}\n\n"
                        "func ANew() int {\n\treturn b.BExport(3)\n}\n"})
    shas.append(repo.commit("a6: add ANew calling b.BExport (import re-added)"))

    repo.mv("a.go", "impl.go")
    shas.append(repo.commit("a7: rename a.go->impl.go (cross-repo definer moves file)"))

    return shas


# ── Java cross-repo corpus (same battery, single-type imports name the owner fqcn) ───────
def _build_java_repo_b(repo: GitRepo) -> str:
    """Stable Java neighbour (package b): exports `help` (called by A) and calls A's `exec` via a
    static `A.exec()` on an imported `a.A` -> an `extern://java/a.A#exec` placeholder that
    reconciliation folds onto A's definition."""
    repo.write({
        "B.java": "package b;\n\nimport a.A;\n\n"
                  "public class B {\n"
                  "    public int help(int x) {\n        return x + 1;\n    }\n\n"
                  "    public int usesA() {\n        return A.exec();\n    }\n}\n",
    })
    return repo.commit("b1: define B.help; B.usesA calls A's a.A#exec")


def _build_java_repo_a(repo: GitRepo) -> list[str]:
    """Evolving Java repo (package a): exports `exec` (called by B) and imports B's `help`."""
    shas = []

    def a_src(body_exec: str, uses_b_body: str, extra: str = "") -> dict[str, str]:
        cls = "package a;\n\nimport b.B;\n\npublic class A {\n"
        cls += f"    public int usesB() {{\n        {uses_b_body}\n    }}\n"
        if body_exec:
            cls += f"\n    public int exec() {{\n        {body_exec}\n    }}\n"
        cls += extra
        cls += "}\n"
        return {"A.java": cls}

    repo.write(a_src("return 5;", "return B.help(2);"))
    shas.append(repo.commit("a1: usesB calls B's b.B#help; define exec"))

    repo.write(a_src("return 5;", "return B.help(2) + 7;"))
    shas.append(repo.commit("a2: usesB body change, still calls B.help"))

    # a3: REMOVE the exported exec -> B's usesA->exec must demote to a placeholder (B is never
    #     re-parsed), matching a from-scratch ingest where B's static call is an unresolved extern.
    repo.write(a_src("", "return B.help(2) + 7;"))
    shas.append(repo.commit("a3: remove exec (export deleted -> consumer demotes)"))

    repo.write(a_src("return 9;", "return B.help(2) + 7;"))
    shas.append(repo.commit("a4: re-add exec (export restored -> consumer re-binds)"))

    # a5: usesB stops calling B.help -> A->B edge dropped (B.help keeps its structure; no A caller)
    repo.write(a_src("return 9;", "return 0;"))
    shas.append(repo.commit("a5: usesB stops calling B.help (import use dropped)"))

    # a6: add a new method that calls B.help -> a fresh A->B edge
    repo.write(a_src("return 9;", "return 0;",
                     extra="\n    public int fresh() {\n        return B.help(3);\n    }\n"))
    shas.append(repo.commit("a6: add fresh() calling B.help (import re-added)"))

    # a7: RENAME A.java -> Impl.java (public class stays A) -- the DEFINES edges move file, but both
    #     cross-repo edges (B->exec, A->help) must survive (rename composed with live cross links).
    repo.mv("A.java", "Impl.java")
    shas.append(repo.commit("a7: rename A.java->Impl.java (cross-repo definer moves file)"))

    return shas


# ── Rust cross-repo corpus (same battery, crate-qualified free-function paths) ───────────
def _build_rust_repo_b(repo: GitRepo) -> str:
    """Stable Rust neighbour (crate rust_repo_b): exports `b_export` (imported by A) and calls A's
    `a_export` via the crate-qualified path `rust_repo_a::a_export()` -> an
    `extern://rust/rust_repo_a::a_export` placeholder reconciliation folds onto A's definition."""
    repo.write({
        "b.rs": "pub fn b_export(x: i32) -> i32 {\n    x + 1\n}\n\n"
                "pub fn b_uses_a() -> i32 {\n    rust_repo_a::a_export()\n}\n",
    })
    return repo.commit("b1: define b_export; b_uses_a calls A's rust_repo_a::a_export")


def _build_rust_repo_a(repo: GitRepo) -> list[str]:
    """Evolving Rust crate (rust_repo_a): exports `a_export` (called by B) and imports B's `b_export`."""
    shas = []

    repo.write({"a.rs": "pub fn a_uses_b() -> i32 {\n    rust_repo_b::b_export(2)\n}\n\n"
                        "pub fn a_export() -> i32 {\n    5\n}\n"})
    shas.append(repo.commit("a1: a_uses_b calls B's rust_repo_b::b_export; define a_export"))

    repo.write({"a.rs": "pub fn a_uses_b() -> i32 {\n    rust_repo_b::b_export(2) + 7\n}\n\n"
                        "pub fn a_export() -> i32 {\n    5\n}\n"})
    shas.append(repo.commit("a2: a_uses_b body change, still calls b_export"))

    # a3: REMOVE the exported a_export -> B's b_uses_a->a_export must demote to a placeholder (B is
    #     never re-parsed), matching a from-scratch ingest where B's crate-qualified call is extern.
    repo.write({"a.rs": "pub fn a_uses_b() -> i32 {\n    rust_repo_b::b_export(2) + 7\n}\n"})
    shas.append(repo.commit("a3: remove a_export (export deleted -> consumer demotes)"))

    # a4: RE-ADD a_export -> B's placeholder must re-bind (upgrade) onto A's definition.
    repo.write({"a.rs": "pub fn a_uses_b() -> i32 {\n    rust_repo_b::b_export(2) + 7\n}\n\n"
                        "pub fn a_export() -> i32 {\n    9\n}\n"})
    shas.append(repo.commit("a4: re-add a_export (export restored -> consumer re-binds)"))

    # a5: a_uses_b STOPS importing b_export -> A->B edge dropped (B's b_export keeps its other caller)
    repo.write({"a.rs": "pub fn a_uses_b() -> i32 {\n    0\n}\n\n"
                        "pub fn a_export() -> i32 {\n    9\n}\n"})
    shas.append(repo.commit("a5: a_uses_b stops calling b_export (import dropped)"))

    # a6: add a new function that imports b_export -> a fresh A->B edge
    repo.write({"a.rs": "pub fn a_uses_b() -> i32 {\n    0\n}\n\n"
                        "pub fn a_export() -> i32 {\n    9\n}\n\n"
                        "pub fn a_new() -> i32 {\n    rust_repo_b::b_export(3)\n}\n"})
    shas.append(repo.commit("a6: add a_new calling b_export (import re-added)"))

    # a7: RENAME a.rs -> impl.rs while it still defines a_export and imports b_export -- the DEFINES
    #     edge moves file, but both cross-repo edges (B->a_export, A->b_export) must survive.
    repo.mv("a.rs", "impl.rs")
    shas.append(repo.commit("a7: rename a.rs->impl.rs (cross-repo definer moves file)"))

    return shas


# ── multi-repo differential harness ─────────────────────────────────────────────────────
def _walk_multi(conn, cache, projects, steps, *, label):
    """`projects`: list of (repo, project_name). `steps`: list of {project_name: ref}. At each step,
    (re-)ingest into a PERSISTENT graph only the projects whose ref changed from the prior step (the
    edited repo takes the incremental path; the neighbour is left alone), then assert the whole graph
    equals a FRESH full ingest of every project at that step's refs."""
    incr = fresh_graph(conn, "xrepo_incr")
    prev: dict[str, str] = {}
    from auryn.core.graph_client import GraphWriter
    try:
        for i, step in enumerate(steps):
            for repo, proj in projects:
                if prev.get(proj) != step[proj]:
                    ingest_repo(GraphWriter(incr), repo, step[proj], cache, proj)
            prev = dict(step)
            full = fresh_graph(conn, "xrepo_full")
            try:
                for repo, proj in projects:
                    ingest_repo(GraphWriter(full), repo, step[proj], cache, proj)
                assert_graphs_equal(dump(full), dump(incr), f"{label} step {i}")
            finally:
                drop(full)
    finally:
        drop(incr)


# Registry: authored-shared, cross-repo-capable languages (a `linkage_key` + demote path). Each
# supplies (build stable repo B, build evolving repo A, the bidirectional walk order). To gate a new
# such language, add its two builders here -- it inherits the whole cross-repo battery.
_CROSS_REPO = {
    "c": (_build_c_repo_b, _build_c_repo_a, [0, 1, 2, 3, 4, 5, 6, 2, 6]),
    "go": (_build_go_repo_b, _build_go_repo_a, [0, 1, 2, 3, 4, 5, 6, 2, 6]),
    "java": (_build_java_repo_b, _build_java_repo_a, [0, 1, 2, 3, 4, 5, 6, 2, 6]),
    "rust": (_build_rust_repo_b, _build_rust_repo_a, [0, 1, 2, 3, 4, 5, 6, 2, 6]),
}


@pytest.fixture(params=sorted(_CROSS_REPO))
def cross_repo(request, tmp_path):
    lang = request.param
    build_b, build_a, order = _CROSS_REPO[lang]
    a = GitRepo.init(tmp_path / "repo_a")
    b = GitRepo.init(tmp_path / "repo_b")
    a_shas = build_a(a)
    b_sha = build_b(b)
    return lang, a, a_shas, b, b_sha, tmp_path / "cache", order


def test_cross_repo_bidirectional(cross_repo, falkordb_connection):
    """Repo A (evolving) both exports to and imports from stable repo B; every hop's incremental
    resync of A must leave the whole two-repo graph identical to a from-scratch ingest of both.
    Forward through all of A's commits (incl. the rename), then backward leaps to catch stale
    residue on both a definition-delete (demote) and a file-rename with live cross edges."""
    lang, a, a_shas, b, b_sha, cache, order = cross_repo
    pa, pb = f"{lang}_repo_a", f"{lang}_repo_b"
    projects = [(a, pa), (b, pb)]
    steps = [{pa: a_shas[i], pb: b_sha} for i in order]
    _walk_multi(falkordb_connection, cache, projects, steps, label=f"{lang} cross-repo bidirectional")


def test_multi_repo_isolated(cross_repo, falkordb_connection):
    """Two repos in one graph: editing A must never perturb B's subgraph. B has cross edges TO A, so
    whole-graph equality at each hop is the stronger isolation test (a symbol-disjoint repo is a
    strict subset of this)."""
    lang, a, a_shas, b, b_sha, cache, order = cross_repo
    pa, pb = f"{lang}_repo_a", f"{lang}_repo_b"
    projects = [(a, pa), (b, pb)]
    steps = [{pa: a_shas[i], pb: b_sha} for i in (0, 1, 4, 5)]
    _walk_multi(falkordb_connection, cache, projects, steps, label=f"{lang} multi-repo")
