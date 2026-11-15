"""Thin database access layer over the platform-db schema.

`connect` reads the DSN from the environment; `query`/`execute` are the two chokepoints
every other module goes through to touch the `platform-db` tables.
"""

from __future__ import annotations

import os
import sqlite3


def connect() -> sqlite3.Connection:
    dsn = os.getenv("DATABASE_URL", "meridian.db")   # stdlib call -> surfaces in the audit
    return sqlite3.connect(dsn)


def query(sql: str, *params) -> list[dict]:
    conn = connect()
    try:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def execute(sql: str, *params) -> None:
    conn = connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
