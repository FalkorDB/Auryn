"""Repository provenance ingestion: local Git history plus public GitHub metadata.

This graph layer has its own labels and stable repository-scoped IDs, so ordinary
code delta syncs never delete provenance nodes. GitHub reads use the authenticated local `gh` CLI
when available, otherwise the public REST API, and are gated by a public visibility check.
"""

from __future__ import annotations

import json
import functools
import hashlib
import http.client
import logging
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import git
from redis.exceptions import ResponseError

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode, NodeSchema

logger = logging.getLogger(__name__)

NODE_SCHEMAS = [
    NodeSchema("GitRepository", indexed_properties=("canonical_url",)),
    NodeSchema("GitCheckout", indexed_properties=("source_path",)),
    NodeSchema("GitCommit", indexed_properties=("sha", "authored_at")),
    NodeSchema("GitUser", indexed_properties=("identity",)),
    NodeSchema("GitTag", indexed_properties=("name",)),
    NodeSchema("GitBranch", indexed_properties=("name",)),
    NodeSchema("GitIssue", indexed_properties=("number", "state")),
    NodeSchema("GitPullRequest", indexed_properties=("number", "state")),
    NodeSchema("GitRelease", indexed_properties=("tag_name",)),
    NodeSchema("GitHubApp", indexed_properties=("app_id", "slug")),
    NodeSchema("GitCheckSuite", indexed_properties=("suite_id", "head_sha", "status", "conclusion")),
    NodeSchema("GitCheckRun", indexed_properties=("run_id", "name", "status", "conclusion")),
]
GIT_INFO_LABELS = tuple(schema.label for schema in NODE_SCHEMAS)


def purge_git_info(writer: GraphWriter) -> int:
    """Remove the Git-information subgraph from this graph, preserving code and other data."""
    roots = writer.ro_query("MATCH (n:GitRepository) RETURN count(n)").result_set
    if not roots or not roots[0][0]:
        return 0
    removed = 0
    for label in GIT_INFO_LABELS:
        try:
            rows = writer.ro_query(f"MATCH (n:{label}) RETURN count(n)").result_set
            removed += int(rows[0][0]) if rows else 0
            writer.query(f"MATCH (n:{label}) DETACH DELETE n")
        except Exception:
            logger.exception("Failed removing :%s Git information nodes", label)
            raise
    return removed


def _repo_id_for_source(writer: GraphWriter, source_path: str) -> str | None:
    rows = writer.ro_query(
        "MATCH (r:GitRepository {source_path: $path}) RETURN r.id", {"path": source_path}
    ).result_set
    return str(rows[0][0]) if rows else None


def _delete_repo_subgraph(writer: GraphWriter, repo_id: str) -> None:
    prefix = repo_id + "/"
    upper = prefix + chr(0xFFFF)
    for label in GIT_INFO_LABELS:
        if label == "GitRepository":
            continue
        writer.query(
            f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi DETACH DELETE n",
            {"lo": prefix, "hi": upper},
        )
    writer.query("MATCH (n:GitRepository {id: $id}) DETACH DELETE n", {"id": repo_id})


def _checkout_id(source_path: str) -> str:
    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    return f"git-checkout://{digest}"


def _unlink_checkout(writer: GraphWriter, source_path: str) -> None:
    checkout_id = _checkout_id(source_path)
    try:
        rows = writer.ro_query(
            "MATCH (:GitCheckout {id: $id})-[:USES_REPOSITORY]->(r:GitRepository) RETURN r.id",
            {"id": checkout_id},
        ).result_set
    except ResponseError as exc:
        if "Invalid graph operation on empty key" in str(exc):
            return
        raise
    for (old_repo_id,) in rows:
        writer.query(
            "MATCH (c:GitCheckout {id: $checkout})-[e:USES_REPOSITORY]->(r:GitRepository {id: $repo}) DELETE e",
            {"checkout": checkout_id, "repo": old_repo_id},
        )
        remaining = writer.ro_query(
            "MATCH (:GitCheckout)-[:USES_REPOSITORY]->(r:GitRepository {id: $id}) RETURN count(r)",
            {"id": old_repo_id},
        ).result_set
        if not remaining or not remaining[0][0]:
            _delete_repo_subgraph(writer, str(old_repo_id))
    writer.query("MATCH (c:GitCheckout {id: $id}) DETACH DELETE c", {"id": checkout_id})


def remove_git_info_for_source(writer: GraphWriter, source: str | Path) -> None:
    """Remove provenance associated with a checkout that is no longer a Git repository."""
    candidate = Path(source).expanduser()
    source_path = str(candidate.resolve()) if candidate.exists() else str(source)
    _unlink_checkout(writer, source_path)

def _canonical_repo(repo: git.Repo, source_uri: str) -> tuple[str, tuple[str, str] | None]:
    remote = source_uri
    try:
        remotes = list(repo.remotes)
        if remotes:
            origin = next((r for r in remotes if r.name == "origin"), remotes[0])
            remote = origin.url
    except (AttributeError, IndexError):
        pass
    remote = remote.strip()
    match = re.search(r"(?:github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?/?$", remote, re.I)
    github = (match.group(1), match.group(2)) if match else None
    if github:
        canonical = f"https://github.com/{github[0]}/{github[1]}"
    else:
        canonical = remote.rstrip("/") or str(Path(source_uri).resolve())
    return canonical, github


def _node(label: str, node_id: str, **props) -> GraphNode:
    return GraphNode(label, {"id": node_id}, {"id": node_id, **props})


def _edge(rel: str, a_label: str, a_id: str, b_label: str, b_id: str, **props) -> GraphEdge:
    return GraphEdge(rel, a_label, {"id": a_id}, b_label, {"id": b_id}, props)


def _commit_file_changes(commit: git.Commit) -> list[dict]:
    """Return file-level changes against the first parent (or the empty tree for a root commit)."""
    try:
        if commit.parents:
            parent = commit.parents[0]
            diffs = parent.diff(commit, find_renames=False)
        else:
            diffs = commit.diff(git.NULL_TREE)
    except git.GitCommandError as exc:
        # Shallow clones may not contain a commit's parent, so Git cannot construct its diff.
        logger.warning("Cannot collect changed files for commit %s: %s", commit.hexsha, exc)
        return []
    changes: list[dict] = []
    for diff in diffs:
        change_type = diff.change_type or ("A" if diff.new_file else "D" if diff.deleted_file else "M")
        path = (diff.b_path or diff.a_path or "")
        changes.append({
            "path": path,
            "change_type": change_type,
        })
    return changes


def _batch_commit_file_changes(repo: git.Repo, commits: list[git.Commit]) -> dict[str, list[dict]]:
    """Read per-commit A/M/D statuses with a small number of `git log` processes.

    Starting a `git diff-tree` process for every commit dominated the first FalkorDB benchmark.
    `git log --name-status -z` reports the same first-parent status data for a batch in one
    process, without reading patches or line counts. The record separator keeps arbitrary file
    names (including newlines) unambiguous.
    """
    changes_by_sha: dict[str, list[dict]] = {commit.hexsha: [] for commit in commits}
    chunk_size = 128  # keep argv small even for large initial histories
    for start in range(0, len(commits), chunk_size):
        batch = commits[start:start + chunk_size]
        output = subprocess.run(
            ["git", "-C", repo.working_tree_dir or repo.git_dir, "log", "--no-walk=unsorted",
             "--root", "--diff-merges=first-parent", "--no-renames",
             "--format=%x1e%H%x00%P", "--name-status", "-z",
             *(commit.hexsha for commit in batch)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        for record in output.split(b"\x1e"):
            if not record:
                continue
            header, separator, file_data = record.partition(b"\n")
            if not separator:
                continue
            sha = header.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
            if not sha:
                continue
            fields = file_data.split(b"\x00")
            changes: list[dict] = []
            for index in range(0, len(fields) - 1, 2):
                status = fields[index].decode("ascii", "ignore")
                raw_path = fields[index + 1]
                if not raw_path:
                    continue
                change_type = status if status in {"A", "D"} else "M"
                changes.append({"path": raw_path.decode("utf-8", "surrogateescape"),
                                "change_type": change_type})
            changes_by_sha[sha] = changes
    return changes_by_sha


def _code_file_index(writer: GraphWriter, source_uri: str, canonical: str) -> dict[str, list[str]]:
    """Load source File IDs by path once, scoped by Project provenance where available."""
    rows = writer.ro_query(
        "MATCH (p:Project)-[:HAS_FILE]->(f:File) "
        "WHERE p.repo_uri IN $origins RETURN DISTINCT f.path, f.id",
        {"origins": [source_uri, canonical]},
    ).result_set
    if not rows:
        rows = writer.ro_query("MATCH (f:File) RETURN DISTINCT f.path, f.id").result_set
    paths: dict[str, list[str]] = {}
    for path, file_id in rows:
        if path and file_id:
            paths.setdefault(str(path), []).append(str(file_id))
    return paths


@functools.lru_cache(maxsize=1)
def _github_token() -> str | None:
    """The local GitHub CLI's token, fetched ONCE and reused for every REST/GraphQL call this process --
    so we never spawn `gh` per request, and we get the authenticated 5000/hr rate limit (vs the anonymous
    60/hr). None when `gh` is absent or not logged in; calls then fall back to the anonymous public API."""
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(["gh", "auth", "token"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=10, check=False)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _auth_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Auryn-codegraph",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Transport-level failures GitHub returns under load on large fetches (e.g. check runs for thousands
# of commits): a dropped connection (RemoteDisconnected), a reset, or a read timeout. These surface
# NOT as an HTTPError (which carries a status code) but as a bare transport exception, so the status
# retry in `_graphql` never caught them. TimeoutError covers socket.timeout (its alias since 3.10).
_TRANSPORT_ERRORS = (http.client.RemoteDisconnected, ConnectionError, TimeoutError)


def _urlopen_retrying(request: urllib.request.Request, timeout: int, retries: int = 3):
    """`urlopen` + read with retry on *transient* failures only -- HTTP 429/5xx and transport-level
    drops (connection reset / RemoteDisconnected / timeout). Returns (body_bytes, headers). An HTTPError
    on a non-retriable status, or exhausted retries, re-raises so the caller's own error handling runs."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as exc:  # has a status code; HTTPError IS-A URLError, so catch first
            last_exc = exc
            if exc.code not in _RETRY_STATUS or attempt == retries - 1:
                raise
        except (urllib.error.URLError, *_TRANSPORT_ERRORS) as exc:  # transport drop (wrapped or bare)
            last_exc = exc
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))
    raise last_exc  # unreachable (the loop re-raises on the last attempt), but satisfies the type checker


def _api_pages(path: str) -> list[dict]:
    """Read a GitHub REST endpoint, following `Link` pagination. Returns the items; a single-object
    response (e.g. the repo-metadata endpoint) is returned as a one-element list. Uses the recycled gh
    token when available (authenticated rate limit), otherwise the anonymous public API. Each page is
    fetched with transient-failure retry (5xx / connection drops)."""
    url = path if path.startswith("https://api.github.com/") else "https://api.github.com/" + path.lstrip("/")
    rows: list[dict] = []
    while url:
        request = urllib.request.Request(url, headers=_auth_headers())
        body, response_headers = _urlopen_retrying(request, timeout=30)
        payload = json.loads(body)
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            rows.append(payload)
        link = response_headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                candidate = part[part.find("<") + 1:part.find(">")]
                parsed = urllib.parse.urlparse(candidate)
                if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                    raise ValueError("GitHub API pagination returned an unexpected URL")
                next_url = candidate
                break
        url = next_url
    return rows


def _graphql(query: str, variables: dict) -> dict:
    """POST a GraphQL query to the GitHub API with the recycled token (GraphQL is not available
    anonymously). Returns the `data` object; raises on a missing token or a GraphQL error."""
    token = _github_token()
    if not token:
        raise PermissionError("GitHub GraphQL requires an authenticated gh token")
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    request = urllib.request.Request("https://api.github.com/graphql", data=body, headers=headers, method="POST")
    # Retries transient HTTP 5xx AND transport drops (GraphQL check-suite fetches for many commits are
    # where GitHub most often resets the connection under load).
    response_body, _ = _urlopen_retrying(request, timeout=60)
    payload = json.loads(response_body)
    if payload.get("errors"):
        raise ValueError(f"GitHub GraphQL error: {payload['errors'][:1]}")
    return payload.get("data") or {}


def _verify_public_github(owner: str, name: str) -> str | None:
    prefix = f"https://api.github.com/repos/{owner}/{name}"
    meta = _api_pages(prefix)
    if not meta or meta[0].get("private") is not False or meta[0].get("visibility") not in (None, "public"):
        raise PermissionError("GitHub repository is private or its public visibility could not be verified")
    default_branch = meta[0].get("default_branch")
    return default_branch if isinstance(default_branch, str) and default_branch else None


_ISSUE_REF = re.compile(r"(?:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+))?#(?P<number>\d+)")
_CLOSING_BLOCK = re.compile(
    r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+((?:(?:[\w.-]+/[\w.-]+)?#\d+)"
    r"(?:\s*(?:,|and|&)\s*(?:(?:[\w.-]+/[\w.-]+)?#\d+))*)",
    re.IGNORECASE,
)


def _same_repo_issue_refs(text: str, owner: str, name: str) -> set[int]:
    """Find issue refs that unambiguously target this repo; ignore cross-repo references."""
    found: set[int] = set()
    for match in _ISSUE_REF.finditer(text or ""):
        ref_owner, ref_repo = match.group("owner"), match.group("repo")
        if ref_owner and (ref_owner.casefold(), ref_repo.casefold()) != (owner.casefold(), name.casefold()):
            continue
        found.add(int(match.group("number")))
    return found


def _same_repo_closing_refs(text: str, owner: str, name: str) -> set[int]:
    """Find same-repo issue references covered by GitHub's closing-keyword syntax."""
    return {
        number
        for block in _CLOSING_BLOCK.finditer(text or "")
        for number in _same_repo_issue_refs(block.group(1), owner, name)
    }


def _replace_github_issue_edges(writer: GraphWriter, repo_id: str) -> None:
    """Drop refreshed issue/PR author and cross-reference edges before re-emitting them.

    This removes legacy CREATED_BY author edges and prevents stale PR/issue links when a PR body
    or author changes between daily refreshes.
    """
    prefix = repo_id + "/"
    for label in ("GitIssue", "GitPullRequest"):
        for rel_type in ("CREATED_BY", "OPENED_BY"):
            writer.query(
                f"MATCH (n:{label})-[e:{rel_type}]->(:GitUser) "
                "WHERE n.id STARTS WITH $prefix DELETE e",
                {"prefix": prefix},
            )
    for label in ("REFERENCES_ISSUE", "CLOSES_ISSUE"):
        writer.query(
            f"MATCH (p:GitPullRequest)-[e:{label}]->(:GitIssue) "
            "WHERE p.id STARTS WITH $prefix DELETE e",
            {"prefix": prefix},
        )


def _github_metadata(owner: str, name: str, repo_id: str,
                     default_branch: str | None = None) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    api_root = f"repos/{owner}/{name}"

    def add_github_user(actor: dict | None, target_label: str, target_id: str,
                        rel_type: str = "OPENED_BY") -> str:
        login = (actor or {}).get("login") or ""
        if not login:
            return ""
        identity = f"github:{login}"
        user_id = f"{repo_id}/user/{urllib.parse.quote(identity, safe='')}"
        nodes.append(_node("GitUser", user_id, identity=identity, name=login, source="github"))
        edges.append(_edge(rel_type, target_label, target_id, "GitUser", user_id))
        return login

    # The four list endpoints are independent -> fetch them concurrently (each deep-paginates), then build.
    _meta_paths = {
        "issues": f"{api_root}/issues?state=all&sort=created&direction=desc&per_page=100",
        "pulls": f"{api_root}/pulls?state=all&sort=created&direction=desc&per_page=100",
        "releases": f"{api_root}/releases?per_page=100",
        "tags": f"{api_root}/tags?per_page=100",
    }
    with ThreadPoolExecutor(max_workers=4) as _pool:
        _fetched = dict(zip(_meta_paths, _pool.map(_api_pages, _meta_paths.values())))

    for issue in _fetched["issues"]:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        is_pr = "pull_request" in issue
        label = "GitPullRequest" if is_pr else "GitIssue"
        kind = "pull" if is_pr else "issue"
        obj_id = f"{repo_id}/{kind}/{number}"
        nodes.append(_node(label, obj_id, number=number, title=issue.get("title", ""),
                           body=issue.get("body") or "", state=issue.get("state", ""),
                           url=issue.get("html_url", ""), created_at=issue.get("created_at", ""),
                           updated_at=issue.get("updated_at", ""), closed_at=issue.get("closed_at") or "",
                           labels=[x.get("name", "") for x in issue.get("labels", []) if isinstance(x, dict)],
                           author=(issue.get("user") or {}).get("login", "")))
        edges.append(_edge("HAS_PULL_REQUEST" if is_pr else "HAS_ISSUE", "GitRepository", repo_id, label, obj_id))
        add_github_user(issue.get("user"), label, obj_id)

    issue_numbers = {
        int(node.properties["number"]) for node in nodes if node.label == "GitIssue"
    }

    for pr in _fetched["pulls"]:
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        pr_id = f"{repo_id}/pull/{number}"
        head_sha = (pr.get("head") or {}).get("sha") or ""
        base_sha = (pr.get("base") or {}).get("sha") or ""
        merge_sha = pr.get("merge_commit_sha") or ""
        nodes.append(_node("GitPullRequest", pr_id, number=number, title=pr.get("title", ""),
                           body=pr.get("body") or "", state=pr.get("state", ""),
                           url=pr.get("html_url", ""), created_at=pr.get("created_at", ""),
                           updated_at=pr.get("updated_at", ""), closed_at=pr.get("closed_at") or "",
                           merged=bool(pr.get("merged")), merged_at=pr.get("merged_at") or "",
                           draft=bool(pr.get("draft")), head_sha=head_sha, base_sha=base_sha,
                           merge_commit_sha=merge_sha))
        add_github_user(pr.get("user"), "GitPullRequest", pr_id)
        body = pr.get("body") or ""
        referenced = _same_repo_issue_refs(f"{pr.get('title') or ''}\n{body}", owner, name) & issue_numbers
        base_ref = ((pr.get("base") or {}).get("ref") or "")
        closing = (_same_repo_closing_refs(body, owner, name) & issue_numbers
                   if bool(pr.get("merged")) and default_branch and base_ref == default_branch else set())
        for issue_number in sorted(referenced):
            issue_id = f"{repo_id}/issue/{issue_number}"
            edges.append(_edge("REFERENCES_ISSUE", "GitPullRequest", pr_id, "GitIssue", issue_id))
        for issue_number in sorted(closing):
            issue_id = f"{repo_id}/issue/{issue_number}"
            edges.append(_edge("CLOSES_ISSUE", "GitPullRequest", pr_id, "GitIssue", issue_id))
        for sha in (head_sha, base_sha, merge_sha):
            if sha:
                commit_id = f"{repo_id}/commit/{sha}"
                nodes.append(_node("GitCommit", commit_id, sha=sha, source="github"))
        if merge_sha:
            edges.append(_edge("MERGED_AS", "GitPullRequest", pr_id, "GitCommit",
                               f"{repo_id}/commit/{merge_sha}"))

    for release in _fetched["releases"]:
        tag = release.get("tag_name") or ""
        if not tag:
            continue
        obj_id = f"{repo_id}/release/{release.get('id', tag)}"
        nodes.append(_node("GitRelease", obj_id, tag_name=tag, name=release.get("name") or tag,
                           body=release.get("body") or "", draft=bool(release.get("draft")),
                           prerelease=bool(release.get("prerelease")),
                           published_at=release.get("published_at") or "", url=release.get("html_url", "")))
        edges.append(_edge("HAS_RELEASE", "GitRepository", repo_id, "GitRelease", obj_id))
        add_github_user(release.get("author"), "GitRelease", obj_id, rel_type="CREATED_BY")

    for tag in _fetched["tags"]:
        name = tag.get("name")
        sha = (tag.get("commit") or {}).get("sha")
        if not name:
            continue
        tag_id = f"{repo_id}/tag/{urllib.parse.quote(name, safe='')}"
        nodes.append(_node("GitTag", tag_id, name=name, target_sha=sha or "", source="github"))
        edges.append(_edge("HAS_TAG", "GitRepository", repo_id, "GitTag", tag_id))
        if sha:
            edges.append(_edge("TAGS_COMMIT", "GitTag", tag_id, "GitCommit", f"{repo_id}/commit/{sha}"))
    return nodes, edges


_HEX_SHA = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")

# A commit's check suites + their runs + the publishing app. `first:` caps a pathological commit
# (>20 suites, or a CI matrix >100 runs in one suite); normal commits have one suite and a handful of runs.
_CHECK_FRAGMENT = """
fragment CS on Commit {
  oid
  checkSuites(first: 10) { nodes {
    databaseId status conclusion createdAt updatedAt
    app { databaseId slug name url }
    checkRuns(first: 50) { nodes { databaseId name status conclusion detailsUrl startedAt completedAt } }
  } }
}"""


def _github_checks(owner: str, name: str, repo_id: str, shas) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Collect check suites/runs for the given commit SHAs. The caller passes EVERY ingested commit, so
    CI data covers the whole history. With a recycled gh token this batches ~20 commits per GraphQL query
    and runs the batches in parallel (one round-trip per ~20 commits, instead of one REST call per commit);
    without a token it falls back to the anonymous per-SHA REST Checks endpoint (serial, rate-limited).
    Check runs are provider-neutral: GitHub Actions and third-party GitHub Apps share the shape."""
    sha_list = sorted({str(s) for s in shas if s and _HEX_SHA.match(str(s))})
    if not sha_list:
        return [], []
    if _github_token():
        return _github_checks_graphql(owner, name, repo_id, sha_list)
    return _github_checks_rest(owner, name, repo_id, sha_list)


def _github_checks_graphql(owner: str, name: str, repo_id: str, sha_list: list[str], *,
                           chunk: int = 10, workers: int = 8) -> tuple[list[GraphNode], list[GraphEdge]]:
    chunks = [sha_list[i:i + chunk] for i in range(0, len(sha_list), chunk)]

    def fetch(chunk_shas: list[str]) -> dict:
        fields = "\n".join(f'    c{i}: object(oid: "{sha}") {{ ...CS }}' for i, sha in enumerate(chunk_shas))
        query = f"query($o:String!,$n:String!){{ repository(owner:$o,name:$n){{\n{fields}\n}} }}\n{_CHECK_FRAGMENT}"
        return _graphql(query, {"o": owner, "n": name})

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_suites: set[str] = set()
    seen_runs: set[str] = set()
    seen_apps: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
        results = list(pool.map(fetch, chunks))
    for data in results:
        for commit in ((data or {}).get("repository") or {}).values():
            if not isinstance(commit, dict):
                continue  # a null alias (the oid is not a commit in the public remote)
            sha = commit.get("oid") or ""
            commit_node = f"{repo_id}/commit/{sha}"
            for suite in ((commit.get("checkSuites") or {}).get("nodes") or []):
                suite_db = suite.get("databaseId")
                if suite_db is None:
                    continue
                suite_key = str(suite_db)
                suite_node = f"{repo_id}/check-suite/{suite_key}"
                app = suite.get("app") or {}
                app_db = app.get("databaseId")
                app_node = f"{repo_id}/app/{app_db}" if app_db is not None else ""
                if suite_key not in seen_suites:
                    seen_suites.add(suite_key)
                    nodes.append(_node("GitCheckSuite", suite_node, suite_id=suite_key, head_sha=sha,
                                       status=suite.get("status") or "", conclusion=suite.get("conclusion") or "",
                                       created_at=suite.get("createdAt") or "", updated_at=suite.get("updatedAt") or ""))
                    edges.append(_edge("HAS_CHECK_SUITE", "GitCommit", commit_node, "GitCheckSuite", suite_node))
                if app_db is not None and str(app_db) not in seen_apps:
                    seen_apps.add(str(app_db))
                    nodes.append(_node("GitHubApp", app_node, app_id=str(app_db), slug=app.get("slug") or "",
                                       name=app.get("name") or "", url=app.get("url") or ""))
                for run in ((suite.get("checkRuns") or {}).get("nodes") or []):
                    run_db = run.get("databaseId")
                    if run_db is None or str(run_db) in seen_runs:
                        continue
                    seen_runs.add(str(run_db))
                    run_node = f"{repo_id}/check-run/{run_db}"
                    nodes.append(_node("GitCheckRun", run_node, run_id=str(run_db), name=run.get("name") or "",
                                       status=run.get("status") or "", conclusion=run.get("conclusion") or "",
                                       head_sha=sha, details_url=run.get("detailsUrl") or "",
                                       started_at=run.get("startedAt") or "", completed_at=run.get("completedAt") or ""))
                    edges.append(_edge("HAS_CHECK_RUN", "GitCheckSuite", suite_node, "GitCheckRun", run_node))
                    if app_node:
                        edges.append(_edge("PROVIDED_BY", "GitCheckRun", run_node, "GitHubApp", app_node))
    return nodes, edges


def _github_checks_rest(owner: str, name: str, repo_id: str, sha_list: list[str]) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Anonymous fallback: one REST Checks call per SHA, kept serial (the anonymous API is rate-limited)."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_runs: set[str] = set()
    seen_suites: set[str] = set()
    seen_apps: set[str] = set()
    for sha in sha_list:
        try:
            response = _api_pages(f"repos/{owner}/{name}/commits/{sha}/check-runs?per_page=100")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 422):
                # The local checkout can contain refs not present in the public remote. Such a
                # ref has no public Checks API result; continue collecting the other SHAs and
                # preserve the issues/PRs/releases already fetched for this repository.
                logger.info("No public check data for commit %s (GitHub HTTP %s)", sha, exc.code)
                continue
            raise
        for page in response:
            for run in page.get("check_runs", []) if isinstance(page, dict) else []:
                run_id = run.get("id")
                if run_id is None:
                    continue
                run_key = str(run_id)
                run_node_id = f"{repo_id}/check-run/{run_key}"
                suite = run.get("check_suite") or {}
                suite_id = suite.get("id")
                suite_node_id = f"{repo_id}/check-suite/{suite_id}" if suite_id is not None else ""
                run_sha = run.get("head_sha") or sha
                if suite_id is not None and str(suite_id) not in seen_suites:
                    seen_suites.add(str(suite_id))
                    nodes.append(_node("GitCheckSuite", suite_node_id, suite_id=str(suite_id),
                                       head_sha=run_sha, status=suite.get("status", ""),
                                       conclusion=suite.get("conclusion") or "",
                                       before=suite.get("before") or "", after=suite.get("after") or "",
                                       created_at=suite.get("created_at") or "",
                                       updated_at=suite.get("updated_at") or ""))
                    edges.append(_edge("HAS_CHECK_SUITE", "GitCommit", f"{repo_id}/commit/{run_sha}",
                                       "GitCheckSuite", suite_node_id))
                if run_key not in seen_runs:
                    seen_runs.add(run_key)
                    app = run.get("app") or {}
                    app_id = app.get("id")
                    app_node_id = f"{repo_id}/app/{app_id}" if app_id is not None else ""
                    nodes.append(_node("GitCheckRun", run_node_id, run_id=run_key,
                                       name=run.get("name", ""), status=run.get("status", ""),
                                       conclusion=run.get("conclusion") or "", head_sha=run_sha,
                                       details_url=run.get("details_url") or run.get("html_url", ""),
                                       started_at=run.get("started_at") or "",
                                       completed_at=run.get("completed_at") or ""))
                    if suite_node_id:
                        edges.append(_edge("HAS_CHECK_RUN", "GitCheckSuite", suite_node_id,
                                           "GitCheckRun", run_node_id))
                    if app_id is not None:
                        if str(app_id) not in seen_apps:
                            seen_apps.add(str(app_id))
                            nodes.append(_node("GitHubApp", app_node_id, app_id=str(app_id),
                                               slug=app.get("slug", ""), name=app.get("name", ""),
                                               url=app.get("html_url", "")))
                        edges.append(_edge("PROVIDED_BY", "GitCheckRun", run_node_id,
                                           "GitHubApp", app_node_id))
    return nodes, edges


_AUTHOR_FRAGMENT = "fragment AU on Commit { oid author { user { login } } committer { user { login } } }"


def _github_commit_logins(owner: str, name: str, shas) -> dict[str, dict]:
    """Map each commit SHA to its GitHub author/committer login, when the commit's email is linked to a
    GitHub account. This lets commit authorship UNIFY onto the SAME GitUser node that opens PRs/issues/
    releases (keyed `github:<login>`), instead of a second node keyed by git email -- no duplicate identity
    per person. Batched GraphQL in parallel, like the checks. Best-effort: returns {} without a token or on
    error, so commit ingestion still works offline (it then keys GitUsers by git email as before)."""
    sha_list = sorted({str(s) for s in shas if s and _HEX_SHA.match(str(s))})
    if not sha_list or not _github_token():
        return {}
    chunks = [sha_list[i:i + 50] for i in range(0, len(sha_list), 50)]

    def fetch(chunk_shas: list[str]) -> dict:
        fields = "\n".join(f'    c{i}: object(oid: "{sha}") {{ ...AU }}' for i, sha in enumerate(chunk_shas))
        query = f"query($o:String!,$n:String!){{ repository(owner:$o,name:$n){{\n{fields}\n}} }}\n{_AUTHOR_FRAGMENT}"
        try:
            return _graphql(query, {"o": owner, "n": name})
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, PermissionError):
            return {}

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as pool:
        results = list(pool.map(fetch, chunks))
    for data in results:
        for commit in ((data or {}).get("repository") or {}).values():
            if not isinstance(commit, dict) or not commit.get("oid"):
                continue
            entry = {}
            author_login = ((commit.get("author") or {}).get("user") or {}).get("login")
            committer_login = ((commit.get("committer") or {}).get("user") or {}).get("login")
            if author_login:
                entry["author"] = author_login
            if committer_login:
                entry["committer"] = committer_login
            if entry:
                out[commit["oid"]] = entry
    return out


def _commit_user(logins: dict, hexsha: str, role: str, git_email: str | None,
                 git_name: str | None) -> tuple[str, str, str]:
    """Resolve a commit's author/committer to a GitUser (identity, display, source). Prefer the GitHub
    login (unifies with PR/issue/release authors); otherwise fall back to the git email/name."""
    login = (logins.get(hexsha) or {}).get(role)
    if login:
        return f"github:{login}", login, "github"
    return (git_email or git_name or ""), (git_name or git_email or ""), "git"


def sync_git_info(
    writer: GraphWriter,
    repo_path: str | Path,
    *,
    source_uri: str | None = None,
    force_remote: bool = False,
) -> dict:
    """Idempotently ingest all locally reachable commits/tags and public GitHub issues,
    pull requests, releases, and tags. Remote API failures leave local Git data intact."""
    repo = git.Repo(str(repo_path))
    canonical, github = _canonical_repo(repo, source_uri or str(repo_path))
    source_candidate = Path(source_uri).expanduser() if source_uri else Path(repo_path)
    if source_candidate.exists():
        source_path = str(source_candidate.resolve())
    elif repo.working_tree_dir:
        source_path = str(Path(repo.working_tree_dir).resolve())
    else:
        source_path = str(source_uri or repo_path)
    if not github:
        _unlink_checkout(writer, source_path)
        logger.info("Git information skipped for %s: no verifiable public GitHub remote", canonical)
        return {"repository": canonical, "skipped": "public GitHub remote not identified"}
    prospective_repo_id = f"github://{github[0]}/{github[1]}"
    writer.setup_schema(NODE_SCHEMAS)
    checkout_id = _checkout_id(source_path)
    linked_before_check = writer.ro_query(
        "MATCH (:GitCheckout {id: $id})-[:USES_REPOSITORY]->(r:GitRepository) RETURN r.id",
        {"id": checkout_id},
    ).result_set
    try:
        default_branch = _verify_public_github(*github)
    except PermissionError as exc:
        _unlink_checkout(writer, source_path)
        logger.warning("Git information skipped for %s: public visibility check failed (%s)", canonical, exc)
        return {"repository": canonical, "skipped": "public visibility check failed"}
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        # A transient API outage must not erase data previously collected from this same
        # public repo. If origin changed, unlink the old repo because it no longer describes
        # this checkout, even while the new target's visibility remains unverified.
        if any(old_id != prospective_repo_id for (old_id,) in linked_before_check):
            _unlink_checkout(writer, source_path)
        logger.warning("Git information skipped for %s: public visibility check failed (%s)", canonical, exc)
        return {"repository": canonical, "skipped": "public visibility check failed"}

    repo_id = prospective_repo_id
    linked = writer.ro_query(
        "MATCH (:GitCheckout {id: $id})-[:USES_REPOSITORY]->(r:GitRepository) RETURN r.id",
        {"id": checkout_id},
    ).result_set
    if any(old_id != repo_id for (old_id,) in linked):
        _unlink_checkout(writer, source_path)
    legacy_repo_id = _repo_id_for_source(writer, source_path)
    if legacy_repo_id and legacy_repo_id != repo_id:
        linked_elsewhere = writer.ro_query(
            "MATCH (:GitCheckout)-[:USES_REPOSITORY]->(r:GitRepository {id: $id}) RETURN count(r)",
            {"id": legacy_repo_id},
        ).result_set
        if not linked_elsewhere or not linked_elsewhere[0][0]:
            _delete_repo_subgraph(writer, legacy_repo_id)

    repo_node = _node("GitRepository", repo_id, name=canonical.rstrip("/").rsplit("/", 1)[-1],
                      canonical_url=canonical, provider="github", source_path=source_path)
    writer.merge_node(repo_node)
    checkout_node = _node("GitCheckout", checkout_id, source_path=source_path)
    writer.merge_node(checkout_node)
    writer.merge_edge(_edge("USES_REPOSITORY", "GitCheckout", checkout_id, "GitRepository", repo_id))

    state_rows = writer.ro_query(
        "MATCH (r:GitRepository {id: $id}) RETURN r.branch_tips, r.github_synced_at", {"id": repo_id}
    ).result_set
    previous_tips = json.loads(state_rows[0][0]) if state_rows and state_rows[0][0] else {}
    previous_remote_sync = state_rows[0][1] if state_rows else None
    current_tips = {branch.name: branch.commit.hexsha for branch in repo.heads}

    def commits_to_add():
        seen: set[str] = set()
        if not previous_tips:
            candidates = repo.iter_commits("--all")
            for commit in candidates:
                if commit.hexsha not in seen:
                    seen.add(commit.hexsha)
                    yield commit
            return
        for branch_name, tip in current_tips.items():
            old_tip = previous_tips.get(branch_name)
            if old_tip == tip:
                continue
            try:
                walk = repo.iter_commits(f"{old_tip}..{tip}") if old_tip and repo.is_ancestor(old_tip, tip) else repo.iter_commits(tip)
            except (git.BadName, git.GitCommandError):
                walk = repo.iter_commits(tip)
            for commit in walk:
                if commit.hexsha not in seen:
                    seen.add(commit.hexsha)
                    yield commit

    # Commit histories can be very large. Stream nodes first, then edges (edge MERGE needs
    # both endpoints to exist); this bounds working memory independently of history length.
    commit_count = 0
    node_batch: list[GraphNode] = []
    user_ids_seen: set[str] = set()
    file_targets = _code_file_index(writer, source_uri or source_path, canonical)
    commits = list(commits_to_add())
    file_changes_by_sha = _batch_commit_file_changes(repo, commits)
    commit_logins = _github_commit_logins(github[0], github[1], [c.hexsha for c in commits])

    for commit in commits:
        cid = f"{repo_id}/commit/{commit.hexsha}"
        changes = file_changes_by_sha.get(commit.hexsha, [])
        node_batch.append(_node("GitCommit", cid, sha=commit.hexsha, message=commit.message.strip(),
                                authored_at=commit.authored_datetime.isoformat(),
                                committed_at=commit.committed_datetime.isoformat(),
                                summary=commit.summary, parents=[p.hexsha for p in commit.parents],
                                changed_files=json.dumps(changes, separators=(",", ":"))))
        for role, git_email, git_name in (("author", commit.author.email, commit.author.name),
                                          ("committer", commit.committer.email, commit.committer.name)):
            identity, display, source = _commit_user(commit_logins, commit.hexsha, role, git_email, git_name)
            if not identity:
                continue
            user_id = f"{repo_id}/user/{urllib.parse.quote(identity, safe='')}"
            if user_id not in user_ids_seen:
                user_ids_seen.add(user_id)
                node_batch.append(_node("GitUser", user_id, identity=identity, name=display, source=source))
        commit_count += 1
        if len(node_batch) >= 2000:
            writer.merge_nodes(node_batch)
            node_batch.clear()
    if node_batch:
        writer.merge_nodes(node_batch)

    edge_batch: list[GraphEdge] = []
    for commit in commits:
        cid = f"{repo_id}/commit/{commit.hexsha}"
        edge_batch.append(_edge("HAS_COMMIT", "GitRepository", repo_id, "GitCommit", cid))
        for rel, role, git_email, git_name in (
            ("AUTHORED", "author", commit.author.email, commit.author.name),
            ("COMMITTED", "committer", commit.committer.email, commit.committer.name),
        ):
            identity, _display, _source = _commit_user(commit_logins, commit.hexsha, role, git_email, git_name)
            if not identity:
                continue
            user_id = f"{repo_id}/user/{urllib.parse.quote(identity, safe='')}"
            edge_batch.append(_edge(rel, "GitCommit", cid, "GitUser", user_id))
        for parent in commit.parents:
            edge_batch.append(_edge("PARENT_COMMIT", "GitCommit", cid, "GitCommit",
                                    f"{repo_id}/commit/{parent.hexsha}"))
        for change in file_changes_by_sha[commit.hexsha]:
            properties = {
                "change_type": change["change_type"],
            }
            path = change["path"]
            edge_batch.extend(_edge("CHANGED", "GitCommit", cid, "File", target_id, **properties)
                              for target_id in file_targets.get(path, ()))
        if len(edge_batch) >= 2000:
            writer.merge_edges(edge_batch)
            edge_batch.clear()

    local_tags = list(repo.tags)
    ref_nodes, ref_edges = [], []
    for tag in local_tags:
        tag_id = f"{repo_id}/tag/{urllib.parse.quote(tag.name, safe='')}"
        target = tag.commit.hexsha
        ref_nodes.append(_node("GitTag", tag_id, name=tag.name, target_sha=target, source="git"))
        ref_edges.extend((
            _edge("HAS_TAG", "GitRepository", repo_id, "GitTag", tag_id),
            _edge("TAGS_COMMIT", "GitTag", tag_id, "GitCommit", f"{repo_id}/commit/{target}"),
        ))
    for branch in repo.heads:
        branch_id = f"{repo_id}/branch/{urllib.parse.quote(branch.name, safe='')}"
        target = branch.commit.hexsha
        ref_nodes.append(_node("GitBranch", branch_id, name=branch.name, target_sha=target))
        ref_edges.extend((
            _edge("HAS_BRANCH", "GitRepository", repo_id, "GitBranch", branch_id),
            _edge("POINTS_TO_COMMIT", "GitBranch", branch_id, "GitCommit", f"{repo_id}/commit/{target}"),
        ))
    if ref_nodes:
        writer.merge_nodes(ref_nodes)
    edge_batch.extend(ref_edges)
    if edge_batch:
        writer.merge_edges(edge_batch)

    remote_stats = {"issues": 0, "pull_requests": 0, "releases": 0, "github_tags": 0,
                    "check_suites": 0, "check_runs": 0}
    remote_due = force_remote or not previous_remote_sync
    if previous_remote_sync and not force_remote:
        try:
            remote_due = datetime.now(timezone.utc) - datetime.fromisoformat(previous_remote_sync) >= timedelta(days=1)
        except ValueError:
            remote_due = True
    remote_synced_at = previous_remote_sync
    if remote_due:
        try:
            remote_nodes, remote_edges = _github_metadata(*github, repo_id, default_branch)
            check_shas = {commit.hexsha for commit in commits}   # CI for EVERY ingested commit (full history)
            check_shas.update(current_tips.values())
            check_shas.update(
                str(node.properties.get("head_sha")) for node in remote_nodes
                if node.label == "GitPullRequest" and node.properties.get("head_sha")
            )
            check_nodes, check_edges = _github_checks(*github, repo_id, check_shas)
            remote_nodes.extend(check_nodes)
            remote_edges.extend(check_edges)
            # Metadata volumes are normally far smaller than commit history; still batch writes.
            _replace_github_issue_edges(writer, repo_id)
            writer.merge_nodes(remote_nodes)
            writer.merge_edges(remote_edges)
            remote_stats = {
                "issues": sum(n.label == "GitIssue" for n in remote_nodes),
                "pull_requests": sum(n.label == "GitPullRequest" for n in remote_nodes),
                "releases": sum(n.label == "GitRelease" for n in remote_nodes),
                "github_tags": sum(n.label == "GitTag" and n.properties.get("source") == "github" for n in remote_nodes),
                "check_suites": sum(n.label == "GitCheckSuite" for n in remote_nodes),
                "check_runs": sum(n.label == "GitCheckRun" for n in remote_nodes),
            }
            remote_synced_at = datetime.now(timezone.utc).isoformat()
        except (urllib.error.URLError, TimeoutError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("GitHub metadata was skipped for %s: %s", canonical, exc)

    writer.merge_node(_node("GitRepository", repo_id, name=canonical.rstrip("/").rsplit("/", 1)[-1],
                            canonical_url=canonical, provider="github", source_path=source_path,
                            branch_tips=json.dumps(current_tips, sort_keys=True),
                            github_synced_at=remote_synced_at or ""))
    return {"repository": canonical, "commits": commit_count, "users": len(user_ids_seen),
            "tags": len(local_tags) + remote_stats["github_tags"], **remote_stats}
