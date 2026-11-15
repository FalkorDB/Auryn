"""Runs the .NET Roslyn extractor (tools/roslyn-extractor) over a C# source tree and
parses its JSON into GraphNode/GraphEdge for the shared delta-sync pipeline.

The extractor does the semantic analysis the tree-sitter engine can't -- DI
registration resolution (DIRegistration/REGISTERS/RESOLVES_TO plus interface->impl
`di-resolved` CALLS), true EXTENDS/IMPLEMENTS, resolved call graphs, and line-range
provenance -- and emits nodes/edges already keyed with the same id scheme this
project's C# graph uses (see ingestors/csharp/{config,resolution}.py), so the output
merges into the same graph as the tree-sitter engine.

Availability is best-effort: if the .NET SDK or the tool isn't present, or the tool
fails, we raise RoslynUnavailable so callers (git_sync) can fall back to tree-sitter
rather than failing the ingest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from auryn.core.graph_model import GraphEdge, GraphNode

logger = logging.getLogger(__name__)

_ENV_OVERRIDE = "AURYN_ROSLYN_EXTRACTOR"
"""Optional path to a published extractor binary (or its .dll). Set this in
production to skip the per-run `dotnet run` build and point at a prebuilt tool."""


class RoslynUnavailable(RuntimeError):
    """The Roslyn extractor could not be run (no .NET SDK, tool not found, or it
    failed to produce output). Signals the caller to fall back to another engine."""


def _find_dev_project() -> Path | None:
    """Locate tools/roslyn-extractor/RoslynExtractor.csproj by walking up from here to the
    repo root -- present only in a source checkout (dev/CI)."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tools" / "roslyn-extractor" / "RoslynExtractor.csproj"
        if candidate.exists():
            return candidate
    return None


def _bundled_project() -> Path | None:
    """The extractor source shipped *inside* this package, so `uv tool install auryn[csharp]`
    carries it -- no repo checkout, no separate download."""
    candidate = Path(__file__).resolve().parent / "roslyn_extractor" / "RoslynExtractor.csproj"
    return candidate if candidate.exists() else None


def _ensure_bundled_built(project: Path) -> Path:
    """Publish the bundled extractor once into a cache under ~/.auryn (keyed by a hash of
    its source, so a package upgrade rebuilds) and return the resulting .dll. Keeps build output
    out of site-packages and avoids rebuilding every run. The first build needs the .NET SDK and
    internet (to restore Microsoft.CodeAnalysis.CSharp)."""
    from auryn.core.home import home_dir

    src = project.parent
    digest = hashlib.sha256(
        b"".join(f.read_bytes() for f in sorted(src.glob("*")) if f.suffix in (".cs", ".csproj"))
    ).hexdigest()[:12]
    out = home_dir() / "roslyn-extractor" / digest
    dll = out / "roslyn-extractor.dll"
    if dll.exists():
        return dll
    out.mkdir(parents=True, exist_ok=True)
    logger.info("Building the bundled Roslyn extractor (one-time) -> %s", out)
    result = subprocess.run(
        ["dotnet", "publish", str(project), "-c", "Release", "-o", str(out), "-v", "quiet", "--nologo"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dll.exists():
        raise RoslynUnavailable(
            f"failed to build the bundled Roslyn extractor: {(result.stderr or result.stdout).strip()[:400]}"
        )
    return dll


def _tool_command() -> list[str]:
    """How to invoke the extractor, in priority order: an explicit prebuilt path
    (AURYN_ROSLYN_EXTRACTOR), the repo's tools/roslyn-extractor (dev checkout), or the copy
    bundled inside this installed package (built once, cached). Raises RoslynUnavailable if the
    .NET SDK is absent or no extractor source can be found -- the caller falls back to tree-sitter."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        path = Path(override)
        return ["dotnet", str(path)] if path.suffix == ".dll" else [str(path)]

    if shutil.which("dotnet") is None:
        raise RoslynUnavailable("the .NET SDK ('dotnet') is not on PATH")

    dev = _find_dev_project()
    if dev is not None:
        # source checkout: `dotnet run` rebuilds in place each invocation (fine for dev/CI).
        return ["dotnet", "run", "--project", str(dev), "-c", "Release", "-v", "quiet", "--nologo", "--"]

    bundled = _bundled_project()
    if bundled is not None:
        return ["dotnet", str(_ensure_bundled_built(bundled))]

    raise RoslynUnavailable("the Roslyn extractor was not found (none bundled, and no tools/roslyn-extractor)")


def extract(
    source_dir: str,
    project_name: str,
    *,
    solution_name: str | None = None,
    commit_sha: str | None = None,
    target_framework: str | None = None,
    dto_suffixes: tuple[str, ...] = (),
) -> tuple[list[GraphNode | GraphEdge], dict]:
    """Run the extractor over `source_dir`; return (graph items, metrics dict).
    Raises RoslynUnavailable if the tool can't be run or produced no valid output."""
    command = _tool_command()
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "extraction.json"
        args = [*command, "--source", source_dir, "--project-name", project_name, "--out", str(out_path)]
        if solution_name:
            args += ["--solution-name", solution_name]
        if commit_sha:
            args += ["--commit-sha", commit_sha]
        if target_framework:
            args += ["--target-framework", target_framework]
        for suffix in dto_suffixes:
            args += ["--dto-suffix", suffix]

        logger.info("Running Roslyn extractor over %s", source_dir)
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as e:
            raise RoslynUnavailable(f"could not launch the Roslyn extractor: {e}") from e
        # Stream the tool's stderr live instead of buffering the whole (long) run, so --verbose
        # per-file progress reaches the user as each file is parsed rather than all at once at the
        # end. The extraction JSON goes to --out, so stdout carries nothing (DEVNULL -> no pipe
        # deadlock). Lines are forwarded to our logger and a short tail kept for error reporting.
        stderr_lines: list[str] = []
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip("\n")
            if line:
                stderr_lines.append(line)
                logger.info("%s", line)
        returncode = proc.wait()
        if returncode != 0:
            raise RoslynUnavailable(
                f"Roslyn extractor exited {returncode}: {chr(10).join(stderr_lines[-5:]).strip()}"
            )
        if not out_path.exists():
            raise RoslynUnavailable("Roslyn extractor produced no output file")
        try:
            data = json.loads(out_path.read_text())
        except json.JSONDecodeError as e:
            raise RoslynUnavailable(f"Roslyn extractor produced invalid JSON: {e}") from e

    items = _to_graph_items(data)
    metrics = data.get("metrics", {})
    logger.info(
        "Roslyn extractor: %d nodes, %d edges (%s)",
        len(data.get("nodes", [])),
        len(data.get("edges", [])),
        metrics,
    )
    return items, metrics


def _to_graph_items(data: dict) -> list[GraphNode | GraphEdge]:
    items: list[GraphNode | GraphEdge] = []
    for node in data.get("nodes", []):
        items.append(GraphNode(node["label"], {"id": node["id"]}, node.get("properties", {})))
    for edge in data.get("edges", []):
        props = dict(edge.get("properties", {}))
        # Cross-ingestor consistency: CALLS carries its call-resolution provenance under the
        # shared `resolution` key (as Python/C/the C# tree-sitter engine do). The Roslyn tool
        # emits it as `provenance` (e.g. "di-resolved") -- remap it here so both C# engines
        # agree. Only CALLS edges are touched; other edges' `provenance` is left as-is.
        if edge["relType"] == "CALLS" and "provenance" in props and "resolution" not in props:
            props["resolution"] = props.pop("provenance")
        items.append(
            GraphEdge(
                edge["relType"],
                edge["fromLabel"],
                {"id": edge["fromId"]},
                edge["toLabel"],
                {"id": edge["toId"]},
                props,
            )
        )
    return items
