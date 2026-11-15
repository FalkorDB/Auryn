# SQL ingestor demo

The `sql` ingestor reflects **any** relational database supported by SQLAlchemy — SQLite,
PostgreSQL, MySQL/MariaDB, Oracle, and more — into the graph via one dialect-agnostic
Inspector. There is nothing language-specific to parse: you point it at a live connection.

`build_sample.py` creates a small SQLite database (`sample.db`) with two tables, a foreign key,
a unique constraint, an index, and a view — enough to exercise every node/edge kind the
ingestor emits, with **zero external dependencies** (SQLite ships with Python).

```bash
# 1. Build the sample SQLite database (writes ./sample.db)
uv run python demo/sql-auryn/build_sample.py

# 2. Ingest it (SQLite needs no driver install)
uv run auryn run sql \
    --dsn "sqlite:///$(pwd)/demo/sql-auryn/sample.db" \
    --database-name bookstore
```

Point `--dsn` at any SQLAlchemy URL to ingest another engine — the per-dialect driver is
auto-installed on first use (or pre-install it with the matching extra):

```bash
uv run auryn run sql --dsn "postgresql+psycopg2://user:pw@host/db"       --database-name app   # auryn-sql[postgres]
uv run auryn run sql --dsn "mysql+pymysql://user:pw@host/db"             --database-name shop  # auryn-sql[mysql]
uv run auryn run sql --dsn "oracle+oracledb://user:pw@host:1521/?service_name=XEPDB1" \
                        --database-name legacy                                                    # auryn-sql[oracle]
```

Node ids are rooted at `<dialect>://<database-name>` (e.g. `sqlite://bookstore.main.books.id`),
so graphs from different engines never collide.
