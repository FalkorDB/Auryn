"""Freshness benchmark: how expensive is keeping the graph up to date after a *small*
source edit?

The re-ingest model (see core/graph_sync.py) never diffs at the source level -- it
recomputes the whole desired target state for the language (parse every file, resolve
globally), reads the entire current subgraph back out of FalkorDB to diff against, and
writes only the delta. So the cost of reflecting a one-line change is almost entirely
independent of how small that change is. This harness measures exactly that: it warms a
graph once (a full cold ingest of a real tree), then replays a matrix of realistic small
edits against it, timing each resync broken down by phase:

    total = parse + resolve + read_fingerprints + diff + write   (+ other: schema/detect/enrichment)
            \_______________________/   \_________________________________/
              recompute target state       reconcile with the graph

The phase split comes from core/instrument.py (off by default; this harness turns it on
around each resync). The headline question is how much of a trivial edit's cost is
`read_fingerprints` -- the whole-subgraph read-back that has been profiled at ~87% of an
incremental resync elsewhere -- versus work that actually depends on what changed.

Isolation: every scenario is measured against the *same* warm baseline. After the measured
resync, the mutation is undone and an unmeasured "restore" resync returns the graph to the
baseline, so scenarios don't accumulate. A synthetic sentinel module (planted before the
cold ingest) gives the entity-level scenarios -- edit a body, change a signature, add/delete
a function/file -- crisp, repo-independent deltas, while the surrounding real tree provides
the read-back scale that makes the numbers meaningful.

Self-contained: no pytest, no running server required. Uses ingest_directory (the same path
the MCP `index_repository` tool takes), and an embedded FalkorDB by default (falls back to a
localhost server if one is up). Nothing here is part of `uv run pytest`.

Run:
    # against a local source tree (no network) -- fastest way to a baseline:
    uv run python benchmarks/freshness/run_freshness_benchmark.py \
        --repo-dir packages --project-name auryn-src \
        --out benchmarks/results/freshness.json

    # against a real public repo (clones it):
    uv run python benchmarks/freshness/run_freshness_benchmark.py \
        --repo-uri https://github.com/django/django.git --ref 5.1 --project-name django

Every flag is also an env var (FRESHNESS_BENCH_*); see Config.from_args.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

# benchmarks/ is not a package on sys.path the way the installed `auryn` is; allow both
# `python benchmarks/freshness/run_freshness_benchmark.py` and `-m` invocation to find report.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from report import print_summary  # noqa: E402

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect  # noqa: E402
from auryn.core.ingest import ingest_directory, join_deep_phase  # noqa: E402
from auryn.core.instrument import record_phases  # noqa: E402

logger = logging.getLogger("freshness_benchmark")

# Directories never worth copying into the throwaway workspace (heavy, non-source, or
# regenerated) -- keeps `--repo-dir` copies fast and the ingest focused on real source.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info", ".DS_Store",
    "results", "results-*", "results-corpus", "results-swe",
)

_SENTINEL_NAME = "zzz_freshness_bench_sentinel.py"
_ADDED_NAME = "zzz_freshness_bench_added.py"
_RENAMED_NAME = "zzz_freshness_bench_renamed.py"


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    repo_dir: str | None
    repo_uri: str | None
    ref: str | None
    project_name: str
    deep: str
    falkordb: str            # "auto" | "embedded" | "host:port"
    n_functions: int
    bulk_files: int
    scenarios: tuple[str, ...] | None   # None = all
    out: str | None
    keep_workspace: bool
    verbose: bool

    @staticmethod
    def from_args(argv: list[str]) -> "Config":
        e = os.environ.get
        p = argparse.ArgumentParser(description="Graph freshness (incremental-resync) benchmark")
        p.add_argument("--repo-dir", default=e("FRESHNESS_BENCH_REPO_DIR"),
                       help="Local source tree to copy + benchmark (no network). Mutually exclusive with --repo-uri.")
        p.add_argument("--repo-uri", default=e("FRESHNESS_BENCH_REPO_URI"),
                       help="Git repo to shallow-clone + benchmark.")
        p.add_argument("--ref", default=e("FRESHNESS_BENCH_REF"), help="Ref/tag/branch for --repo-uri.")
        p.add_argument("--project-name", default=e("FRESHNESS_BENCH_PROJECT", "freshness-bench"))
        p.add_argument("--deep", default=e("FRESHNESS_BENCH_DEEP", "off"), choices=("off", "inline"),
                       help="'off' (default) measures the fast/structural path; 'inline' also runs deep engines (e.g. Python Jedi).")
        p.add_argument("--falkordb", default=e("FRESHNESS_BENCH_FALKORDB", "auto"),
                       help="'auto' (localhost:6379 if up, else embedded), 'embedded', or 'host:port'.")
        p.add_argument("--n-functions", type=int, default=int(e("FRESHNESS_BENCH_NFUNCS", "40")),
                       help="How many functions the planted sentinel module holds (drives the comment_top line-churn signal).")
        p.add_argument("--bulk-files", type=int, default=int(e("FRESHNESS_BENCH_BULK", "25")),
                       help="How many real files the bulk_edit scenario touches.")
        p.add_argument("--scenarios", default=e("FRESHNESS_BENCH_SCENARIOS"),
                       help="Comma-separated subset of scenario names to run (default: all).")
        p.add_argument("--out", default=e("FRESHNESS_BENCH_OUT"), help="Write full results JSON here.")
        p.add_argument("--keep-workspace", action="store_true", default=bool(e("FRESHNESS_BENCH_KEEP")))
        p.add_argument("--verbose", action="store_true", default=bool(e("FRESHNESS_BENCH_VERBOSE")))
        a = p.parse_args(argv)
        if not a.repo_dir and not a.repo_uri:
            p.error("one of --repo-dir or --repo-uri is required")
        if a.repo_dir and a.repo_uri:
            p.error("--repo-dir and --repo-uri are mutually exclusive")
        scen = tuple(s.strip() for s in a.scenarios.split(",") if s.strip()) if a.scenarios else None
        return Config(
            repo_dir=a.repo_dir, repo_uri=a.repo_uri, ref=a.ref, project_name=a.project_name,
            deep=a.deep, falkordb=a.falkordb, n_functions=a.n_functions, bulk_files=a.bulk_files,
            scenarios=scen, out=a.out, keep_workspace=a.keep_workspace, verbose=a.verbose,
        )


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------
@dataclass
class Metrics:
    scenario: str
    description: str
    total_s: float
    parse_s: float
    resolve_s: float
    read_fingerprints_s: float
    diff_s: float
    write_s: float
    other_s: float                 # total - (parse+resolve+readback+diff+write): schema/detect/enrichment/overhead
    readback_pct: float            # read_fingerprints as % of total
    nodes_added: int
    nodes_updated: int
    nodes_removed: int
    edges_added: int
    edges_updated: int
    edges_removed: int
    files_parsed: int
    files_cached: int

    @property
    def delta_total(self) -> int:
        return (self.nodes_added + self.nodes_updated + self.nodes_removed
                + self.edges_added + self.edges_updated + self.edges_removed)


def _build_metrics(scenario: str, description: str, report, phases: dict[str, float]) -> Metrics:
    total = report.elapsed_seconds
    readback = phases.get("read_fingerprints", 0.0)
    diff = phases.get("diff", 0.0)
    write = phases.get("write", 0.0)
    parse = phases.get("parse", 0.0)
    resolve = phases.get("resolve", 0.0)
    other = max(0.0, total - (parse + resolve + readback + diff + write))

    def s(attr: str) -> int:
        return sum(getattr(r.result, attr) for r in report.languages if r.result is not None)

    return Metrics(
        scenario=scenario, description=description,
        total_s=round(total, 4), parse_s=round(parse, 4), resolve_s=round(resolve, 4),
        read_fingerprints_s=round(readback, 4), diff_s=round(diff, 4), write_s=round(write, 4),
        other_s=round(other, 4),
        readback_pct=round(100.0 * readback / total, 1) if total > 0 else 0.0,
        nodes_added=s("nodes_added"), nodes_updated=s("nodes_updated"), nodes_removed=s("nodes_removed"),
        edges_added=s("edges_added"), edges_updated=s("edges_updated"), edges_removed=s("edges_removed"),
        files_parsed=s("files_parsed"), files_cached=s("files_cached"),
    )


def _resync(writer: GraphWriter, workspace: Path, cfg: Config, scenario: str, description: str) -> Metrics:
    with record_phases() as phases:
        report = ingest_directory(writer, str(workspace), project_name=cfg.project_name, deep=cfg.deep)
    join_deep_phase()  # harmless for deep=off/inline; guards against a stray background thread
    return _build_metrics(scenario, description, report, phases)


def _restore(writer: GraphWriter, workspace: Path, cfg: Config) -> None:
    """Unmeasured resync that returns the graph to the current (already-reverted) workspace
    state, so the next scenario starts from the identical warm baseline."""
    with record_phases():
        ingest_directory(writer, str(workspace), project_name=cfg.project_name, deep=cfg.deep)
    join_deep_phase()


# --------------------------------------------------------------------------------------
# Workspace + sentinel
# --------------------------------------------------------------------------------------
def _sentinel_source(n_functions: int) -> str:
    """A synthetic module of `n_functions` trivial top-level functions plus one caller that
    calls the first few (so there are CALLS edges to churn). Deterministic strings the
    scenario mutations below match against exactly."""
    lines = ['"""Synthetic sentinel planted by the freshness benchmark (not real source)."""', ""]
    for i in range(n_functions):
        lines += [f"def bench_fn_{i:03d}(value):", f"    return value + {i}", ""]
    lines += ["def bench_caller():", "    total = 0"]
    for i in range(min(5, n_functions)):
        lines += [f"    total += bench_fn_{i:03d}(total)"]
    lines += ["    return total", ""]
    return "\n".join(lines)


def prepare_workspace(cfg: Config) -> tuple[Path, Path]:
    """Materialize a throwaway workspace (copy of --repo-dir, or shallow clone of --repo-uri),
    plant the sentinel module, and return (tmp_root, workspace_dir)."""
    tmp_root = Path(tempfile.mkdtemp(prefix="freshness_bench_"))
    workspace = tmp_root / "workspace"
    if cfg.repo_dir:
        src = Path(cfg.repo_dir).expanduser().resolve()
        if not src.is_dir():
            raise SystemExit(f"--repo-dir is not a directory: {src}")
        logger.info("Copying %s -> %s", src, workspace)
        shutil.copytree(src, workspace, ignore=_COPY_IGNORE, symlinks=False, ignore_dangling_symlinks=True)
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if cfg.ref:
            cmd += ["--branch", cfg.ref]
        cmd += [cfg.repo_uri, str(workspace)]
        logger.info("Cloning: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
        shutil.rmtree(workspace / ".git", ignore_errors=True)  # LocalDirectorySource ingests, no git needed

    (workspace / _SENTINEL_NAME).write_text(_sentinel_source(cfg.n_functions))
    return tmp_root, workspace


# --------------------------------------------------------------------------------------
# Scenarios -- each apply() mutates the workspace and returns an undo() closure
# --------------------------------------------------------------------------------------
@dataclass
class Scenario:
    name: str
    description: str
    apply: object  # Callable[[Path, Config], Callable[[], None]]


def _sentinel(workspace: Path) -> Path:
    return workspace / _SENTINEL_NAME


def _rewrite(path: Path, old: str, new: str) -> "callable":
    """Replace the first occurrence of `old` with `new` in a text file; return an undo that
    restores the exact prior bytes. Raises if `old` isn't present (a mutation that silently
    no-ops would quietly corrupt the benchmark)."""
    before = path.read_bytes()
    text = before.decode()
    if old not in text:
        raise RuntimeError(f"scenario setup: {old!r} not found in {path.name}")
    path.write_text(text.replace(old, new, 1))
    return lambda: path.write_bytes(before)


def _append(path: Path, suffix: str) -> "callable":
    before = path.read_bytes()
    path.write_bytes(before + suffix.encode())
    return lambda: path.write_bytes(before)


def _prepend(path: Path, prefix: str) -> "callable":
    before = path.read_bytes()
    path.write_bytes(prefix.encode() + before)
    return lambda: path.write_bytes(before)


def _noop(workspace: Path, cfg: Config):
    return lambda: None


def _comment_eof(workspace: Path, cfg: Config):
    return _append(_sentinel(workspace), "\n# freshness-benchmark trailing comment\n")


def _comment_top(workspace: Path, cfg: Config):
    return _prepend(_sentinel(workspace), "# freshness-benchmark top-of-file comment\n")


def _whitespace(workspace: Path, cfg: Config):
    return _append(_sentinel(workspace), "\n\n\n")


def _edit_body(workspace: Path, cfg: Config):
    return _rewrite(_sentinel(workspace), "def bench_fn_000(value):\n    return value + 0",
                    "def bench_fn_000(value):\n    return value + 424242")


def _edit_signature(workspace: Path, cfg: Config):
    return _rewrite(_sentinel(workspace), "def bench_fn_000(value):", "def bench_fn_000(value, extra=0):")


def _add_function(workspace: Path, cfg: Config):
    return _append(_sentinel(workspace), "\n\ndef bench_fn_added(value):\n    return value * 2\n")


def _delete_function(workspace: Path, cfg: Config):
    last = cfg.n_functions - 1
    block = f"def bench_fn_{last:03d}(value):\n    return value + {last}\n\n"
    return _rewrite(_sentinel(workspace), block, "")


def _add_file(workspace: Path, cfg: Config):
    path = workspace / _ADDED_NAME
    path.write_text(_sentinel_source(5))
    return lambda: path.unlink(missing_ok=True)


def _delete_file(workspace: Path, cfg: Config):
    path = _sentinel(workspace)
    before = path.read_bytes()
    path.unlink()
    return lambda: path.write_bytes(before)


def _rename_file(workspace: Path, cfg: Config):
    src, dst = _sentinel(workspace), workspace / _RENAMED_NAME
    before = src.read_bytes()
    src.rename(dst)

    def undo():
        if dst.exists():
            dst.rename(src)
        else:
            src.write_bytes(before)
    return undo


def _bulk_edit(workspace: Path, cfg: Config):
    reserved = {_SENTINEL_NAME, _ADDED_NAME, _RENAMED_NAME}
    excluded_parts = {".git", "__pycache__", ".venv", "node_modules", "build", "dist"}
    targets: list[Path] = []
    for path in sorted(workspace.rglob("*.py")):
        rel = path.relative_to(workspace)
        if path.name in reserved or excluded_parts & set(rel.parts):
            continue
        targets.append(path)
        if len(targets) >= cfg.bulk_files:
            break
    snapshots = [(p, p.read_bytes()) for p in targets]
    for p, _ in snapshots:
        p.write_bytes(p.read_bytes() + b"\n# freshness-benchmark bulk comment\n")

    def undo():
        for p, data in snapshots:
            p.write_bytes(data)
    return undo


def build_scenarios(cfg: Config) -> list[Scenario]:
    all_scenarios = [
        Scenario("noop", "Re-ingest with zero changes (commit/tree-sha short-circuit)", _noop),
        Scenario("comment_eof", "Add a comment at end-of-file (no entity shifts)", _comment_eof),
        Scenario("comment_top", "Add a comment at top-of-file (renumbers every entity below)", _comment_top),
        Scenario("whitespace", "Append trailing blank lines", _whitespace),
        Scenario("edit_body", "Change one function's body", _edit_body),
        Scenario("edit_signature", "Add a parameter to one function", _edit_signature),
        Scenario("add_function", "Add one new function", _add_function),
        Scenario("delete_function", "Delete one function", _delete_function),
        Scenario("add_file", "Add one new source file", _add_file),
        Scenario("delete_file", "Delete one source file", _delete_file),
        Scenario("rename_file", "Rename one source file", _rename_file),
        Scenario("bulk_edit", f"Touch {cfg.bulk_files} real files (trailing comment each)", _bulk_edit),
    ]
    if cfg.scenarios is None:
        return all_scenarios
    by_name = {s.name: s for s in all_scenarios}
    unknown = [n for n in cfg.scenarios if n not in by_name]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; known: {sorted(by_name)}")
    return [by_name[n] for n in cfg.scenarios]


# --------------------------------------------------------------------------------------
# FalkorDB connection
# --------------------------------------------------------------------------------------
def _server_up(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def connect_writer(cfg: Config, tmp_root: Path) -> tuple[GraphWriter, "callable"]:
    graph_name = f"freshness_{uuid4().hex[:8]}"
    mode = cfg.falkordb
    if mode == "auto":
        mode = "localhost:6379" if _server_up("localhost", 6379) else "embedded"
        logger.info("FalkorDB auto-selected: %s", mode)

    if mode == "embedded":
        conf = FalkorDBConfig(embedded=True, db_path=str(tmp_root / "graph.db"), graph_name=graph_name)
    else:
        host, _, port = mode.partition(":")
        conf = FalkorDBConfig(host=host or "localhost", port=int(port or 6379), graph_name=graph_name)

    graph = connect(conf)
    writer = GraphWriter(graph)

    def cleanup() -> None:
        try:
            graph.delete()
        except Exception:
            pass

    return writer, cleanup


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def _row(m: Metrics) -> dict[str, object]:
    return {
        "total_s": m.total_s, "parse_s": m.parse_s, "resolve_s": m.resolve_s,
        "read_fingerprints_s": m.read_fingerprints_s, "diff_s": m.diff_s, "write_s": m.write_s,
        "other_s": m.other_s, "read_fingerprints_%": m.readback_pct,
        "nodes(+/~/-)": f"{m.nodes_added}/{m.nodes_updated}/{m.nodes_removed}",
        "edges(+/~/-)": f"{m.edges_added}/{m.edges_updated}/{m.edges_removed}",
        "files_parsed": m.files_parsed, "files_cached": m.files_cached,
    }


def print_matrix(cold: Metrics, results: list[Metrics]) -> None:
    cols = [
        ("scenario", 16, "<"), ("total_s", 9, ">"), ("parse", 8, ">"), ("resolve", 8, ">"),
        ("readback", 9, ">"), ("diff", 7, ">"), ("write", 7, ">"), ("rdbk%", 6, ">"),
        ("Δnodes", 8, ">"), ("Δedges", 8, ">"), ("parsed", 7, ">"),
    ]
    header = "  ".join(f"{name:{align}{width}}" for name, width, align in cols)
    print("\n" + "=" * len(header))
    print("FRESHNESS BENCHMARK -- resync cost per scenario (all times seconds)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    def line(m: Metrics) -> None:
        vals = [
            m.scenario, f"{m.total_s:.3f}", f"{m.parse_s:.3f}", f"{m.resolve_s:.3f}",
            f"{m.read_fingerprints_s:.3f}", f"{m.diff_s:.3f}", f"{m.write_s:.3f}", f"{m.readback_pct:.0f}",
            str(m.nodes_added + m.nodes_updated + m.nodes_removed),
            str(m.edges_added + m.edges_updated + m.edges_removed), str(m.files_parsed),
        ]
        print("  ".join(f"{v:{align}{width}}" for v, (_, width, align) in zip(vals, cols)))

    line(cold)
    print("-" * len(header))
    for m in results:
        line(m)
    print("=" * len(header))
    print("readback = read_fingerprints (whole-subgraph read-back to diff against); "
          "rdbk% = readback / total.")
    print("Δnodes/Δedges = nodes/edges actually written or deleted (added+updated+removed).\n")


def write_json(path: str, cfg: Config, cold: Metrics, results: list[Metrics]) -> None:
    payload = {
        "config": asdict(cfg),
        "cold": asdict(cold),
        "scenarios": [asdict(m) for m in results],
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out}")


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main(argv: list[str]) -> None:
    cfg = Config.from_args(argv)
    logging.basicConfig(level=logging.INFO if cfg.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    logger.setLevel(logging.INFO)

    tmp_root, workspace = prepare_workspace(cfg)
    writer, cleanup_db = connect_writer(cfg, tmp_root)
    started = time.monotonic()
    try:
        logger.info("Cold ingest of %s (deep=%s) ...", workspace, cfg.deep)
        cold = _resync(writer, workspace, cfg, "cold", "Full cold ingest (warms the graph + parse caches)")
        print_summary("Cold ingest (baseline)", _row(cold))
        if cold.nodes_added == 0:
            raise SystemExit("cold ingest added no nodes -- is the tree empty / all-excluded? "
                             "Check --repo-dir points at real source.")

        results: list[Metrics] = []
        for scen in build_scenarios(cfg):
            undo = scen.apply(workspace, cfg)
            try:
                m = _resync(writer, workspace, cfg, scen.name, scen.description)
            finally:
                undo()
                _restore(writer, workspace, cfg)  # graph back to baseline for the next scenario
            results.append(m)
            print_summary(f"Scenario: {scen.name} -- {scen.description}", _row(m))

        print_matrix(cold, results)
        if cfg.out:
            write_json(cfg.out, cfg, cold, results)
        logger.info("Done in %.1fs", time.monotonic() - started)
    finally:
        cleanup_db()
        if cfg.keep_workspace:
            print(f"Workspace kept at {workspace}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
