# Graph model reference

What the ingestors write into FalkorDB: the shared vocabulary every language honours, the
provenance conventions that make the graph trustworthy, the id scheme, and a per-language
map of node labels and edge types. Read this before writing Cypher against the graph, or
before [adding a language](adding-a-language.md).

The graph is a real [property graph](https://falkordb.com/) queried with Cypher. Nodes have
a label (`Function`, `Class`, `Table`, …) and properties; edges have a relationship type
(`CALLS`, `EXTENDS`, `READS`, …) and properties. Every write is an idempotent `MERGE` keyed
on a node/edge id.

---

## The cross-ingestor contract

Multiple languages write into one graph, so they share a core vocabulary — that's what makes
a query like *"who calls X"* or *"what's the blast radius of changing Y"* work uniformly
regardless of source language. A new ingestor **reuses** these where they fit and only adds
language-specific labels/edges where its concepts genuinely don't map.

### Shared node labels

| Label | Meaning |
|---|---|
| `Project` / `Solution` | Root of a file-ingested codebase. `Solution` optionally wraps `Project`. |
| `Package` / `Namespace` / `Module` | The language's top grouping tier (name varies by language). |
| `File` | A source file; entities link to it via `DEFINED_IN`. |
| **`Function`** | A free/top-level callable. |
| **`Method`** | A callable bound to a type (has a receiver/owner). |
| **`Parameter`** | A parameter of a function/method; carries a **`type`** property. |
| `Class` / `Interface` / `Struct` / `Enum` / `Union` / `Record` / `TypeAlias` … | Named types (which exist depends on the language). |
| `Field` / `Property` | Data members of a type. Languages with a first-class *property* construct (C#/Swift/TS/PHP) label theirs `Property`; the rest use `Field`. A consumer wanting every data member should match both. |
| `EnumMember` | A member of an enum (every language — never `EnumCase`/`EnumVariant`). |
| `Const` | A named constant (every language — never `Constant`). |

Callables are always `Function` (free) or `Method` (bound) — never a language-specific name —
so call-graph traversal is uniform.

**The vocabulary is deliberately minimal — one name per concept, no aliases.** A language reuses the
shared labels/edges and only introduces its own when the concept *genuinely differs*. Convergences already
enforced: enum members are `EnumMember` (not `EnumCase`/`EnumVariant`), a constant is `Const`, a type alias
is `TypeAlias` (not `Typedef`), a Swift protocol / Rust trait is an `Interface` (those languages have no
separate interface), a class data member is a `Field` (not `Attribute`), and the container→type edge is
always `HAS_TYPE` (not `HAS_CLASS`/`HAS_INTERFACE`/`HAS_ENUM`/`HAS_TYPE_ALIAS`); a type nested **inside another
type** uses `HAS_NESTED_TYPE`. The few distinctions that
*earn* a separate name: **PHP `Trait`/`USES_TRAIT`** (PHP has both traits and interfaces — a `use`d mixin is
not an `implements`ed contract), and C/C++ **`DEFINES` vs `DECLARES`** (a definition vs a forward
declaration, load-bearing for incremental resync). A future step folds the remaining type labels
(`Class`/`Struct`/`Record`/…) into a single `Type` label carrying a `kind` property, extending the
`Function`/`Method` rule to types.

### Shared node properties

Every ingestor stamps these on each callable and named type (`Function`, `Method`,
`Class`/`Struct`/`Interface`/…), so a consumer can read them without knowing the language:

| Property | Meaning |
|---|---|
| `name` / `qualified_name` | The symbol's short and fully-qualified name. |
| **`source`** | The exact source text of the declaration (signature + body, including in-body comments) — the declaration's byte span. |
| **`doc`** | The documentation comment **immediately preceding** the declaration, with comment markers stripped (`//`, `#`, `--`, `/* */`, leading `*`) — the natural-language description a human wrote. `""` when there is none. Captured uniformly by `core.docstrings` (a preceding-sibling walk for tree-sitter engines; a source-text scan above the declaration for the `ast`/Roslyn engines). |
| `line_start` / `line_end` | 1-based inclusive line span (`0` when a source position isn't available). |

`source` + `doc` are the two halves of the text an embedding is built from: `doc` is the
intent, `source` is the implementation. Together they make a symbol semantically searchable
without re-reading the file.

### Shared edge types

| Edge | From → To | Notes |
|---|---|---|
| `HAS_*` | container → contained | Structural backbone: `HAS_PACKAGE`, `HAS_TYPE`, `HAS_METHOD`, `HAS_PARAMETER`, `HAS_FIELD`, … |
| `DEFINED_IN` | type → `File` | Provenance of where a type is declared. |
| **`CALLS`** | callable → callable | **Always carries a `resolution` property** (see below). |
| **`EXTENDS`** | class→class, interface→interface | Inheritance. |
| **`IMPLEMENTS`** | class → interface | Interface conformance. |
| **`DEPENDS_ON`** | member → type | A type reference (parameter/return/field type, construction site). |
| `READS` / `WRITES` | code → `Table` | Data access (`WRITES` carries an `operation`: `INSERT`/`UPDATE`/`DELETE`). |
| `REFERENCES` | column → column | Foreign keys. |

### Git provenance labels (opt-in)

When `[git_info].enabled = true`, verified public GitHub repositories add a repo-scoped provenance
subgraph. IDs start with `github://<owner>/<repo>` and do not depend on the checked-out branch.
The current collector writes `GitCheckout` (a local source mapping), `GitRepository`, `GitCommit`,
`GitUser`, `GitBranch`, `GitTag`, `GitIssue`, `GitPullRequest`, `GitRelease`, `GitCheckSuite`,
`GitCheckRun`, and `GitHubApp` nodes. A commit links directly to matching code `File` nodes through
`CHANGED`; paths without a code `File` do not get a synthetic node, while the commit's `changed_files`
JSON property preserves their paths and change types (`A`, `M`, or `D`). Renames appear as a deletion
at the old path and an addition at the new path. Diffs compare against the first parent (root
commits compare against an empty tree). Check suites and runs come from GitHub's provider-neutral
Checks API, so results published by GitHub Actions and other GitHub Apps use the same model. The
first remote sync fetches checks for **every ingested commit** (made affordable by batched GraphQL);
later refreshes add checks for new commits plus current ref tips and PR head commits. (A very large
history therefore pays a one-time Checks cost on the first sync.) Edges include `HAS_COMMIT`, `AUTHORED`, `COMMITTED`, `PARENT_COMMIT`,
`HAS_BRANCH`, `POINTS_TO_COMMIT`, `HAS_TAG`, `TAGS_COMMIT`, `USES_REPOSITORY`, `HAS_ISSUE`,
`HAS_PULL_REQUEST`, `HAS_RELEASE`, `OPENED_BY`, `CREATED_BY`, `REFERENCES_ISSUE`, `CLOSES_ISSUE`,
`MERGED_AS`, `CHANGED`, `HAS_CHECK_SUITE`, `HAS_CHECK_RUN`, and `PROVIDED_BY`.

Issue and pull-request authors link through `OPENED_BY`; release authors use `CREATED_BY`. A PR links
to same-repository issues mentioned in its title or body through `REFERENCES_ISSUE`. `CLOSES_ISSUE`
is emitted only when the PR is merged into the repository's verified default branch and its
description uses GitHub closing syntax (`close`, `fix`, or `resolve`). Cross-repository issue
references are currently ignored.

The collector retains public contributor names, commit messages, and issue/PR/release text. It
verifies repository visibility is public before reading any local history or GitHub contents. All
reads (visibility, metadata, Checks) use the local `gh` token when one is present — only to raise the
rate limit; they read exclusively public data — and the anonymous public API otherwise. A remote
refresh runs at most daily during ordinary ingestion; `auryn git-info sync` forces one.

> **Golden rule for a new ingestor: emit no edge rather than a wrong one.** The fast
> tree-sitter pass is name-based; when a reference is ambiguous or unresolvable, it drops the
> edge instead of guessing. Every present edge is therefore trustworthy. Relationships that
> genuinely need real type information (structural interface satisfaction, interface-dispatch
> calls) are left to an optional [deep pass](adding-a-language.md#13-two-phase-languages-deep_sync).

---

## Provenance: how to know what to trust

The graph is designed so a consumer can tell a fact from an inference. Two conventions carry
this:

- **`resolution` on `CALLS` (and other resolved edges)** — how the edge was derived:
  - `"name"` — a syntactic/name-based match from the fast pass.
  - `"resolved"` — resolved by a real semantic engine (Roslyn DI resolution, Jedi, …).
  - `"inferred"` — derived from inferred (not annotated) types.
  - `"external"` — a [cross-repo placeholder](#cross-repo-linkage-adr-0004) not yet resolved
    to a real definition.
- **`resolved: false` + `origin`** on placeholder nodes — an unresolved external symbol,
  with `origin` distinguishing `"stdlib"` (a terminal external) from `"external"` (a symbol a
  sibling repo could define).

An agent querying the graph is instructed to trust `resolved` over `inferred` — and to say
which it used. Answers cite the node `id` and file path the graph returns, not a guess.

---

## The id scheme

Every node is keyed by a string `id` that is its identity across syncs — the `MERGE` key.
Ids are **stable** (the same source symbol always produces the same id) and **hierarchical**,
rooted at a per-language, per-project scheme:

```
go://<project>/package/<pkg>.func.<name>(<param-types>)
csharp://<project>/<namespace>/<type>/<method>(<sig>)
plsql://<project>/…            oracle://<db>/<schema>/…            <dialect>://<db>/…
```

Rules the ids obey (and a new ingestor must too):

- Ids embed **names and structural position, never resolved or inferred types** — a
  parameter's *written* type text may appear in a signature; an inferred type never does.
  This keeps a fast pass and a deep pass keying the same symbol identically, so they're
  interchangeable in one graph.
- Ids are **project-scoped**, so two repos' same-named symbols stay distinct in a shared
  graph.
- Generic **arity** is encoded so `Container` and `Container[T]` can't collide.
- **Case** follows the language: Go ids preserve case (export is encoded in case — `Foo`
  and `foo` must not collide); Java ids may lowercase.

Every read tool takes a node's `id`, never its display name — `locate` is the way to turn a
name into an id.

---

## Cross-repo linkage (ADR 0004)

A call from one repo into a symbol defined in another (or in the stdlib) can't be resolved by
a single-repo pass — but it can often be *named*. Rather than dropping it, the ingestors emit
a **placeholder node** reached by a normal `CALLS` edge, so that ingesting the defining repo
later *heals* it into a real edge. The join is a project-independent **linkage key**:

- An exported symbol carries a `linkage_key` property — how an outside consumer names it
  (`package.Func`, `pkg.Type.Method`), **name-based only**.
- An unresolved-but-nameable call emits a placeholder whose `id` equals that same key (e.g.
  `extern://go/fmt.Println`), with `resolved: false` and an `origin`.
- Reconciliation folds `placeholder.id == real.linkage_key` pairs when both repos live in one
  graph.

The upshot: system/stdlib symbols surface as single shared nodes (one `fork` node, every
caller inbound), and co-locating the repos of one system in a single graph resolves their
cross-repo calls into real edges. Full rationale in
[`adr/0004-cross-repo-symbol-resolution.md`](adr/0004-cross-repo-symbol-resolution.md); the
cases that don't yet heal (and why) are catalogued in the Python ingestor's
[`LIMITATIONS.md`](../packages/python/src/auryn/ingestors/python/LIMITATIONS.md).

---

## Per-language maps

The [README](../README.md) has the full prose for each; this is the condensed schema. All
share the backbone above.

### Python, TypeScript/JavaScript, Java, Kotlin, C, C++, Go, Rust, PHP, Julia

Fast tree-sitter ingestors. Each models its language faithfully rather than forcing one
shape — but all use `Function`/`Method` for callables, `CALLS` with `resolution`, and
`Parameter.type`. Highlights of the language-specific parts:

- **Go** — go.mod aware: a `Module` tier (one per `go.mod`) over `Package`s keyed by **directory**
  (not the bare `package` clause name, so a workspace's many identically-named packages — every
  `package main`, repeated `util`/`handlers` — stay distinct), `Project`-`[:HAS_MODULE]`→`Module`-
  `[:HAS_PACKAGE]`→`Package`. A package carries its canonical `import_path` (module path + dir),
  which is what cross-repo linkage keys use (`extern://go/<import-path>.<Name>`), so healing matches
  the path a consumer actually imports. `Method` = func with a receiver (`HAS_METHOD`); `EMBEDS` for
  struct/interface embedding; `Module`-`[:DEPENDS_ON]`→`Module` from `require`. `IMPLEMENTS`
  (structural) is deferred to a deep pass.
- **Rust** — Cargo-aware: a `Package` tier (one per `Cargo.toml`) over a `Crate` tier (one per
  build target — lib/bin/test/example/bench), `Project`-`[:HAS_PACKAGE]`→`Package`-`[:HAS_TARGET]`→
  `Crate`. The **crate is the scope root** (crate-derived from the file's `src/` layout), so two
  crates in a workspace keep identically-named items distinct. A `Module` tier hangs under each
  crate; methods live in impl blocks re-attached to their type; `IMPLEMENTS` (impl Trait for T) and
  `EXTENDS` (supertraits); `HAS_VARIANT` for enums; `Package`-`[:DEPENDS_ON]`→`Package` for a
  crate-to-crate dependency (from `[dependencies]`).
- **PHP** — Java's structural sibling: `Namespace` → `Class`/`Interface`/`Trait`/`Enum`,
  `EXTENDS`/`IMPLEMENTS`/`USES_TRAIT`.
- **Julia** — multiple dispatch: one generic `Function`, many typed `Method`s;
  `DISPATCHES_ON` records the arg types a method dispatches on; `EXTENDS` is the `<:` subtype
  relation.
- **Java / Kotlin** — Maven/Gradle module-aware: a `Module` tier (one per **subproject**) over the
  `Package` prefix chain, `Project`-`[:HAS_MODULE]`→`Module`-`[:HAS_PACKAGE]`→`Package`. The
  subproject is the scope root (path-derived from the dir before `src/{main,test}/{java,kotlin}/`),
  so two modules that both declare `com.acme.util.Helper` keep their types/packages distinct, and a
  reference resolves within its own module. The tier appears **only when there are subprojects**:
  a root-level file (no `<module>/src/…` dir) hangs off the `Project` directly, with no `Module` node
  — so a root file and a subproject literally named after the project can't collide. Unlike
  Go/Rust/Swift, the module is NOT an import target (a consumer imports a Java/Kotlin type by its
  **FQCN**, never the Gradle module name), so the `Module` carries no linkage key and heals nothing —
  cross-repo linkage stays FQCN-based (`extern://java/<fqcn>#<member>`). Kotlin adds top-level
  `Function`s/`Field`s (`HAS_FUNCTION`).
- **C# / C / C++** — class/namespace models; `EXTENDS`/`IMPLEMENTS`/`DEPENDS_ON`;
  C uses `DEFINES`/`DECLARES`/`INCLUDES`/`REFERENCES`.
- **Python** — two-phase: stdlib `ast` fast pass, then a Jedi deep pass (inferred types,
  receiver-resolved calls). A `pyframework` enrichment adds route endpoints.

### C# (the fullest model)

```
(Solution)-[:HAS_PROJECT]->(Project)-[:HAS_NAMESPACE]->(Namespace)-[:HAS_TYPE]->(Class|Interface|Struct|Enum|Record|Delegate|DTO)
(Class|Record)-[:EXTENDS]->(Class|Record)      (Class|Struct|Record|DTO)-[:IMPLEMENTS]->(Interface)
(Method)-[:CALLS]->(Method)                    (Method)-[:EXPOSES_ENDPOINT]->(APIEndpoint)-[:ACCEPTS|RETURNS]->(DTO)
(Method)-[:DEPENDS_ON]->(...)                  (DIRegistration)-[:REGISTERS]->(Interface|Class)  # Roslyn
(Interface)-[:RESOLVES_TO]->(Class)            # Roslyn DI resolution
# Kendo/ASP.NET MVC view tier + message-routing seam (see README):
(View)-[:CONTAINS]->(Grid)   (Grid)-[:CALLS_API|DISPLAYS_DATA]->(Method)   (View)-[:SUBMITS_TO]->(Method)
(Method)-[:DISPATCHES_TO]->(Procedure|Function)   # C#→PL/SQL seam, routing-derived
```

`DTO` and `APIEndpoint` are **classified**, not keywords: a controller method with an
Http-verb attribute becomes an `APIEndpoint`; a class used as an endpoint's param/return type
(or matching a name suffix) is relabeled `DTO`.

### SQL (schema reflection) and PL/SQL

```
(Database)-[:HAS_SCHEMA]->(Schema)-[:HAS_TABLE]->(Table)-[:HAS_COLUMN]->(Column)
(Column)-[:REFERENCES]->(Column)                 # foreign keys
(Table)-[:HAS_INDEX]->(Index)-[:INDEXES]->(Column)
(Table)-[:HAS_CONSTRAINT]->(Constraint)-[:CONSTRAINS]->(Column)
(Schema)-[:HAS_VIEW]->(View)   (Schema)-[:HAS_SEQUENCE]->(Sequence)

# Oracle / PL/SQL add the code layer, linking into the tables above:
(Schema|Project)-[:HAS_PACKAGE]->(Package)-[:HAS_PROCEDURE|HAS_FUNCTION]->(Procedure|Function)
(Procedure|Function)-[:HAS_PARAMETER]->(Parameter)     (Table)-[:HAS_TRIGGER]->(Trigger)
(Procedure|Function|Package|Trigger)-[:CALLS]->(Procedure|Function|Package)
(Procedure|Function|Package|Trigger)-[:READS|WRITES]->(Table)   # WRITES carries operation
(...)-[:DEPENDS_ON]->(Table|View|Sequence|Procedure|Function|Package|Type)
```

The live `sql`/`oracle` ingestors root at a real database (`<dialect>://` / `oracle://`); the
file-based `plsql` ingestor roots at a `Project` (`plsql://`) since source files carry no
schema. Within a file project, PL/SQL code links to the *actual* DDL tables it parses (a
name-only stub `Table` appears only for a table referenced by code but never defined by DDL).

---

## Inspecting a live graph

Rather than trusting this page, ask the graph itself:

```bash
uv run auryn view --falkordb-port 6379      # schema overview per label/edge in the UI
```

Or via the MCP server / Cypher (`get_schema`, or `run_cypher`):

```cypher
CALL db.labels()               // every node label present
CALL db.relationshipTypes()    // every edge type present
MATCH (n) RETURN labels(n)[0] AS label, count(*) ORDER BY count(*) DESC
```
</content>
