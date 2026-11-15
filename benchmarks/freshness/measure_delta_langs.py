"""Delta-sync vs full-recompute, per delta-supported language, on real repos.

Language-agnostic: the git-commit path (`ingest_repository`) is identical for every language, so one
harness benchmarks them all. For each language it shallow-clones a real repo, builds a 2-commit
scenario (base + one representative small edit: append a single function/method to an existing
source file), then times a FULL ingest at the target vs an INCREMENTAL resync (ingest base, then
resync to target) -- both fresh embedded FalkorDB graphs, both the real MCP ingest entry point.

Adding a language = one line in CONFIGS (repo, edit file, snippet). Prints a table of
full_s / incr_s / speedup and how many files the incremental resync re-parsed vs the whole repo.

Run (clones over the network; not part of `uv run pytest`):
    uv run python benchmarks/freshness/measure_delta_langs.py
    uv run python benchmarks/freshness/measure_delta_langs.py --langs c,go   # a subset
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect
from auryn.core.ingest import ingest_repository

# (lang, repo_uri, ref-or-None, subdir, file-to-edit (rel to subdir), appended symbol, project)
CONFIGS = [
    ("c", "https://github.com/redis/redis.git", "7.4.0", "src", "sds.c",
     "\nint zzz_bench_delta(int x) { return x + 1; }\n", "c_bench"),
    ("go", "https://github.com/gin-gonic/gin.git", None, ".", "tree.go",
     "\nfunc ZzzBenchDelta(x int) int { return x + 1 }\n", "go_bench"),
    ("java", "https://github.com/google/gson.git", None, "gson/src/main/java",
     "com/google/gson/FieldAttributes.java",
     "\nclass ZzzBenchDelta { int touch(int x) { return x + 1; } }\n", "java_bench"),
    ("rust", "https://github.com/rust-lang/regex.git", None, "regex-automata/src", "lib.rs",
     "\npub fn zzz_bench_delta(x: usize) -> usize { x + 1 }\n", "rust_bench"),
]


def _git(ws: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True)


def _rev(ws: Path, r: str) -> str:
    return subprocess.run(["git", "-C", str(ws), "rev-parse", r], capture_output=True, text=True).stdout.strip()


def _build_repo(repo_uri: str, ref: str | None, subdir: str, editfile: str, snippet: str, tmp: Path) -> tuple[Path, str, str]:
    """Shallow-clone `repo_uri`@`ref`, take `subdir` as a fresh git repo, commit base, append
    `snippet` to `editfile`, commit target; return (workspace, base_sha, target_sha)."""
    clone = tmp / "clone"
    cmd = ["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [repo_uri, str(clone)]
    subprocess.run(cmd, check=True, capture_output=True)
    ws = tmp / "ws"
    shutil.copytree(clone / subdir, ws, symlinks=False, ignore_dangling_symlinks=True)
    shutil.rmtree(ws / ".git", ignore_errors=True)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@e.com")
    _git(ws, "config", "user.name", "T")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "base")
    p = ws / editfile
    p.write_text(p.read_text() + snippet)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "edit: append one symbol")
    return ws, _rev(ws, "HEAD~1"), _rev(ws, "HEAD")


def _fresh(tmp: Path, tag: str) -> GraphWriter:
    conf = FalkorDBConfig(embedded=True, db_path=str(tmp / f"{tag}.db"), graph_name=f"{tag}_{uuid4().hex[:6]}")
    return GraphWriter(connect(conf))


def _result(report, lang):
    return next((r.result for r in report.languages if r.language == lang and r.result), None)


def _detected(report, lang) -> int:
    return next((r.files_detected for r in report.languages if r.language == lang), 0)


def _measure(lang, repo_uri, ref, subdir, editfile, snippet, project) -> tuple:
    tmp = Path(tempfile.mkdtemp(prefix=f"delta_{lang}_"))
    try:
        ws, base, target = _build_repo(repo_uri, ref, subdir, editfile, snippet, tmp)
        gf = _fresh(tmp, "full")
        t = time.perf_counter()
        repf = ingest_repository(gf, str(ws), ref=target, project_name=project, cache_root=tmp / "cf", deep="off", auto_install=False)
        full = time.perf_counter() - t
        gi = _fresh(tmp, "incr")
        ingest_repository(gi, str(ws), ref=base, project_name=project, cache_root=tmp / "ci", deep="off", auto_install=False)
        t = time.perf_counter()
        repi = ingest_repository(gi, str(ws), ref=target, project_name=project, cache_root=tmp / "ci", deep="off", auto_install=False)
        incr = time.perf_counter() - t
        ri = _result(repi, lang)
        try:
            gf._graph.delete()
            gi._graph.delete()
        except Exception:
            pass
        return (lang, _detected(repf, lang), full, incr, (full / incr) if incr else 0.0,
                ri.files_parsed, ri.files_cached,
                ri.nodes_added + ri.nodes_updated + ri.nodes_removed,
                ri.edges_added + ri.edges_updated + ri.edges_removed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="Delta-sync vs full-recompute per language")
    p.add_argument("--langs", help="comma-separated subset of languages (default: all)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)  # silence the benign reconcile 'ambiguous linkage' warnings

    want = {s.strip() for s in args.langs.split(",")} if args.langs else None
    configs = [c for c in CONFIGS if not want or c[0] in want]

    rows = [_measure(*c) for c in configs]
    print(f"\n{'lang':<6}{'files':>7}{'full_s':>10}{'incr_s':>10}{'speedup':>10}{'parsed':>8}{'cached':>8}{'dNodes':>8}{'dEdges':>8}")
    print("-" * 75)
    for lang, files, full, incr, sp, parsed, cached, dn, de in rows:
        print(f"{lang:<6}{files:>7}{full:>10.3f}{incr:>10.3f}{sp:>9.1f}x{parsed:>8}{cached:>8}{dn:>8}{de:>8}")
    print("-" * 75)
    print("full_s = full recompute of the repo; incr_s = incremental resync of a one-symbol edit")
    print("(real git-commit path, embedded FalkorDB; parsed/cached = files re-parsed vs served from the graph)")


if __name__ == "__main__":
    main(sys.argv[1:])
