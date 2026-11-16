# Changelog

All notable changes to Auryn are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`auryn --version`** — the installed version and, for a build that isn't a release, the git
  commit it was built from.

### Changed
- Package versions are derived from git instead of a fixed `0.1.0`, so reinstalling from a
  clone after a `git pull`, or after editing a package's sources, rebuilds instead of reusing
  a stale cached build.

## [0.1.0] — Initial public release

First public release of Auryn: point it at a repository and it builds a queryable
FalkorDB knowledge graph your AI agent traverses over MCP — citing file and line
instead of grepping.

### Added
- **Zero-infrastructure quickstart** — `pip install "falkordb-auryn[all,embedded]"` then
  `auryn quickstart` boots an in-process FalkorDB (no server, no Docker) and opens the viewer.
- **Sixteen languages**, auto-detected in one pass: Python (tree-sitter → Jedi deep),
  TypeScript/JavaScript, C#, Java, C, C++, CUDA, Go, Rust, PHP, Julia, Ruby, Kotlin, Swift,
  Bash, and PL/SQL; plus live SQL (any SQLAlchemy dialect) and live Oracle schemas.
- **Cross-ingestor seams** that tie separate subgraphs together: HTTP call ↔ endpoint,
  ORM model ↔ DB table, gRPC/Protobuf contract (client ↔ server), and message/event bus
  (publisher ↔ subscriber).
- **MCP server + `auryn install`** — one command registers the graph with Claude Code,
  Cursor, Windsurf, and other MCP clients.
- **Incremental resync** — after the first pass, re-ingestion is a delta; unchanged files
  are a no-op. Commit-hook and save-watcher triggers keep the graph fresh.
- **Semantic + lexical code search** over the graph.
- Umbrella distribution `falkordb-auryn` with per-language `auryn-<lang>` plugin packages,
  so you install only the languages you need.

### License
- Released under the Server Side Public License v1 (SSPLv1), matching FalkorDB.

[0.1.0]: https://github.com/FalkorDB/Auryn/releases/tag/v0.1.0
