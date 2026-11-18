# Adding a language ingestor

This is the full, worked guide to contributing a new **file-based language ingestor** —
the kind that reads source files (Python, TypeScript, C#, Java, Go, Rust, …) and writes a
structural subgraph. If you're skimming, [CONTRIBUTING.md](../CONTRIBUTING.md) has the
short checklist; this page explains the *why* behind each step and walks the reference
implementation line by line.

We use the **Go plugin** (`packages/go/`) as the reference throughout: it's a complete,
single-phase tree-sitter ingestor with no live-DB or SDK dependencies, so it's the
cleanest template to copy. The Rust, PHP, Julia, C, and Java plugins are near-identical in
shape — read any of them alongside this guide.

> **Three kinds of ingestor.** Most contributions are the file-based kind covered here.
> Two other extension points exist and are covered briefly at the end:
> [live-source ingestors](#appendix-a-live-source-ingestors-baseingestor) (read a database
> connection, e.g. `oracle`/`sql`) and
> [enrichment passes](#appendix-b-enrichment-passes) (derive edges from the finished graph,
> e.g. the C#→PL/SQL routing seam).

---

## 1. The big picture in 90 seconds

```
  git repo / local dir
        │  list_files(suffix) → read_blob(sha)          ← the tiny Source surface
        ▼
  ┌──────────────┐   phase 1: one pure function per file, parallel across a process pool
  │ file_parser  │   parse_source(bytes, path) → RawFile   (picklable, no tree-sitter escapes)
  └──────┬───────┘
         │  list[RawFile]
         ▼
  ┌──────────────┐   phase 2: whole-project pass — cross-file resolution happens here
  │  resolution  │   resolve(raw_files, project_id) → Iterator[GraphNode | GraphEdge]
  └──────┬───────┘
         │  target state (every node/edge the repo should produce at this commit)
         ▼
  ┌──────────────┐   diff target-state against what the graph currently holds, write only Δ
  │  git_sync    │   read_fingerprints → compute_delta → apply_delta   (idempotent)
  └──────────────┘
```

Two rules drive the whole design:

1. **Phase 1 is per-file and pure; phase 2 is whole-project.** A single file can't know
   whether the name `Store` refers to a type defined three files over, so file parsing only
   records *what one file can see on its own* (names, types-as-written, call sites with
   their receiver identifier). Every cross-file decision — attaching a method to its
   receiver type, resolving a call, deciding a reference is external — is deferred to
   phase 2, when the whole file set is visible at once. This split is also what lets phase 1
   run in a `ProcessPoolExecutor`: only picklable dataclasses cross the boundary, never a
   tree-sitter node.

2. **The graph is recomputed and diffed, never appended to.** Every sync recomputes the
   *complete* target state for the project at that commit and diffs it against the graph.
   This is why re-ingesting unchanged content is a no-op, why moving to any other
   commit/branch is the same operation, and why **node ids must be stable** — the id *is*
   the identity used to match "same symbol" across syncs.

---

## 2. Package layout

Each language ships as its own distribution, `auryn-<lang>`, under `packages/<lang>/`.
Copy the Go package's shape exactly:

```
packages/go/
├── pyproject.toml
└── src/
    └── auryn/
        └── ingestors/
            └── go/
                ├── __init__.py       # empty — leaf marker only
                ├── plugin.py         # entry-point target: import-light registration
                ├── grammar.py        # tree-sitter Language/Parser setup
                ├── raw_model.py      # picklable phase-1 dataclasses
                ├── file_parser.py    # parse_source(bytes, path) → RawFile   (phase 1)
                ├── resolution.py     # resolve(files, project_id) → nodes/edges (phase 2)
                ├── ids.py            # canonical id scheme  (go://…)
                ├── schema.py         # NODE_SCHEMAS / EDGE_SCHEMAS
                └── git_sync.py       # delta-sync engine + GitLanguage adapter
```

> **Namespace packages, on purpose.** There is deliberately **no** `__init__.py` at
> `src/auryn/` or `src/auryn/ingestors/`. Those are [PEP 420 namespace
> packages](https://peps.python.org/pep-0420/), so every installed `auryn-<lang>`
> distribution *merges* into the one `auryn.ingestors.*` namespace. Add an `__init__.py`
> at either level and you'll shadow the other language plugins. Only the leaf
> (`ingestors/go/__init__.py`) gets one, and it's empty.

### `pyproject.toml`

```toml
[project]
name = "auryn-go"
dynamic = ["version"]             # derived from git, like every package -- see the last two tables
description = "Go language plugin for Auryn: …"
authors = [{ name = "…", email = "…" }]
requires-python = ">=3.12"
dependencies = [
    "auryn-core",
    "tree-sitter>=0.26.0",
    "tree-sitter-go>=0.25.0",     # the pre-built grammar for your language
]

# THIS is how the plugin is discovered — an entry point, not an import scan.
[project.entry-points."auryn.languages"]
go = "auryn.ingestors.go.plugin"

[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/auryn"]

# Copy these two tables from any sibling package: the version comes from git (a release builds
# every package at the tag's version), and uv rebuilds a local install when the sources change.
[tool.hatch.version]
source = "vcs"
fallback-version = "0.0.0.dev0"
raw-options = { root = "../..", local_scheme = "node-and-timestamp" }

[tool.uv]
cache-keys = [
    { file = "pyproject.toml" }, { git = { commit = true, tags = true } },
    { file = "src/**/*.py" },    # + any other file type your package ships
]
```

The entry point is the single most important line. When the umbrella asks
`discover_git_languages()`, core loads every entry point in the **`auryn.languages`**
group and imports its target module — running that module's registration side effect. You
never edit a central registry or list your module in an `__init__.py`; declaring the entry
point *is* the registration. (Older revisions of this project used an `__init__.py`
import-scan; that's gone. If you see it in a stale doc, ignore it.)

There are three groups, split by concern so a consumer loads only what it needs:

| Entry-point group | Register with | Reached from | Your file-based plugin uses |
|---|---|---|---|
| `auryn.languages` | `register_git_language()` | `ingest dir`/`ingest repo`, the `ingest_*` MCP tools | **✅ this one** |
| `auryn.ingestors` | `@register_ingestor("name")` | `auryn run <name>` | only [live-source](#appendix-a-live-source-ingestors-baseingestor) ingestors |
| `auryn.enrichments` | `register_graph_enrichment()` | run after languages by the ingest endpoint | only [enrichment passes](#appendix-b-enrichment-passes) |

---

## 3. `plugin.py` — import-light registration

This module is the entry-point target, and it has one job: **register a `GitLanguage`
without importing your parser.** Discovery imports every language plugin eagerly, but a
consumer who only has C# files in their repo should never pay to import tree-sitter-go. So
the parser import is deferred behind a lazy `sync` thunk:

```python
"""Import-light Go language registration — the `auryn.languages` entry-point target."""
from __future__ import annotations
from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "go"
FILE_SUFFIXES = (".go",)
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "vendor", "node_modules", "testdata", "bin", "dist", ".idea", ".vscode"}
)

def _sync(*args, **kwargs):
    """Deferred until a source actually has Go files — imports the tree-sitter engine here."""
    from auryn.ingestors.go.git_sync import _git_language_sync
    return _git_language_sync(*args, **kwargs)

register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
```

Keep `LANGUAGE`, `FILE_SUFFIXES`, and `EXCLUDED_DIR_NAMES` here as the single source of
truth — `git_sync.py` imports them back. `file_suffixes` are how the endpoint decides your
language is present in a repo and which blobs to hand you (lowercased, dot-included).
`excluded_dir_names` should list build output, vendored deps, and anything your language's
own toolchain ignores (for Go: `vendor`, `testdata`).

The `GitLanguage` contract (`core/registry.py`):

```python
@dataclass(frozen=True)
class GitLanguage:
    language: str
    file_suffixes: tuple[str, ...]
    excluded_dir_names: frozenset[str]
    sync: Callable[..., Any]
    deep_sync: Callable[..., Any] | None = None   # optional second pass — see §9
```

---

## 4. `grammar.py` — the parser

Trivial for any language with a pre-built tree-sitter grammar on PyPI:

```python
import tree_sitter_go as tsgo
from tree_sitter import Language, Parser

LANGUAGE = Language(tsgo.language())

def new_parser() -> Parser:
    return Parser(LANGUAGE)
```

Keep it in one place so `file_parser.py` and your tests get an identically configured
parser. (PL/SQL is the exception — it has no tree-sitter grammar and vendors an ANTLR one
instead. If your language is in that boat, look at `packages/oracle/`.)

---

## 5. `raw_model.py` — what one file can know

These are the picklable dataclasses phase 1 emits. The discipline: record **only what a
single file's parse can see on its own**, as *text*. Names, types-as-written (`"*Store"`,
`"[]string"`, `"map[string]int"`), the unqualified/qualified type-reference names a
signature mentions, and call sites with their receiver *identifier*. No resolution, no
inference, nothing that needs another file.

```python
@dataclass
class RawParameter:
    name: str
    type: str          # type as written, verbatim
    position: int
    is_variadic: bool = False

@dataclass
class RawCall:
    name: str                     # callee's simple name
    receiver: str | None = None   # the `x` in `x.foo()`; None for bare `foo()`

@dataclass
class RawFunction:
    name: str
    receiver: RawReceiver | None = None    # None → free function; set → method
    parameters: list[RawParameter] = field(default_factory=list)
    return_type: str = ""
    type_refs: list[str] = field(default_factory=list)   # "Name" | "pkg.Name" refs it mentions
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""      # leading comment above the declaration (see below)
    line_start: int = 0
    line_end: int = 0

@dataclass
class RawFile:
    path: str
    package: str = ""
    imports: list[RawImport] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
    consts: list[RawConst] = field(default_factory=list)
```

Model *your language's* shape, not a lowest common denominator. Go puts methods at top
level with a receiver, so `RawFunction` carries an optional `RawReceiver`; a class-based
language would nest methods inside a `RawType` instead. Record line numbers as **1-based
inclusive** (`0` = not captured) to match the other ingestors.

**Capture `doc`** (the leading comment) on every callable and named type — it is a
[shared node property](graph-model.md#shared-node-properties) and the natural-language input
to semantic search. Don't hand-roll it: use `core.docstrings`.

```python
from auryn.core.docstrings import leading_comment

# tree-sitter engines: pass the *declaration* node (comments are its preceding siblings).
# For grammars where the doc sits above an enclosing node (e.g. Go's `type_declaration`
# wraps `type_spec`), fall back to the parent: leading_comment(spec, src) or leading_comment(decl, src)
fn.doc = leading_comment(func_decl_node, source)
```

An `ast`/Roslyn-style engine (parser discards comments) uses the source-text scan instead —
`leading_comment_from_source(text, top_line, prefixes=("#",))` — where `top_line` is the
declaration's first line **above any decorators/attributes** (C# passes `prefixes=("///","//")`).

---

## 6. `file_parser.py` — phase 1

One pure function, `parse_source(source: bytes, path_label: str) -> RawFile`, plus a
`parse_file(path)` convenience wrapper. Taking raw bytes (not a path) is what lets a git
blob be parsed without ever touching disk.

```python
def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)
    # …walk tree-sitter nodes, build RawFile from raw_model dataclasses…
    return raw_file
```

Two things to get right:

- **Verify node/field names against your actual grammar version.** Don't assume tree-sitter
  node types — probe them. The Go parser's module docstring records every `source_file`
  child type and field it relies on, confirmed empirically against `tree-sitter-go 0.25.0`.
  Do the same; grammars differ between versions and between languages in non-obvious ways
  (e.g. PHP's grammar entry point is `language_php()`, not `language()`).
- **Stay pure and picklable.** No shared state, no tree-sitter object in the return value —
  only `raw_model` dataclasses. This function runs under a process pool, one file per task.

---

## 7. `ids.py` — the identity scheme (get this right first)

**Ids are your idempotency key.** Every node and edge is written with `MERGE` keyed on its
id. Re-running must produce the *same* id for the *same* source symbol, or you get
duplicates instead of updates, and the delta sync breaks. Design this before you write any
emitter.

Root everything at `<lang>://<project>` and build hierarchically:

```python
def project_id(project_name: str) -> str:
    return f"go://{project_name}"

def package_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/package/{name}"

def function_id(pkg_id: str, name: str, param_types: list[str]) -> str:
    return f"{pkg_id}.func.{name}({_signature(param_types)})"

def method_id(owner_type_id: str, name: str, param_types: list[str]) -> str:
    return f"{owner_type_id}.method.{name}({_signature(param_types)})"
```

Design rules, learned the hard way across the existing ingestors:

- **Ids embed names and structural position — never *resolved* or *inferred* types.** A
  parameter's type text goes in the signature; an inferred/resolved type does not. This is
  what keeps a future [deep pass](#13-two-phase-languages-deep_sync) interchangeable in the
  same graph: both engines key the same symbol identically.
- **Respect your language's case rules.** Go is case-sensitive *and* encodes export in case
  (`Put` exported, `put` not), so its ids must **not** lowercase identifiers — doing so
  would collide `Foo`/`foo`. Java can and does lowercase. Know which yours is.
- **Encode generic arity** so `Container` and `Container[T]` can't collide.
- **Project-scope real nodes.** The `<project>` in the root keeps two repos' same-named
  symbols distinct in one graph. The one thing that deliberately crosses that boundary is
  the cross-repo linkage key — see §8.

---

## 8. `schema.py` — labels, and the shared contract

Declare a `NodeSchema` for every label and an `EdgeSchema` for every relationship type you
write. `NodeSchema` drives index/constraint creation (`setup_schema`) and the fingerprint
read (`read_fingerprints`) that the delta sync depends on.

```python
NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Package", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: §8.2
    NodeSchema(label="Method",   indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    # …one per label you emit…
]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_PACKAGE"),
    EdgeSchema(rel_type="HAS_FUNCTION"),
    EdgeSchema(rel_type="CALLS"),        # carries a {resolution} property — see contract below
    EdgeSchema(rel_type="DEPENDS_ON"),
    # …
]
```

### 8.1 Honour the cross-ingestor contract

The whole point of a shared graph is that a query like "who calls X" works the same across
languages. So reuse the shared vocabulary rather than inventing parallel names. The
[graph model reference](graph-model.md) is the authoritative list; the load-bearing rules:

- **Callables are `Function` (free) and `Method` (bound to a type).**
- **The call edge is `CALLS`, and it carries a `resolution` property** recording provenance
  (`"name"` for a syntactic/name-based match, `"external"` for a cross-repo placeholder,
  and richer values like `"resolved"`/`"inferred"` from a deep engine). Never emit a
  `CALLS` edge without it.
- **`Parameter` nodes carry a `type` property**, attached via `HAS_PARAMETER`.
- **Inheritance is `EXTENDS` (class→class, interface→interface) and `IMPLEMENTS`
  (class→interface); type references are `DEPENDS_ON`.** Map your language's concepts onto
  these where they fit; add language-specific edges (Go's `EMBEDS`, PHP's `USES_TRAIT`,
  Julia's `DISPATCHES_ON`) where they genuinely don't.

> **Emit no edge rather than a wrong one.** The fast tree-sitter pass is name-based. When a
> reference is ambiguous or unresolvable, drop it — don't guess. Every existing ingestor
> makes this trade, and downstream tools rely on it: a present edge is trustworthy.
> Structural interface satisfaction (Go/PHP `IMPLEMENTS`) genuinely can't be decided
> without real types, so the fast pass stays silent on it and leaves it to the deep pass.

### 8.2 Cross-repo linkage (ADR 0004)

When one repo calls a symbol defined in another (or in the stdlib), the fast pass can't
resolve it — but it can often *name* it. Instead of dropping such a call, emit a
**placeholder node** reached by a normal `CALLS` edge, so that ingesting the defining repo
later *heals* it into a real edge. The mechanism is a project-independent **linkage key**:

- On a locally-defined, externally-nameable symbol (an *exported* function/method), stamp a
  `linkage_key` property — how an outside consumer would name it: `package.Func`,
  `pkg.Type.Method`. **Name-based only**, never parameter types (a consumer can't know
  those).
- For an unresolved call you *can* name, emit a placeholder node whose **`id` equals that
  same key** (e.g. `extern://go/fmt.Println`), with `resolved: false` and an
  `origin` (`"stdlib"` vs `"external"`).
- `git_sync.py` then calls `reconcile_linkages()` (below), which folds
  `placeholder.id == real.linkage_key` pairs — upgrading the placeholder in place when both
  repos live in one graph.

Index `linkage_key` on any label that can carry one (see `NodeSchema` above). Full rationale
in [`docs/adr/0004-cross-repo-symbol-resolution.md`](adr/0004-cross-repo-symbol-resolution.md).
This is optional for a first cut — but cheap to include, and it's what makes a multi-repo
graph resolve across boundaries.

---

## 9. `resolution.py` — phase 2

`resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]` is
where the graph is actually built. It has the whole file set, so this is where every
cross-file decision lives. The Go pass is a good template for the ordering:

1. **Emit files and packages**, collecting the set of project package names.
2. **Index types**, then **index functions/methods** (methods need the type index to find
   their receiver owner). Build name→symbol lookup tables.
3. **Pass 1 — nodes + structural edges.** Emit every type, function, method, parameter,
   field, const, and its containment edge (`HAS_TYPE`, `HAS_FUNCTION`, `HAS_METHOD`,
   `HAS_PARAMETER`, …). These are the landing nodes pass 2's edges point at.
4. **Pass 2 — semantic edges.** `DEPENDS_ON` (resolve each type-ref against the type
   index), `CALLS` (resolve each call site: bare name → same-scope function; `recv.m()`
   where `recv`'s static type is known → that type's method; `pkg.f()` → that package's
   function), and any language-specific edges. Unresolved-but-nameable calls become
   [linkage placeholders](#82-cross-repo-linkage-adr-0004); truly unnameable ones are
   dropped.

The resolver's job is entirely name-and-scope bookkeeping — which is why it's honest about
its limits and tags every `CALLS` edge `resolution="name"`. Keep it a generator; the caller
batches whatever it yields.

---

## 10. `git_sync.py` — the delta engine

This wires phases 1–2 into the recompute-and-diff model. It's boilerplate you can copy
almost verbatim from Go, changing only the imports. Three functions:

**`sync_at(...)`** — recompute the whole subgraph at `target_sha` and delta it in:

```python
def sync_at(writer, source, target_sha, project_name, repo_uri, ref) -> SyncStats:
    proj_id = project_id(project_name)
    writer.setup_schema(NODE_SCHEMAS)                       # indexes/constraints, before any read
    if writer.get_property("Project", {"id": proj_id}, "commit_sha") == target_sha:
        return SyncStats(commit_sha=target_sha, ...)        # already at this commit → no-op

    files = []
    for suffix in _FILE_SUFFIXES:
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES)):
            files.append(parse_source(source.read_blob(blob_sha), path))   # phase 1

    target_items = [GraphNode("Project", {"id": proj_id},
                              {"name": project_name, "repo_uri": repo_uri,
                               "commit_sha": target_sha, "ref": ref})]
    target_items.extend(resolution.resolve(files, proj_id))                # phase 2
    return _apply(writer, proj_id, target_items, target_sha, ...)
```

**`_apply(...)`** — the diff-and-write, identical across languages:

```python
def _apply(writer, proj_id, target_items, target_sha, ...) -> SyncStats:
    current_node_fps, current_edge_fps = writer.read_fingerprints(
        proj_id, [s.label for s in NODE_SCHEMAS])
    delta = compute_delta(current_node_fps, current_edge_fps, target_items)
    writer.apply_delta(delta)

    # ADR 0004: heal placeholder↔definer pairs among the linkages this ingest touched.
    from auryn.core.linkage import collect_linkages, reconcile_linkages
    reals, placeholders = collect_linkages(target_items)
    reconcile_linkages(writer, reals, placeholders)
    return SyncStats(...)   # counts derived from the delta
```

`read_fingerprints(id_prefix, labels)` reads a cheap hash of every node/edge currently under
this project's id prefix; `compute_delta` diffs the recomputed target against it; `apply_delta`
writes only the difference (upsert changed, remove gone). This is the machinery that makes an
unchanged resync ~free and a one-file change a precise small delta.

**`_git_language_sync(...)`** — the adapter the endpoint actually calls. It maps your native
`SyncStats` onto the uniform `LanguageIngestResult` so the endpoint's report doesn't
special-case each language:

```python
def _git_language_sync(writer, source, target_sha, *, repo_uri, project_name,
                       solution_name, ref, cache_root=None) -> LanguageIngestResult:
    s = sync_at(writer, source, target_sha, project_name=project_name,
                repo_uri=repo_uri, ref=ref)
    return LanguageIngestResult(nodes_added=s.nodes_added, ..., commit_sha=s.commit_sha)
```

Accept the full keyword signature even if you ignore some of it — `solution_name` and
`cache_root` are part of the shared interface (C# uses them; Go accepts and ignores them).
`source` is duck-typed: a `GitRepoCache` (git blobs) or a `LocalDirectorySource` (a plain
folder) both implement `list_files` / `read_blob`, so the same engine serves `ingest repo`
and `ingest dir` unchanged. The endpoint fetches/resolves the commit **once** and shares it,
so your `sync` never clones or resolves refs itself.

---

## 11. Wire the package into the workspace

Adding a language *does* require touching the **root `pyproject.toml`** (four spots) and
**`packages/core/pyproject.toml`** (two spots) — the per-language folder is self-contained,
but the workspace has to know the distribution exists. Copy every place `go` appears:

```toml
# pyproject.toml (root: the falkordb-auryn umbrella)
[project]
dependencies = [..., "auryn-go==0.1.0", ...]   # 1. `pip install falkordb-auryn` installs every plugin

[tool.uv.sources]
auryn-go = { workspace = true }    # 2. resolve it from the workspace, not PyPI

[dependency-groups]
dev = [..., "auryn-go", ...]       # 3. so `uv sync` installs it editable for dev/CI

[tool.coverage.run]
source = [..., "packages/go/src/auryn", ...]   # 4. include it in coverage

# packages/core/pyproject.toml
[project.optional-dependencies]
go = ["auryn-go==0.1.0"]           # 5. the custom-install extra: pip install "auryn-core[go]"
all = [..., "auryn-go==0.1.0", ...]   # 6. pip install "auryn-core[all]" installs every plugin
```

Pin to the version the other pins carry; the release workflow rewrites these pins to the
released version (the package's own version comes from git, see step 1). `tests/test_package_extras.py` fails
if a plugin is missing from the umbrella's dependencies (1), its core extra (5) or `all` (6).

Then `uv sync` and confirm discovery picks it up:

```bash
uv sync
uv run python -c "from auryn.core.registry import discover_git_languages as d; print(sorted(d()))"
# → your language should appear in the list
```

---

## 12. Tests

Add `tests/ingestors/<lang>/`, reusing the `graph` / `falkordb_connection` fixtures from
`tests/conftest.py` (they default to the embedded FalkorDB, so no server or Docker needed).
At minimum, cover:

- **Structure** — the expected nodes and edges are produced from a small fixture source
  (a package with a struct, an interface, a free function, a method, a call, a dependency).
- **Idempotency** — running twice produces no duplicates and an empty second delta. This is
  the real test of your id scheme; if it fails, your ids aren't stable.
- **Incremental** — changing one file yields a precise delta (only affected nodes/edges
  change), and moving back reverts it.
- **The contract** — callables are `Function`/`Method`, `CALLS` edges carry `resolution`,
  `Parameter` carries `type`.
- **Honesty** — an ambiguous/unresolvable reference produces *no* edge, not a wrong one.

Run with coverage and confirm your new code is exercised:

```bash
uv run pytest tests/ingestors/go -q
uv run pytest --cov=auryn --cov-report=term-missing
```

Add a small demo under `demo/<lang>-auryn/` (a `sample/` tree the docs can point at) if
you want a runnable showcase, like the other languages have.

---

## 13. Two-phase languages (`deep_sync`)

The fast tree-sitter pass is name-based and deliberately silent on anything that needs real
type information. When a real analyzer exists for your language, add it as a **second,
deeper pass** rather than complicating the fast one. Set `deep_sync` on the `GitLanguage`:

```python
register_git_language(GitLanguage(
    language=LANGUAGE, file_suffixes=FILE_SUFFIXES,
    excluded_dir_names=EXCLUDED_DIR_NAMES,
    sync=_sync,            # fast: tree-sitter, queryable immediately
    deep_sync=_deep_sync,  # deep: e.g. gopls / go-types, resolves what the fast pass couldn't
))
```

The endpoint runs `sync` first so the graph is usable quickly, then upgrades it in place
with `deep_sync` (inline or in the background). **Because both engines emit identical node
ids, the deep pass is a pure delta over the fast one** — it adds the `IMPLEMENTS` edges,
resolves interface-dispatch calls, fills inferred types, and re-tags `CALLS` edges from
`resolution="name"` to `"resolved"`/`"inferred"`. This is exactly how Python works today
(stdlib `ast` fast pass → Jedi deep pass) and how C# runs Roslyn behind the tree-sitter
fallback. Go/Rust/PHP/Julia ship the fast pass only; the deep pass is their planned future
phase. **Ship the fast pass first** — it's independently valuable and the deep pass slots in
without touching it.

---

## Appendix A: live-source ingestors (`BaseIngestor`)

If your source is a **live connection** rather than files (like the `oracle` and `sql`
ingestors reading `ALL_*` catalog views / a SQLAlchemy Inspector), you don't register a
`GitLanguage` — you subclass `BaseIngestor` and register in the **`auryn.ingestors`**
entry-point group. This makes it a `auryn run <name>` command, not part of the
file-based repo endpoint.

```python
from auryn.core.base_ingestor import BaseIngestor
from auryn.core.registry import register_ingestor

@register_ingestor("mysource")
class MySourceIngestor(BaseIngestor):
    node_schemas = NODE_SCHEMAS
    edge_schemas = EDGE_SCHEMAS

    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        # yield every node/edge from the live source, in any order
        ...

    @classmethod
    def build_cli_command(cls) -> click.Command:
        # a click.Command exposing your config (e.g. --dsn) as options.
        # Overriding this is what makes `auryn run mysource` appear —
        # cli.py discovers and attaches it automatically.
        ...
```

`BaseIngestor.run(writer)` handles the batching loop and calls `setup_schema` for you; you
only implement `extract()` (and, to be runnable from the CLI, `build_cli_command()`). Nodes
always flush before edges within a batch, so an edge never references an unwritten node.
Same idempotency rules apply — ids are your MERGE key. See `packages/sql/` for a clean
example. (A source can do *both*: the `plsql` ingestor registers a file-based `GitLanguage`
*and* a `BaseIngestor` for the live path.)

## Appendix B: enrichment passes

A capability that runs **after** the language ingestors and derives edges from the finished
graph — rather than extracting from a source — is a **graph enrichment**. It's neither a
`BaseIngestor` nor a CLI command. Register a `GraphEnrichment` in the
**`auryn.enrichments`** group:

```python
from auryn.core.registry import GraphEnrichment, register_graph_enrichment

def _run(writer, repo, target_sha) -> dict | None:
    # read what the language ingestors produced, synthesize derived nodes/edges,
    # return a small report (or None if there was nothing to do).
    ...

register_graph_enrichment(GraphEnrichment(name="myenrichment", run=_run))
```

The endpoint runs every registered enrichment after the languages — on **both** the full ingest
and every incremental `sync`. **Make it idempotent** — recompute your own layer each run
(delete-then-rebuild) so the graph stays regenerable. The reference is `packages/grpc/`'s
`grpc-contract` enrichment (the gRPC/Protobuf contract seam, shipped as `auryn-grpc`): it reads `.proto` files from the repo,
resolves nodes the other ingestors produced, and synthesizes contract-derived `IMPLEMENTS_RPC`/`CALLS_RPC` edges.
`pyframework` (in `packages/python/`) is a second example.

> **Just want a one-off hook on your own machine?** Skip the plugin/entry-point ceremony: drop a
> `run(writer, repo, target_sha)` into `~/.auryn/hooks/mything.py` (or run `auryn hooks
> new mything`) and it's auto-discovered. Entry-point registration (above) is for **shipping** a
> hook in a distribution; the local directory is for personal/ad-hoc ones. Both land in the same
> registry and are toggled with `auryn hooks`. See [authoring-a-hook.md](authoring-a-hook.md).

---

## Checklist

- [ ] `packages/<lang>/` created, copying the Go package's file set.
- [ ] `pyproject.toml` declares the `auryn.languages` entry point → your `plugin`.
- [ ] `plugin.py` registers a `GitLanguage` with a **lazy** `sync` (no parser import at module load).
- [ ] `ids.py` produces **stable, project-scoped** ids; case rules match your language.
- [ ] `schema.py` honours the [cross-ingestor contract](graph-model.md) (`Function`/`Method`, `CALLS` + `resolution`, `Parameter.type`).
- [ ] Unresolved references emit **no edge**, not a guess.
- [ ] Root `pyproject.toml` updated in all **four** spots (§11); `uv sync` succeeds; discovery lists your language.
- [ ] Tests cover structure, idempotency, incremental delta, and the contract; coverage is green.
- [ ] (Optional) `linkage_key` / placeholders for cross-repo resolution; `deep_sync` for a semantic second pass.
</content>
</invoke>
