"""Build a small SQLite database next to this script for the SQL-ingestor demo.

Two tables (authors, books) with a foreign key, a unique constraint, an index, and a view --
enough to exercise every node/edge kind the generic SQL ingestor emits, using only Python's
stdlib sqlite3 (no server, no driver install). Run it, then:

    uv run auryn run sql --dsn "sqlite:///$(pwd)/demo/sql-auryn/sample.db" \\
                            --database-name bookstore
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("sample.db")

DDL = """
CREATE TABLE authors (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    email TEXT NOT NULL,
    CONSTRAINT uq_authors_email UNIQUE (email)
);
CREATE INDEX ix_authors_name ON authors (name);

CREATE TABLE books (
    id        INTEGER PRIMARY KEY,
    title     TEXT NOT NULL,
    author_id INTEGER NOT NULL
                  REFERENCES authors (id) ON DELETE CASCADE,
    CONSTRAINT ck_books_title_not_empty CHECK (title <> '')
);

CREATE VIEW author_names AS SELECT id, name FROM authors;
"""


def main() -> None:
    DB_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL)
        conn.commit()
    finally:
        conn.close()
    print(f"Wrote {DB_PATH}")


if __name__ == "__main__":
    main()
