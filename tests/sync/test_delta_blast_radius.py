"""Delta-sync PERFORMANCE guard: a one-file edit must stay O(change), not O(project).

The whole value of delta-sync is that editing one file re-parses ~one file, not the repo. A
correctness oracle (test_delta_differential) can't catch a *performance* regression: a change that
makes editing one file drag its whole package/module back in is still node-for-node EQUAL to a full
recompute -- just slow. That regression is exactly what shipped-and-reverted twice during the Rust
work (a crate-root-collapsed `mod tests` hub, and an aggregation-node read-back that pulled every
sibling), so it needs a standing gate.

We assert on FILES RE-PARSED (`files_parsed`/`files_cached`), not wall-clock: it's the deterministic
proxy for delta work (parse + resolve scale with it) and doesn't flake on a loaded CI box. Each
language gets N independent files in ONE aggregation container (package/module, where the cascade
risk lives); a body-only edit to one of them must re-parse a tiny constant, and deleting one leaf
must re-parse ~none. walk_and_assert still checks incremental == full at every hop, so this file is
also a correctness test -- it just additionally bounds the blast radius.

Adding a language: one entry in `_BLAST`. `test_blast_languages_are_gated` fails if a delta-capable
language has no blast corpus, mirroring test_delta_differential's gate.
"""

from __future__ import annotations

import pytest

from ._helpers import GitRepo, lang_report, walk_and_assert

N = 24  # independent files in one container -- big enough that "parse all" != "parse one"
# Max files a single-file BODY edit may re-parse. A correct delta re-parses exactly the 1 edited
# file (v2 blast-radius narrowing: a body change adds/removes no symbol id, so no caller is pulled).
# The cap allows a tiny margin; a cascade regression (pull the package) blows past it immediately.
EDIT_CAP = 2
# Max files a leaf DELETE may re-parse. The deleted file isn't parsed (it's gone) and an aggregation
# node's siblings are refcount-GC'd, not pulled -- so a correct delete re-parses ~0 language files.
DELETE_CAP = 2


# ── per-language corpora: N independent leaf files in one aggregation container ──────────
def _py(repo: GitRepo) -> list[str]:
    base = {"pkg/__init__.py": ""}
    for i in range(N):
        base[f"pkg/m{i:02d}.py"] = f"def f{i:02d}(x):\n    return x + {i}\n"
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"pkg/m00.py": "def f00(x):\n    return x + 100\n"}).commit("edit body of f0"))
    shas.append(repo.rm("pkg/m01.py").commit("delete m1"))
    return shas


def _ts(repo: GitRepo) -> list[str]:
    base = {f"m{i:02d}.ts": f"export function f{i:02d}(x: number): number {{\n  return x + {i};\n}}\n" for i in range(N)}
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"m00.ts": "export function f00(x: number): number {\n  return x + 100;\n}\n"}).commit("edit f0"))
    shas.append(repo.rm("m01.ts").commit("delete m1"))
    return shas


def _bash(repo: GitRepo) -> list[str]:
    base = {f"m{i:02d}.sh": f"f{i:02d}() {{\n  echo $(( $1 + {i} ))\n}}\n" for i in range(N)}
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"m00.sh": "f00() {\n  echo $(( $1 + 100 ))\n}\n"}).commit("edit f0"))
    shas.append(repo.rm("m01.sh").commit("delete m1"))
    return shas


def _c(repo: GitRepo) -> list[str]:
    base = {f"m{i:02d}.c": f"int f{i:02d}(int x) {{\n    return x + {i};\n}}\n" for i in range(N)}
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"m00.c": "int f00(int x) {\n    return x + 100;\n}\n"}).commit("edit f0"))
    shas.append(repo.rm("m01.c").commit("delete m1"))
    return shas


def _go(repo: GitRepo) -> list[str]:
    # All in package pkg -> the flat Package aggregation node is co-authored by all N files.
    base = {f"pkg/m{i:02d}.go": f"package pkg\n\nfunc F{i:02d}(x int) int {{\n\treturn x + {i}\n}}\n" for i in range(N)}
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"pkg/m00.go": "package pkg\n\nfunc F00(x int) int {\n\treturn x + 100\n}\n"}).commit("edit F00"))
    shas.append(repo.rm("pkg/m01.go").commit("delete m1"))
    return shas


def _java(repo: GitRepo) -> list[str]:
    # All in package app -> the Package tier (+ prefix chain) is co-authored by all N files.
    base = {f"F{i:02d}.java": f"package app;\n\npublic class F{i:02d} {{\n    public int m(int x) {{\n        return x + {i};\n    }}\n}}\n"
            for i in range(N)}
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"F00.java": "package app;\n\npublic class F00 {\n    public int m(int x) {\n        return x + 100;\n    }\n}\n"}).commit("edit F0"))
    shas.append(repo.rm("F01.java").commit("delete F1"))
    return shas


def _rust(repo: GitRepo) -> list[str]:
    # lib.rs declares N modules; each module file has one independent fn. Editing a module file's body
    # must not pull lib.rs or the sibling modules (the Module tier is a hub via lib.rs's declarations).
    base = {"lib.rs": "".join(f"pub mod m{i:02d};\n" for i in range(N))}
    for i in range(N):
        base[f"m{i:02d}.rs"] = f"pub fn f{i:02d}(x: i32) -> i32 {{\n    x + {i}\n}}\n"
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"m00.rs": "pub fn f00(x: i32) -> i32 {\n    x + 100\n}\n"}).commit("edit f0"))
    shas.append(repo.rm("m02.rs").write({"lib.rs": "".join(f"pub mod m{i:02d};\n" for i in range(N) if i != 2)}).commit("delete m2"))
    return shas


def _cpp(repo: GitRepo) -> list[str]:
    # N independent free functions, each in its own .cc, all in one `namespace demo` -> the Namespace
    # aggregation node is co-authored by all N files. Editing one file's body must not pull the siblings
    # (aggregation nodes are refcount-GC'd, excluded from the co-definer closure), and deleting a leaf ~0.
    base = {f"m{i:02d}.cc": f"namespace demo {{\nint f{i:02d}(int x) {{\n    return x + {i};\n}}\n}}\n" for i in range(N)}
    shas = [repo.write(base).commit("base")]
    shas.append(repo.write({"m00.cc": "namespace demo {\nint f00(int x) {\n    return x + 100;\n}\n}\n"}).commit("edit f0"))
    shas.append(repo.rm("m01.cc").commit("delete m1"))
    return shas


_BLAST = {
    "python": (_py, "blast_py"),
    "typescript": (_ts, "blast_ts"),
    "bash": (_bash, "blast_bash"),
    "c": (_c, "blast_c"),
    "cpp": (_cpp, "blast_cpp"),
    "go": (_go, "blast_go"),
    "java": (_java, "blast_java"),
    "rust": (_rust, "blast_rust"),
}


@pytest.fixture(params=sorted(_BLAST))
def blast_corpus(request, tmp_path):
    lang = request.param
    builder, project = _BLAST[lang]
    repo = GitRepo.init(tmp_path / "repo")
    shas = builder(repo)
    return lang, repo, shas, tmp_path / "cache", project


def test_single_file_edit_stays_local(blast_corpus, falkordb_connection):
    """A body-only edit to one of N independent files must re-parse a tiny constant, and a leaf
    delete ~none -- not the whole package/module. walk_and_assert also asserts incremental == full at
    each hop, so a regression that pulls the package fails here on the STAT even while staying EQUAL."""
    lang, repo, shas, cache, project = blast_corpus
    reports = walk_and_assert(falkordb_connection, repo, cache, shas, project=project, label=f"{lang} blast")

    edit = lang_report(reports[1], lang)
    assert edit is not None and edit.result is not None, f"{lang}: no result for the edit hop"
    er = edit.result
    assert er.files_parsed <= EDIT_CAP, (
        f"{lang}: a one-file body edit re-parsed {er.files_parsed} files (cap {EDIT_CAP}) -- "
        f"delta blast radius regressed toward a full recompute (cached={er.files_cached})")
    assert er.files_cached >= N - EDIT_CAP, (
        f"{lang}: only {er.files_cached} files served from the graph on a one-file edit (expected ~{N})")

    delete = lang_report(reports[2], lang)
    assert delete is not None and delete.result is not None, f"{lang}: no result for the delete hop"
    dr = delete.result
    assert dr.files_parsed <= DELETE_CAP, (
        f"{lang}: deleting one leaf file re-parsed {dr.files_parsed} files (cap {DELETE_CAP}) -- "
        f"an aggregation node's siblings are being pulled instead of refcount-GC'd")


def test_blast_languages_are_gated():
    """Every delta-capable language must have a blast corpus here (mirrors test_delta_differential's
    gate), so a new language can't ship delta-sync without a standing performance guard."""
    import importlib.util

    from auryn.core.ingest import discover_git_languages

    gated = set(_BLAST)
    missing = []
    for name in discover_git_languages():
        try:
            has_incremental = importlib.util.find_spec(f"auryn.ingestors.{name}.incremental") is not None
        except (ImportError, ValueError):
            has_incremental = False
        if has_incremental and name not in gated:
            missing.append(name)
    assert not missing, f"delta-capable languages missing a blast-radius corpus: {sorted(missing)}"
