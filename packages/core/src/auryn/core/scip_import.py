"""Generic SCIP importer: build an Auryn code graph DIRECTLY from any indexer's `index.scip`, with no fast
(tree-sitter) pass. This is the "bring your own SCIP index" path -- a team points Auryn at the `index.scip`
their CI already produces (scip-typescript / scip-java / scip-go / rust-analyzer / scip-clang / scip-ruby /
scip-python / ...), and gets a graph for languages Auryn ships no parser for.

What it derives, and why it's RELIABLE across indexers:
  - File nodes, one per document, tagged with `Document.language`.
  - Function / Method / Type nodes from DEFINITION occurrences, with kind + name + containment taken from the
    SCIP DESCRIPTOR grammar (see core.scip_descriptor), NOT from `SymbolInformation.kind`/`display_name` --
    indexers populate those inconsistently (scip-clang omits kind; scip-typescript leaves display_name empty),
    whereas the descriptor (`pkg/Type#method().`) is emitted by all and encodes the full hierarchy.
  - the entity->file link in Auryn's cross-tool shape: a `file_path` property on each symbol (as C/C++/TS
    carry) AND a DEFINED_IN edge (entity -> File, as Go/Java/Rust/PHP carry), so impact_of_diff and get_context
    both treat it like a native graph; plus HAS_METHOD edges (Type -> its methods).

What it does NOT derive here (the structural pass): CALLS. A reference occurrence names the CALLEE but not
the caller; attributing it needs the enclosing callable's line span, which SCIP carries only in the optional
`Occurrence.enclosing_range` -- absent from every indexer sampled. CALLS is a separate follow-up (enclosing
ranges when present, else a line-ordering heuristic).

Nodes are keyed by the SCIP symbol (globally unique + stable within a version), project-scoped so the same
index can be imported into different graphs without collision."""

from __future__ import annotations

from collections.abc import Iterator

from auryn.core import scip, scip_descriptor as sd
from auryn.core.graph_model import GraphEdge, GraphNode

# descriptor-last-component kind -> (node label, is a callable). Terms/namespaces/params are not emitted here.
_CALLABLE_KINDS = {sd.METHOD}
_TYPE_KINDS = {sd.TYPE}


def _norm_path(path: str) -> str:
    # removeprefix, NOT lstrip: lstrip("./") would strip a leading run of dots/slashes and collapse a
    # dotfile path (`.config/tool.py` -> `config/tool.py`) onto a real sibling, giving them one File id.
    return path.replace("\\", "/").removeprefix("./")


def _basename(path: str) -> str:
    return _norm_path(path).rsplit("/", 1)[-1]


def file_id(project_id: str, path: str) -> str:
    return f"{project_id}::scipfile::{_norm_path(path)}"


def symbol_id(project_id: str, symbol: str) -> str:
    return f"{project_id}::{symbol}"


def _label_for(comps: list[sd.Descriptor]) -> str | None:
    """The graph label for a definition whose descriptor components are `comps` (outermost first), or None to
    skip (a term, namespace, local, parameter, ...). A method with a type ancestor is a Method, else a Function."""
    if not comps:
        return None
    last = comps[-1]
    if last.kind in _TYPE_KINDS:
        return "Type"
    if last.kind in _CALLABLE_KINDS:
        has_type_parent = any(c.kind == sd.TYPE for c in comps[:-1])
        return "Method" if has_type_parent else "Function"
    return None


def _type_parent(comps: list[sd.Descriptor]) -> sd.Descriptor | None:
    """The nearest enclosing TYPE component of the last component (for HAS_METHOD), or None."""
    for c in reversed(comps[:-1]):
        if c.kind == sd.TYPE:
            return c
    return None


def build_graph(index_bytes: bytes, project_id: str, *, project_name: str = "",
                repo_uri: str = "") -> Iterator[GraphNode | GraphEdge]:
    """Yield the Project + File + symbol nodes and DEFINED_IN/HAS_METHOD/HAS_FILE edges for `index_bytes`.
    Re-iterable callers (stream_apply runs the generator twice) should pass a fresh call each time."""
    documents = scip.parse_index(index_bytes)

    # Pass 1: collect each defined Function/Method/Type (dedup by symbol, first definition wins) + its file.
    # A type may be defined in several files (C++ header + ...); keep the first, so HAS_METHOD/DEFINED_IN are stable.
    defs: dict[str, tuple[str, str, str, int]] = {}  # symbol -> (label, name, file path, 1-based def line)
    for doc in documents:
        path = _norm_path(doc.relative_path)
        for occ in doc.occurrences:
            if not occ.is_definition or sd.is_local(occ.symbol):
                continue
            comps = sd.parse(occ.symbol)
            label = _label_for(comps)
            if label is None or occ.symbol in defs:
                continue
            defs[occ.symbol] = (label, comps[-1].name, path, occ.start_line + 1)

    yield GraphNode("Project", {"id": project_id},
                    {"name": project_name or project_id, "repo_uri": repo_uri, "source": "scip", "depth": "scip"})

    # File nodes, one per document (all of them -- a file with only references still exists in the codebase).
    seen_files: set[str] = set()
    for doc in documents:
        path = _norm_path(doc.relative_path)
        if not path or path in seen_files:
            continue
        seen_files.add(path)
        fid = file_id(project_id, path)
        yield GraphNode("File", {"id": fid},
                        {"name": _basename(path), "path": path, "language": (doc.language or "").lower()})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})

    # Symbol nodes + their edges. The entity->file link follows Auryn's cross-tool conventions so a
    # generic-imported graph is first-class in both get_context (edge-agnostic 1-hop) AND impact_of_diff,
    # which locates a file's symbols by EITHER a `file_path` property (as C/C++/TS carry) OR a DEFINED_IN edge
    # (as Go/Java/Rust/PHP carry) -- both gated on line_start/line_end. We emit both the property and the edge,
    # and set line_end (= line_start until body spans arrive with the CALLS slice: SCIP definition occurrences
    # anchor only the name line; the full span needs Occurrence.enclosing_range, absent from sampled indexers).
    for symbol, (label, name, path, line) in defs.items():
        nid = symbol_id(project_id, symbol)
        yield GraphNode(label, {"id": nid},
                        {"name": name, "line_start": line, "line_end": line, "file_path": path,
                         "scip_symbol": symbol, "resolution": "scip"})
        yield GraphEdge("DEFINED_IN", label, {"id": nid}, "File", {"id": file_id(project_id, path)})
        # HAS_METHOD: the enclosing type's node -> this method, when that type is itself defined in the index.
        if label == "Method":
            parent = _type_parent(sd.parse(symbol))
            if parent is not None:
                parent_symbol = sd.ancestor_symbol(symbol, parent)
                if parent_symbol in defs:
                    yield GraphEdge("HAS_METHOD", "Type", {"id": symbol_id(project_id, parent_symbol)},
                                    label, {"id": nid})
