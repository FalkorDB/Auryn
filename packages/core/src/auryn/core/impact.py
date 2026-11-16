"""Diff / PR-aware impact -- "what will this break?".

Turns a git diff into a blast-radius report:
  1. parse the diff into changed line ranges per file (`parse_unified_diff`);
  2. map those ranges to graph symbols by line-span overlap (`symbols_in_ranges`);
  3. walk the reverse-dependency closure -- who transitively CALLS/DEPENDS_ON the changed
     symbols -- and enrich it with API endpoints reached, tests covering it, and cross-service
     consumers (`impact_of_diff`).

Reuses the same edge set as graph_queries.impact_of and the graph's existing `line_start`/
`line_end` + cross-repo id-prefix convention; only the diff->symbol mapping and report assembly
are new. The symbol mapping handles the three ways ingestors attach a file to a symbol: a
`file_path` on the symbol itself (C/C++/TS), a containing `Module`/`File` via `HAS_*` (Python,
PL/SQL), or an incoming `DEFINED_IN` (Go/Java/Rust/PHP).
"""

from __future__ import annotations

import re
import subprocess

from auryn.core.graph_client import semantic_label
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_queries import DEFAULT_IMPACT_EDGE_TYPES

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_FULL_SPAN = (0, 10**9)  # a deleted file: treat every symbol it held as changed
_DEFAULT_MAX_DEPTH = 4
_DEFAULT_LIMIT = 500


# ── diff parsing ───────────────────────────────────────────────────────────────────────
def _strip_ab(path: str) -> str | None:
    """A diff path -> repo-relative path, or None for /dev/null (an add/delete side)."""
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_unified_diff(text: str) -> tuple[dict[str, list[tuple[int, int]]], list[str]]:
    """Parse `git diff` output (ideally `-U0`) into ({new_path: [(lo, hi) changed line ranges]},
    [deleted_paths]). Ranges are in the NEW file; a deleted file gets a full-span range so all its
    symbols count as changed. A pure-add file contributes no reverse-dependency impact (nothing
    references brand-new code yet), so its ranges are recorded but usually map to nothing upstream."""
    changed: dict[str, list[tuple[int, int]]] = {}
    deleted: list[str] = []
    old_path: str | None = None
    new_path: str | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            old_path = _strip_ab(line[4:].strip())
        elif line.startswith("+++ "):
            new_path = _strip_ab(line[4:].strip())
            if new_path is None and old_path is not None:  # +++ /dev/null -> file deleted
                deleted.append(old_path)
        elif line.startswith("@@") and new_path is not None:
            m = _HUNK.match(line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            hi = start if count == 0 else start + count - 1  # count 0 = deletion at `start`
            changed.setdefault(new_path, []).append((start, hi))
    for path in deleted:
        changed.setdefault(path, []).append(_FULL_SPAN)
    return changed, deleted


def git_diff(repo_dir: str, base: str | None = None, head: str | None = None, *, staged: bool = False) -> str:
    """Unified diff (`-U0`, exact changed lines) for the requested range: `base..head` if both
    given, `base` vs the working tree if only base, staged changes with `staged=True`, else all
    uncommitted changes vs HEAD. Raises CalledProcessError if git fails."""
    cmd = ["git", "-C", repo_dir, "diff", "-U0", "--no-color"]
    # `base`/`head` come from the caller (the impact_of_diff MCP tool, --base/--head). End option
    # parsing before them so a value starting with `-` fails as a bad revision instead of running
    # as a git option (`--output=<file>` would overwrite that file).
    if base and head:
        cmd += ["--end-of-options", base, head]
    elif base:
        cmd += ["--end-of-options", base]
    elif staged:
        cmd += ["--staged"]
    else:
        cmd += ["HEAD"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


# ── symbol mapping ─────────────────────────────────────────────────────────────────────
def _project_of(node_id: str) -> str:
    """A node's owning project id -- `scheme://name`, the stable prefix every one of a project's
    ids carries. Two symbols with different project ids belong to different repos/services."""
    if "://" not in node_id:
        return ""
    scheme, rest = node_id.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}"


def symbols_in_ranges(graph, changed: dict[str, list[tuple[int, int]]]) -> tuple[list[dict], set[str]]:
    """(symbols, mapped_paths): graph symbols whose source span overlaps any changed line range
    (deduped), and the set of diff paths that produced at least one symbol. Covers the three
    file<->symbol attachments used across ingestors (see module docstring); a path that yields
    nothing (config/docs, or an unindexed file) is absent from `mapped_paths`."""
    found: dict[str, dict] = {}
    mapped_paths: set[str] = set()
    for path, ranges in changed.items():
        for lo, hi in ranges:
            params = {"p": path, "lo": lo, "hi": hi}
            for q in (
                # (a) file_path directly on the symbol (C/C++/TS)
                "MATCH (n) WHERE n.file_path = $p AND n.line_start <= $hi AND n.line_end >= $lo "
                "AND n.line_start > 0 RETURN n.id, n.name, " + semantic_label("n"),
                # (b) symbol under a Module/File container (Python, PL/SQL)
                "MATCH (m)-[:HAS_FUNCTION|HAS_CLASS|HAS_METHOD|HAS_PROCEDURE|HAS_TYPE]->(n) "
                "WHERE (m.file_path = $p OR m.path = $p) AND n.line_start <= $hi AND n.line_end >= $lo "
                "RETURN n.id, n.name, " + semantic_label("n"),
                # (c) symbol -> File via DEFINED_IN (Go/Java/Rust/PHP)
                "MATCH (n)-[:DEFINED_IN]->(f) WHERE (f.path = $p OR f.file_path = $p) "
                "AND n.line_start <= $hi AND n.line_end >= $lo RETURN n.id, n.name, " + semantic_label("n"),
            ):
                for nid, name, kind in graph.ro_query(q, params).result_set:
                    if nid is not None:
                        found[nid] = {"id": nid, "name": name, "kind": kind}
                        mapped_paths.add(path)
    return list(found.values()), mapped_paths


# ── impact ─────────────────────────────────────────────────────────────────────────────
def _looks_like_test(name: str, node_id: str) -> bool:
    n = (name or "").lower()
    return n.startswith("test") or n.endswith("_test") or "/test" in node_id.lower()


def impact_of_diff(
    graph,
    changed: dict[str, list[tuple[int, int]]],
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    limit: int = _DEFAULT_LIMIT,
) -> dict:
    """Blast-radius report for a parsed diff. `changed` is {path: [(lo, hi)]} (see
    parse_unified_diff). Returns the changed symbols, the reverse-dependency closure (with
    direct-vs-transitive split), API endpoints reached, tests covering it, cross-service
    consumers, and human-readable flags. Pure graph reads."""
    changed_syms, mapped_paths = symbols_in_ranges(graph, changed)
    changed_ids = [s["id"] for s in changed_syms]
    unmapped = sorted(p for p in changed if p not in mapped_paths)

    if not changed_ids:
        return {
            "changed_symbols": [],
            "unmapped_files": unmapped,
            "impact": _empty_impact(),
            "flags": ["no graph symbols overlap the changed lines (config/docs-only change, or graph not indexed for these files)"],
            "truncated": False,
        }

    base_projects = {_project_of(i) for i in changed_ids}
    pattern = "|".join(DEFAULT_IMPACT_EDGE_TYPES)  # edge types are our own constants -> safe to interpolate

    all_rows = graph.ro_query(
        f"MATCH (dep)-[:{pattern}*1..{max_depth}]->(c) WHERE c.id IN $ids AND NOT dep.id IN $ids "
        "RETURN DISTINCT dep.id, dep.name, " + semantic_label("dep") + " LIMIT $lim",
        {"ids": changed_ids, "lim": limit + 1},
    ).result_set
    truncated = len(all_rows) > limit
    all_rows = all_rows[:limit]
    direct_ids = {
        r[0]
        for r in graph.ro_query(
            f"MATCH (dep)-[:{pattern}]->(c) WHERE c.id IN $ids AND NOT dep.id IN $ids RETURN DISTINCT dep.id",
            {"ids": changed_ids},
        ).result_set
    }

    affected = [
        {"id": nid, "name": name, "kind": kind, "direct": nid in direct_ids}
        for nid, name, kind in all_rows
    ]
    affected_ids = [a["id"] for a in affected]

    endpoints = sorted(
        {
            r[0]
            for r in graph.ro_query(
                "MATCH (m)-[:EXPOSES_ENDPOINT]->(e) WHERE m.id IN $ids RETURN DISTINCT e.name",
                {"ids": affected_ids + changed_ids},
            ).result_set
            if r[0]
        }
    )
    tests = sorted({a["name"] for a in affected if _looks_like_test(a["name"], a["id"])})
    cross = sorted({_project_of(i) for i in affected_ids} - base_projects - {""})

    flags: list[str] = []
    if cross:
        flags.append(f"cross-service: change reaches {len(cross)} other service(s): {', '.join(cross)}")
    if endpoints:
        flags.append(f"reaches {len(endpoints)} API endpoint(s)")
    if affected_ids and not tests:
        flags.append("no test coverage found on the blast radius")
    if truncated:
        flags.append(f"blast radius exceeded {limit} nodes -- report truncated")

    return {
        "changed_symbols": changed_syms,
        "unmapped_files": unmapped,
        "impact": {
            "total_affected": len(affected),
            "direct_callers": sorted(a["name"] for a in affected if a["direct"]),
            "transitive": sorted(a["name"] for a in affected if not a["direct"]),
            "api_endpoints_reached": endpoints,
            "tests_covering": tests,
            "cross_service_consumers": cross,
            "affected": affected,  # full list with ids -> pivot via who_calls/get_context
        },
        "flags": flags,
        "truncated": truncated,
    }


def _empty_impact() -> dict:
    return {
        "total_affected": 0,
        "direct_callers": [],
        "transitive": [],
        "api_endpoints_reached": [],
        "tests_covering": [],
        "cross_service_consumers": [],
        "affected": [],
    }
