# Documentation

Docs for Auryn. Start with the [project README](../README.md) for install
and a first graph; come here to go deeper.

## Guides

| Doc | Read it to… |
|---|---|
| [architecture.md](architecture.md) | understand how the codebase fits together — plugin discovery, the ingest pipeline, delta-sync, storage backends, and the MCP/viewer layers. |
| [graph-model.md](graph-model.md) | understand (and query) the graph — the shared node/edge vocabulary, provenance conventions, the id scheme, cross-repo linkage, and per-language schemas. |
| [cli.md](cli.md) | look up any command — the consolidated `auryn` CLI reference. |
| [adding-a-language.md](adding-a-language.md) | **contribute a new language ingestor** — a full, line-by-line walkthrough using the Go plugin as the reference. |

See also [CONTRIBUTING.md](../CONTRIBUTING.md) for the contribution quick-map and design
principles.

## Architecture Decision Records

The `adr/` folder records the deeper design decisions and their rationale:

| ADR | Topic |
|---|---|
| [0001](adr/0001-onboarding-and-server-lifecycle.md) | Onboarding & the on-demand local FalkorDB server lifecycle. |
| [0002](adr/0002-incremental-resync.md) | Incremental resync — the recompute-and-diff engine that makes re-ingestion cheap. |
| [0003](adr/0003-repo-lifecycle-and-resync-triggers.md) | Repo lifecycle: the ingested-repo registry, git hooks, and the watch daemon. |
| [0004](adr/0004-cross-repo-symbol-resolution.md) | Cross-repo symbol resolution — placeholder nodes and linkage keys. |
| [0005](adr/0005-delta-sync-all-languages.md) | Delta-sync across all languages — commit-driven incremental updates. |
| [0006](adr/0006-http-call-endpoint-seam.md) | HTTP call↔endpoint seam — the route-keyed, language-agnostic stitch. |
| [0007](adr/0007-deployment-infrastructure-tier.md) | Deployment & infrastructure tier — the image-keyed contract across Docker/Compose/k8s/Helm/Terraform. |

## Website content

`website/` is a content brief for a marketing site (product/developer pages, architecture
diagram copy, screenshots manifest). It's aimed at a site designer, not at using the tool —
the guides above are the developer docs.

## Learning by example

- `demo/` has a runnable sample per language/ingestor (e.g. `demo/go-auryn/`,
  `demo/chinook/` for SQL, `demo/csharp-git/` for git-tracked resync,
  `demo/docker-deploy/` for the Dockerfile ingestor + deployment tier).
- `benchmarks/README.md` documents the ingest, freshness, and agent-adoption benchmark suites.
</content>
