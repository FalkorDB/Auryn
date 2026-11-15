"""Configuration for the C# ingestor."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CSharpSourceConfig:
    path: str
    """Root directory to scan for .cs files (recursively)."""

    project_name: str
    """Logical name for this project, used to build the Project node's id."""

    solution_name: str | None = None
    """Optional: wraps the Project in a Solution node. Omit to make Project the root
    (id "csharp://{project_name}") rather than adding an unnecessary wrapper node."""

    target_framework: str | None = None
    """Free-text, e.g. "net8.0" -- informational only, not parsed from a .csproj."""

    dto_name_suffixes: tuple[str, ...] = ("Dto", "Request", "Response", "ViewModel")
    """Heuristic DTO classification fallback -- see the C# ingestor plan's
    classification rules. A type used as an API endpoint's parameter/return type is
    always classified as DTO regardless of name; this only covers everything else."""

    workers: int | None = None
    """Process pool size for parallel per-file parsing. None means os.cpu_count()."""


def project_id(project_name: str, solution_name: str | None) -> str:
    """The id scheme every Namespace/File/Type/Method/etc. id is ultimately rooted
    under (see resolution.py) -- shared by CSharpIngestor and git_sync.py so this
    correctness-critical prefix is defined in exactly one place."""
    if solution_name:
        return f"csharp://{solution_name}/{project_name}"
    return f"csharp://{project_name}"
