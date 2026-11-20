"""Deep Rust call resolution via rust-analyzer's SCIP index (the `deep_sync` engine, mirroring C#'s
Roslyn and Python's jedi deep passes).

The fast tree-sitter pass resolves CALLS *by name*, so a method on a `dyn`/`impl`/generic receiver, an
untyped local, or a cross-module/crate callee is dropped or guessed (see `unresolved_calls`, #792/#794).
rust-analyzer resolves them precisely. We ask it for a SCIP index (`rust-analyzer scip <root>`), then
derive CALLS by occurrence containment: a *reference* occurrence of a callable symbol that falls inside
a callable's line span is a call from that callable to the referenced one. Node ids come from the fast
pass (`resolution.callable_locations`) -- ids embed only syntactic signatures, so a deep edge lands on
the exact Function/Method node the fast pass authored.

Graceful degradation is the contract (as with Roslyn's `RoslynUnavailable`): if rust-analyzer isn't
available or the crate can't be analyzed, `RustAnalyzerUnavailable` is raised and git_sync falls back to
the tree-sitter fast pass. v1 resolves INTRA-project calls only and leaves the fast pass's `extern://`
placeholders untouched (so stdlib/other-crate edges and cross-repo reconciliation are preserved)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict

from auryn.core.graph_model import GraphEdge
from auryn.ingestors.rust import scip
from auryn.ingestors.rust.resolution import CallableLocation

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_RUST_ANALYZER_TIMEOUT", "900"))


class RustAnalyzerUnavailable(RuntimeError):
    """rust-analyzer could not be located or failed to produce a SCIP index -- the signal for git_sync
    to fall back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate a usable rust-analyzer, in priority order (mirrors roslyn._tool_command):
      1. `AURYN_RUST_ANALYZER` env override (an explicit binary path);
      2. `rustup which rust-analyzer` -- resolves the real toolchain binary only when the component is
         actually installed (the `~/.cargo/bin/rust-analyzer` proxy exists even when it isn't);
      3. a plain `rust-analyzer` on PATH (Homebrew, a manual install, or a working rustup proxy).
    Raises RustAnalyzerUnavailable (with the one-line `rustup component add` hint) if none resolve. The
    chosen command is still validated by use -- a proxy whose component is missing fails at scip time
    and is turned into RustAnalyzerUnavailable there."""
    override = os.environ.get("AURYN_RUST_ANALYZER")
    if override:
        return [override]
    if shutil.which("rustup"):
        try:
            got = subprocess.run(["rustup", "which", "rust-analyzer"], capture_output=True, text=True,
                                 timeout=30, check=False)
            path = got.stdout.strip()
            if got.returncode == 0 and path and os.path.exists(path):
                return [path]
        except (OSError, subprocess.SubprocessError):
            pass
    found = shutil.which("rust-analyzer")
    if found:
        return [found]
    raise RustAnalyzerUnavailable(
        "rust-analyzer not found. Install it with `rustup component add rust-analyzer`, "
        "or set AURYN_RUST_ANALYZER to its path."
    )


def run_scip(root: str) -> bytes:
    """Run `rust-analyzer scip <root>` and return the raw `index.scip` bytes. Raises
    RustAnalyzerUnavailable on any failure (tool missing, non-zero exit, unbuildable crate, no output)
    so the caller can fall back to the fast pass. `root` must be a cargo project directory on disk."""
    cmd = _tool_command()
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "scip", root, "--output", out_path]
        try:
            proc = subprocess.run(
                argv, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=_SCIP_TIMEOUT_SECONDS, check=False,
            )
        except FileNotFoundError as exc:
            raise RustAnalyzerUnavailable(f"could not execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RustAnalyzerUnavailable(f"rust-analyzer scip timed out after {_SCIP_TIMEOUT_SECONDS}s") from exc
        # Older rust-analyzer ignores --output and writes index.scip into CWD; accept either.
        produced = out_path if os.path.exists(out_path) else os.path.join(root, "index.scip")
        if proc.returncode != 0 or not os.path.exists(produced):
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise RustAnalyzerUnavailable(f"rust-analyzer scip exited {proc.returncode}: {tail}")
        with open(produced, "rb") as fh:
            data = fh.read()
        if produced != out_path:  # clean up the CWD artifact from the fallback path
            try:
                os.remove(produced)
            except OSError:
                pass
    return data


# -- pure SCIP -> CALLS derivation (unit-testable without the toolchain) ----------------------------

def _is_callable_symbol(symbol: str) -> bool:
    """A SCIP symbol that names a function/method (its descriptor ends `().`). Excludes params
    (`...().(x)`), types (`...#`), locals (`local N`), and macros."""
    return symbol.endswith("().")


def _simple_name(symbol: str) -> str:
    """The callee's bare name from a SCIP symbol: `rust-analyzer cargo crate 1.0 Fast#Run#run().` -> `run`."""
    descriptor = symbol.split(" ", 4)[-1]
    descriptor = descriptor.removesuffix("().")
    return re.split(r"[#/]", descriptor)[-1] if descriptor else ""


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def resolve_calls(scip_bytes: bytes, locations: list[CallableLocation]) -> tuple[list[GraphEdge], dict]:
    """Derive precise intra-project CALLS from a SCIP index + the fast pass's callable locations.

    Returns (resolved `CALLS` edges with `resolution:"resolved"`, metrics). A reference occurrence of a
    callable symbol is attributed to the INNERMOST callable whose line span contains it, and resolved to
    the in-source callable the symbol defines. References whose symbol isn't defined in this project
    (stdlib / other-crate) are counted as `external` and left to the fast pass's placeholder edges."""
    documents = scip.parse_index(scip_bytes)
    by_path: dict[str, list[CallableLocation]] = defaultdict(list)
    for loc in locations:
        by_path[_norm_path(loc.rel_path)].append(loc)

    def innermost(locs: list[CallableLocation], line_1based: int, name: str | None = None) -> CallableLocation | None:
        """The smallest-span callable whose [line_start, line_end] contains the line (optionally also
        matching `name`) -- so a call site / definition attributes to its enclosing or own callable."""
        hits = [loc for loc in locs if loc.line_start <= line_1based <= loc.line_end and (name is None or loc.name == name)]
        return min(hits, key=lambda loc: loc.line_end - loc.line_start) if hits else None

    # symbol -> the in-source callable it DEFINES (joined by file + containment + name match).
    symbol_to_loc: dict[str, CallableLocation] = {}
    for doc in documents:
        locs = by_path.get(_norm_path(doc.relative_path), [])
        for occ in doc.occurrences:
            if not occ.is_definition or not _is_callable_symbol(occ.symbol):
                continue
            match = innermost(locs, occ.start_line + 1, _simple_name(occ.symbol))
            if match is not None:
                symbol_to_loc[occ.symbol] = match

    edges: dict[tuple[str, str], GraphEdge] = {}
    metrics = {"resolved": 0, "external": 0, "unattributed": 0}
    for doc in documents:
        locs = by_path.get(_norm_path(doc.relative_path), [])
        for occ in doc.occurrences:
            if occ.is_definition or not _is_callable_symbol(occ.symbol):
                continue
            caller = innermost(locs, occ.start_line + 1)
            if caller is None:
                metrics["unattributed"] += 1
                continue
            target = symbol_to_loc.get(occ.symbol)
            if target is None:
                metrics["external"] += 1  # stdlib / other-crate -- fast pass's extern:// edge covers it
                continue
            if target.id == caller.id:
                continue  # self-reference / recursion (the fast pass skips self-CALLS too)
            key = (caller.id, target.id)
            if key not in edges:
                edges[key] = GraphEdge(
                    "CALLS", caller.label, {"id": caller.id}, target.label, {"id": target.id},
                    {"resolution": "resolved"},
                )
                metrics["resolved"] += 1
    return list(edges.values()), metrics
