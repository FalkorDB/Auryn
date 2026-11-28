# auryn-orm

Language-agnostic ORM model<->database-table stitch for Auryn: a post-ingestion hook that joins a SQLAlchemy model class (in a code graph) to the physical Table/Column nodes a SQL ingestor produced (in a schema graph), by table/column name. Forms the code->database connection across two ingestors so a query can trace a function -> its ORM model -> the real table; `trace_entrypoint` lists those tables as `tables_mapped`. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[orm]"               # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/orm/LICENSE).
