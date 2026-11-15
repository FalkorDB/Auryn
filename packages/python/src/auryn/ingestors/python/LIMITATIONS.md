# Python ingestor — known limitations

Behaviours of the **Python** ingestor's resolution that are currently incomplete or intentionally
deferred. Each has a **tracking test** (most are `xfail`, so they run on every CI pass and will
*fail loudly the day the behaviour is fixed* — a prompt to promote the test and delete the entry
here). Add new limitations as they're discovered; remove them as they're closed.

Companion to [`GRAPH_MODEL.md`](GRAPH_MODEL.md) (the graph this ingestor produces) and
[ADR 0004](../../../../../../docs/adr/0004-cross-repo-symbol-resolution.md) (the cross-repo design).

---

## Cross-repo symbol resolution

When several repos share one graph, an outbound reference to a symbol defined in another repo is
emitted as a `resolved:false` placeholder keyed by a **linkage key**
(`extern://python/<module>::<name>`). Ingesting the defining repo *heals* the reference:
reconciliation matches the placeholder's key to the real definition's `linkage_key`, repoints the
edge, and deletes the placeholder.

Because the key is derived from **how the consumer names the symbol** and **which real node carries a
matching `linkage_key`**, healing fails wherever those two strings can't be made to agree. The cases
below don't heal. In every one the reference is *safe but incomplete*: the graph under-reports a
cross-repo edge rather than inventing a wrong one — so for a reachability/impact audit, treat "no
path found" **across a repo boundary** with these caveats in mind.

| # | Case | Effect | Tracking test |
|---|------|--------|---------------|
| 1 | **Re-export through `__init__.py`** | not detected as a match | `tests/e2e/test_cross_repo_entity_kinds.py::test_reexport_via_init_is_a_known_gap` |
| 2 | **Cross-repo class inheritance** | no node *or* edge emitted | `tests/e2e/test_cross_repo_entity_kinds.py::test_cross_repo_inheritance_is_a_known_gap` |
| 3 | **Fully-qualified call** `pkg.sub.fn()` | reference undetected | `tests/e2e/test_cross_repo_import_forms.py::test_fully_qualified_call_is_a_known_gap` |
| 4 | **Star import** `from x import *` | reference undetected | `tests/e2e/test_cross_repo_entity_kinds.py::test_star_import_is_a_known_gap` |
| 5 | **Receiver-typed method call** whose type is in another repo | reference undetected | — (inherent; see below) |

### 1. Re-export through `__init__.py` (highest impact)
A package exposes a symbol on its public surface — `billing/__init__.py` does
`from billing.core import charge` — so consumers write `from billing import charge`. The consumer
mints linkage `billing::charge`, but the real definition is keyed `billing.core::charge`. The strings
differ, so nothing heals. This is the standard public-API pattern, so it's the most likely to bite in
practice.
*Fix (deferred, non-trivial):* let a producer's re-export contribute an **alias linkage key**
(`billing::charge`) onto the real node — i.e. support multiple linkage keys per node, plus a reconcile
change to match any of them.

### 2. Cross-repo class inheritance
`class Wallet(Account)` where `Account` is defined in another repo. Unlike a cross-repo *call*, the
ingestor records an unresolved base only as a **string** in the class's `unresolved_bases` property —
it emits **no placeholder `Class` and no `EXTENDS`/`IMPLEMENTS` edge** — so there is nothing for
reconciliation to upgrade, even once both repos are present.
*Fix (deferred, wide blast radius):* emit a placeholder `Class` + `EXTENDS` edge for an unresolved
base (mirroring the CALLS-placeholder path; reconcile already heals `EXTENDS`). Not done because it
would also fire on every stdlib/third-party base (`Exception`, `ABC`, `Protocol`, …), adding a
placeholder node + edge for each across the whole graph and shifting many golden-test baselines.

### 3. Fully-qualified call `pkg.sub.fn()`
`import billing.core; billing.core.charge()`. The `ast` call extractor records only a single-`Name`
receiver, so the `billing.core` receiver chain is dropped; and `import billing.core` binds the local
name `billing`, not `billing.core`. *Fix:* multi-level receiver capture in the parser + matching in
call resolution. (Note the sibling forms `import billing.core as bc; bc.charge()` and
`from billing import core; core.charge()` **do** heal — see the passing matrix in
`tests/e2e/test_cross_repo_import_forms.py`.)

### 4. Star import `from x import *`
The parser skips `*` imports, so imported names are unbound and no reference is recorded. Resolving
`*` statically needs the producer's exported names, which aren't present when the consumer is
ingested. Low priority (discouraged pattern).

### 5. Receiver-typed method call, type defined in another repo
`def run(c: Connection): c.execute(...)` where `Connection` lives in another repo. Neither engine can
bind `c.execute` without the type's definition, which is absent at consumer-ingest time. Inherent to
the "producer absent" condition; no cheap fix.

### Reconciliation caveat (applies to all languages)
The reconcile near-miss **diagnostic** — the warning that flags an unresolved reference matching a
definition that differs only by a module prefix — only fires when the placeholder and the real
definition are touched in a **single** ingest (e.g. a whole tree indexed as one project). A producer
ingested in a separate pass from its consumer isn't compared. Lives in `core/linkage.py`; unit test
`tests/core/test_linkage_diagnostics.py`.

---

## Recently addressed (for context; no longer limitations)

- **Package-root pollution of the linkage key** — a `src/` layout, a hyphenated distribution dir, or
  any wrapper directory used to leak into the dotted module path and break every cross-repo match.
  Fixed via import-root detection (`ids.dotted_module_path`). Tests: `tests/ingestors/python/test_ids.py`,
  `tests/e2e/test_cross_repo_resolution.py`.
- **Import-call forms** — `from m import fn`, `from m import fn as p`, `import m as x; x.fn()`, and
  `from pkg import sub; sub.fn()` all heal. Tests: `tests/e2e/test_cross_repo_import_forms.py`.

## See also (not Python-specific)
- **Cross-language** resolution (service → DB, Python → C, …) — not implemented; a later phase in
  [ADR 0004](../../../../../../docs/adr/0004-cross-repo-symbol-resolution.md) (§Interactions / phasing item 5).
- **TypeScript** cross-repo *symbol* healing — limited by module identity (npm package name vs
  repo-relative path); belongs to the TypeScript ingestor (ADR 0004 item 4).
