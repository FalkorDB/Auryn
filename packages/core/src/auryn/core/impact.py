"""Diff / PR-aware impact -- "what will this break?".

Turns a git diff into a blast-radius report:
  1. parse the diff into changed line ranges per file (`parse_unified_diff`);
  2. map those ranges to graph symbols by line-span overlap (`symbols_in_ranges`);
  3. walk the reverse-dependency closure -- who transitively CALLS/DEPENDS_ON the changed
     symbols -- and enrich it with API endpoints reached, tests covering it, and cross-service
     consumers (`impact_of_diff`).

Reuses the same edge set as graph_queries.impact_of and the graph's existing `line_start`/
`line_end` + cross-repo id-prefix convention; only the diff->symbol mapping and report assembly
are new. The symbol mapping handles the ways ingestors attach a file to a symbol: a `file_path`
on the symbol itself (C/C++/TS), a containing `Module`/`File` via `HAS_*` (Python, PL/SQL), an
outgoing `DEFINED_IN` (the types of Java/Kotlin/C#/Go/Rust/PHP/Ruby/Swift), an incoming `DEFINES`
from the `File` (C/C++/Go/Java/Rust functions and methods), and the members a type in the file
holds (`HAS_METHOD`, ...; Kotlin/PHP/Ruby/Swift/Python/C# methods). A change inside a method
body is that method's change, not its class's (see _innermost).

A diff path is matched against the paths each project stores, which are relative to the folder
the project was indexed from (see _resolve_paths). A diff taken above that folder -- a monorepo,
or a workspace that holds several repos -- has a prefix; it is stripped, and the match is then
scoped to the project indexed from that folder.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from auryn.core.graph_client import semantic_label
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_queries import DEFAULT_IMPACT_EDGE_TYPES

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FULL_SPAN = (0, 10**9)  # a deleted file: treat every symbol it held as changed
_DEFAULT_MAX_DEPTH = 4
_DEFAULT_LIMIT = 500

_CALLABLE_KINDS = frozenset({"Function", "Method", "Procedure", "Constructor", "Routine"})
"""Kinds whose span is a body of code. A change inside one is its own change, never its type's."""

_MEMBER_EDGES = "HAS_METHOD|HAS_FUNCTION|HAS_PROCEDURE|HAS_CLASS|HAS_TYPE|HAS_NESTED_TYPE"
"""Edges from a type to what it declares in its own body (a method, a nested type)."""

_ROUTE_PARAM = re.compile(r"\{[^}]*\}")


# ── diff parsing ───────────────────────────────────────────────────────────────────────
def _strip_ab(path: str) -> str | None:
    """A diff path -> repo-relative path, or None for /dev/null (an add/delete side)."""
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_unified_diff(text: str) -> tuple[dict[str, list[tuple[int, int]]], list[str]]:
    """Parse `git diff` output into ({new_path: [(lo, hi) changed line ranges]}, [deleted_paths]).

    Only the `+`/`-` lines count, never the context lines around them, so a diff taken with any
    `-U` gives the ranges of a `-U0` one. Ranges are in the NEW file: a block of added or
    rewritten lines is its new lines, and a block of removed lines alone is the line just above
    where they were -- git's own anchor for a `-U0` deletion. A deleted file gets a full-span
    range so all its symbols count as changed. A pure-add file contributes no reverse-dependency
    impact (nothing references brand-new code yet), so its ranges are recorded but usually map to
    nothing upstream. A hunk ends when its header's line counts are used up, so a removed SQL
    comment (`--- ...` in the diff) is never read as a file header."""
    changed: dict[str, list[tuple[int, int]]] = {}
    deleted: list[str] = []
    old_path: str | None = None
    new_path: str | None = None
    old_left = new_left = 0  # body lines of each side the current hunk has still to show
    line_no = 0  # the new-file line the next body line is at
    block: list[int] | None = None  # the +/- run being read: [first, last] new line, [] if only `-`

    def close_block() -> None:
        nonlocal block
        if block is not None and new_path is not None:
            lo, hi = block if block else (line_no - 1, line_no - 1)
            changed.setdefault(new_path, []).append((lo, hi))
        block = None

    for line in text.splitlines():
        if old_left or new_left:
            tag, body = line[:1], True
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag in (" ", "") and old_left and new_left:  # context (an editor may strip its space)
                close_block()
                old_left, new_left, line_no = old_left - 1, new_left - 1, line_no + 1
            elif tag == "-" and old_left:
                block = block if block is not None else []
                old_left -= 1
            elif tag == "+" and new_left:
                block = [block[0] if block else line_no, line_no]
                new_left, line_no = new_left - 1, line_no + 1
            else:  # the hunk is shorter than its header says: read this line as a header
                old_left = new_left = 0
                body = False
            if not (old_left or new_left):
                close_block()
            if body:
                continue
        if line.startswith("--- "):
            old_path = _strip_ab(line[4:].strip())
        elif line.startswith("+++ "):
            new_path = _strip_ab(line[4:].strip())
            if new_path is None and old_path is not None:  # +++ /dev/null -> file deleted
                deleted.append(old_path)
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                continue
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            start = int(m.group(2))
            line_no = start if new_left else start + 1  # `+N,0`: N is the line above the removal
    close_block()
    for path in deleted:
        changed.setdefault(path, []).append(_FULL_SPAN)
    return changed, deleted


def git_toplevel(path: str) -> str:
    """The top folder of the git work tree `path` is in. Raises CalledProcessError outside one."""
    return subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.rstrip("\n")  # only git's newline: a folder name may end in a space


def git_diff(repo_dir: str, base: str | None = None, head: str | None = None, *, staged: bool = False,
             relative: bool = False) -> str:
    """Unified diff (`-U0`, exact changed lines) for the requested range: `base..head` if both
    given, `base` vs the working tree if only base, staged changes with `staged=True`, else all
    uncommitted changes vs HEAD. Paths are relative to the top of the repository, wherever in it
    `repo_dir` is. `relative=True` is for a caller that has resolved `repo_dir` to the folder its
    graph was indexed from: paths are then relative to that folder, and the diff is limited to
    it -- a graph indexed from a subfolder stores its paths that way. Raises CalledProcessError
    if git fails."""
    cmd = ["git", "-C", repo_dir, "diff", "-U0", "--no-color", *(["--relative"] if relative else [])]
    if base and head:
        cmd += [base, head]
    elif base:
        cmd += [base]
    elif staged:
        cmd += ["--staged"]
    else:
        cmd += ["HEAD"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


# ── symbol mapping ─────────────────────────────────────────────────────────────────────
class _Project:
    """A project of the graph: its id, its name, and the parts of the folder it was indexed from
    (its `repo_uri`, a local path or a git URL; empty when the graph doesn't say)."""

    def __init__(self, pid: str, name: str | None, repo_uri: str | None):
        self.id, self.name = pid, name or pid.split("://", 1)[-1]
        uri = (repo_uri or "").split("://", 1)[-1].replace("\\", "/").rstrip("/")
        self.folder = _parts(uri[:-4] if uri.endswith(".git") else uri)


def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in path.split("/") if p)


def _projects(graph) -> dict[str, _Project]:
    """The graph's projects by id."""
    rows = graph.ro_query("MATCH (p:Project) RETURN p.id, p.name, p.repo_uri").result_set
    return {pid: _Project(pid, name, uri) for pid, name, uri in rows if pid}


def _project_of(node_id: str, projects=()) -> str:
    """A node's owning project id: the longest of `projects` (the graph's project ids) that is the
    node's id or its prefix up to a `/` or a `.` -- C++ and PHP ids go on with a `.`
    (`cpp://demo.class.svc`) -- else `scheme://name`, the id up to its first `/`. Two symbols with
    different project ids belong to different repos/services."""
    owners = [p for p in projects if node_id == p or node_id.startswith((p + "/", p + "."))]
    if owners:
        return max(owners, key=len)
    if "://" not in node_id:
        return ""
    scheme, rest = node_id.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _resolve_paths(graph, paths, projects: dict[str, _Project], repo: str | None = None
                   ) -> dict[str, tuple[str, set[str]]]:
    """{diff path: (the graph's path for it, ids of the projects it is scoped to)}, for each diff
    path that names a file of the graph.

    A project stores its paths relative to the folder it was indexed from, while a diff names
    them relative to the top of its repository or to wherever it was taken. So a path is tried as
    it is, then with its leading folders stripped one at a time, and the longest that names a
    file wins. As it is, it matches that file in every project that has one. Stripped, it matches
    only in a project indexed from a folder that ends with the stripped folders, or named after
    the last of them: `payments-lib/harbor_payments/charges.py` is `harbor_payments/charges.py`
    of the project indexed from `.../payments-lib`, never of another repo's. `repo`, the folder
    the diff was taken in, picks among several projects that have the file: the one indexed from
    `repo` (joined with the stripped folders) wins alone. A path no project holds is absent."""
    tries = {p: ["/".join(_parts(p)[k:]) for k in range(len(_parts(p)))] for p in paths}
    wanted = sorted({t for ts in tries.values() for t in ts})
    holders: dict[str, set[str]] = {}
    pids = list(projects)
    for file_path, path, nid in graph.ro_query(
        "MATCH (n) WHERE n.file_path IN $c OR n.path IN $c RETURN n.file_path, n.path, n.id", {"c": wanted}
    ).result_set:
        for f in {file_path, path} & set(wanted):
            holders.setdefault(f, set()).add(_project_of(nid, pids))
    here = _parts(_resolved(repo)) if repo else None

    resolved: dict[str, tuple[str, set[str]]] = {}
    for p, ts in tries.items():
        for k, t in enumerate(ts):
            stripped = _parts(p)[:k]
            owners = {
                pid for pid in holders.get(t, ())
                if not k or _indexed_from(projects.get(pid) or _Project(pid, None, None), stripped)
            }
            if owners:
                if here is not None:
                    owners = {pid for pid in owners if pid in projects
                              and projects[pid].folder == here + stripped} or owners
                resolved[p] = (t, owners)
                break
    return resolved


def _indexed_from(project: _Project, stripped: tuple[str, ...]) -> bool:
    return project.folder[-len(stripped):] == stripped or project.name == stripped[-1]


def _resolved(folder: str) -> str:
    return Path(folder).resolve().as_posix()


def _file_symbols(graph, path: str) -> list[dict]:
    """Every symbol with a line span in the file the graph stores as `path`: {id, name, kind,
    lo, hi}. Covers the file<->symbol attachments in the module docstring. A member is taken from
    its type only when it is inside the type's span and has no file of its own: a C++ method
    defined out of line, or a Go method in another file of its package, belongs to that file."""
    ret = (" AND n.line_start > 0 AND n.line_end IS NOT NULL "
           f"RETURN n.id, n.name, {semantic_label('n')}, n.line_start, n.line_end")
    found: dict[str, dict] = {}

    def add(rows) -> None:
        for nid, name, kind, lo, hi in rows:
            if nid is not None:
                found[nid] = {"id": nid, "name": name, "kind": kind, "lo": lo, "hi": hi}

    for q in (
        # (a) file_path directly on the symbol (C/C++/TS)
        "MATCH (n) WHERE n.file_path = $p",
        # (b) symbol under a Module/File container (Python, PL/SQL)
        "MATCH (m)-[:HAS_FUNCTION|HAS_CLASS|HAS_METHOD|HAS_PROCEDURE|HAS_TYPE]->(n) "
        "WHERE (m.file_path = $p OR m.path = $p)",
        # (c) a type -> its File via DEFINED_IN (Java/Kotlin/C#/Go/Rust/PHP/Ruby/Swift)
        "MATCH (n)-[:DEFINED_IN]->(f) WHERE (f.path = $p OR f.file_path = $p)",
        # (d) File -DEFINES-> its functions, methods and types (C/C++/Go/Java/Rust)
        "MATCH (f)-[:DEFINES]->(n) WHERE (f.path = $p OR f.file_path = $p)",
    ):
        add(graph.ro_query(q + ret, {"p": path}).result_set)
    # (e) the methods and nested types a type above declares in its own body
    add(graph.ro_query(
        f"MATCH (t)-[:{_MEMBER_EDGES}*1..3]->(n) WHERE t.id IN $ids "
        "AND n.line_start >= t.line_start AND n.line_end <= t.line_end AND n.file_path IS NULL "
        "AND NOT ()-[:DEFINES]->(n) AND NOT (n)-[:DEFINED_IN]->()" + ret,
        {"ids": list(found)},
    ).result_set)
    return list(found.values())


def _innermost(syms: list[dict], ranges) -> list[dict]:
    """`syms` without each one that is not a callable and whose changed lines all lie inside
    callables it holds. A class's span holds its methods' spans, so a change inside a method body
    overlaps both -- but it is the method's change: whoever uses the class at all (a constructor
    that takes it, a field of its type) is not its blast radius. A type stays when a changed line
    is outside its callables (a field, its signature)."""
    bodies = sorted((s["lo"], s["hi"]) for s in syms if s["kind"] in _CALLABLE_KINDS)
    keep = []
    for s in syms:
        inner = [(lo, hi) for lo, hi in bodies if s["lo"] <= lo and hi <= s["hi"]]
        if s["kind"] in _CALLABLE_KINDS or any(
            _line_outside(max(lo, s["lo"]), min(hi, s["hi"]), inner) for lo, hi in ranges
            if lo <= s["hi"] and hi >= s["lo"]
        ):
            keep.append(s)
    return keep


def _line_outside(lo: int, hi: int, spans: list[tuple[int, int]]) -> bool:
    """Whether a line of lo..hi is in none of `spans` (sorted by start)."""
    for a, b in spans:
        if a > lo:
            return True
        lo = max(lo, b + 1)
        if lo > hi:
            return False
    return lo <= hi


def _map_diff(graph, changed: dict[str, list[tuple[int, int]]], projects: dict[str, _Project],
              repo: str | None) -> tuple[list[dict], set[str], dict[str, list[str]]]:
    """(symbols, mapped_paths, ambiguous): see symbols_in_ranges. `ambiguous` is {diff path: the
    projects it mapped symbols in} for each path that mapped in more than one."""
    pids = list(projects)
    found: dict[str, dict] = {}
    mapped_paths: set[str] = set()
    ambiguous: dict[str, list[str]] = {}
    for path, (graph_path, owners) in _resolve_paths(graph, changed, projects, repo).items():
        by_project: dict[str, list[dict]] = {}
        for s in sorted(_file_symbols(graph, graph_path), key=lambda s: (s["lo"], s["id"])):
            if any(s["lo"] <= hi and s["hi"] >= lo for lo, hi in changed[path]):
                owner = _project_of(s["id"], pids)
                if owner in owners or not pids:  # no Project nodes: nothing to scope by
                    by_project.setdefault(owner, []).append(s)
        for syms in by_project.values():
            for s in _innermost(syms, changed[path]):
                found[s["id"]] = {"id": s["id"], "name": s["name"], "kind": s["kind"]}
                mapped_paths.add(path)
        if len(by_project) > 1:
            ambiguous[path] = sorted(by_project)
    return list(found.values()), mapped_paths, ambiguous


def symbols_in_ranges(graph, changed: dict[str, list[tuple[int, int]]], *,
                      repo: str | None = None) -> tuple[list[dict], set[str]]:
    """(symbols, mapped_paths): graph symbols whose source span overlaps any changed line range
    (deduped), and the set of diff paths that produced at least one symbol. Covers the
    file<->symbol attachments used across ingestors (see module docstring), keeps only the
    innermost symbol a change is in (see _innermost), and matches each diff path to a project's
    file (see _resolve_paths; `repo` is the folder the diff was taken in, if known). A path that
    yields nothing (config/docs, or an unindexed file) is absent from `mapped_paths`."""
    symbols, mapped_paths, _ambiguous = _map_diff(graph, changed, _projects(graph), repo)
    return symbols, mapped_paths


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
    repo: str | None = None,
) -> dict:
    """Blast-radius report for a parsed diff. `changed` is {path: [(lo, hi)]} (see
    parse_unified_diff); `repo` is the folder the diff was taken in, when the caller ran git
    itself (see _resolve_paths). Returns the changed symbols, the reverse-dependency closure (with
    direct-vs-transitive split), API endpoints reached, tests covering it, cross-service
    consumers, and human-readable flags. Pure graph reads."""
    projects = _projects(graph)
    pids = list(projects)
    changed_syms, mapped_paths, ambiguous = _map_diff(graph, changed, projects, repo)
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

    base_projects = {_project_of(i, pids) for i in changed_ids}
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

    endpoints = _endpoint_names(graph.ro_query(
        "MATCH (m)-[:EXPOSES_ENDPOINT]->(e) WHERE m.id IN $ids RETURN DISTINCT e.id, e.name",
        {"ids": affected_ids + changed_ids},
    ).result_set)
    tests = sorted({a["name"] for a in affected if _looks_like_test(a["name"], a["id"])})
    cross = sorted({_project_of(i, pids) for i in affected_ids} - base_projects - {""})

    flags: list[str] = []
    if cross:
        flags.append(f"cross-service: change reaches {len(cross)} other service(s): {', '.join(cross)}")
    if endpoints:
        flags.append(f"reaches {len(endpoints)} API endpoint(s)")
    if affected_ids and not tests:
        flags.append("no test coverage found on the blast radius")
    if truncated:
        flags.append(f"blast radius exceeded {limit} nodes -- report truncated")
    if ambiguous:
        flags.append(
            f"{len(ambiguous)} changed file(s) exist in more than one project of this graph and were "
            f"mapped in each: {', '.join(sorted(ambiguous))} -- put the repo's folder in front of a "
            "path to pick its project")

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


def _endpoint_names(rows) -> list[str]:
    """The names of the endpoints in `rows` ((id, name) pairs), one per route. A handler exposes
    the route it declares (`POST /api/orders/{order_id}/cancel`) and, once the HTTP stitch has
    matched a call to it, the call site's endpoint too (`extern://http/...`, every parameter named
    `{param}`); both are one route, reported by its declared name."""
    names: dict[str, str] = {}
    for eid, name in sorted(rows, key=lambda r: (str(r[0]).startswith("extern://"), str(r[1]))):
        if name:
            names.setdefault(_route_key(name), name)
    return sorted(names.values())


def _route_key(name: str) -> str:
    """An endpoint name with its path parameters blanked, its path lowercased and its empty
    segments dropped -- the way the HTTP stitch matches a call to a route."""
    verb, _, route = name.partition(" ")
    return f"{verb.upper()} /" + "/".join(_parts(_ROUTE_PARAM.sub("{}", route).lower()))


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
