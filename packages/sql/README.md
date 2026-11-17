# auryn-sql

Generic relational-database schema ingestor for Auryn: reflects any SQLAlchemy-supported engine (PostgreSQL, MySQL/MariaDB, SQLite, Oracle, ...) into Database/Schema/Table/Column/View/Sequence/Index/Constraint nodes plus foreign-key edges, via SQLAlchemy's dialect-agnostic Inspector. Dialect-specific DBAPI drivers are optional extras, auto-installed on demand.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[sql]"               # Auryn with only this plugin
pip install "auryn-core[sql-all]"           # + the postgres/mysql/oracle drivers
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/sql/LICENSE).
