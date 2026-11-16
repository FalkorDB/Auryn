# auryn-orm

Language-agnostic ORM model<->database-table stitch for Auryn: a post-ingestion hook that joins a SQLAlchemy model class (in a code graph) to the physical Table/Column nodes a SQL ingestor produced (in a schema graph), by table/column name. Forms the code->database connection across two ingestors so a query can trace a function -> its ORM model -> the real table. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-orm
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
