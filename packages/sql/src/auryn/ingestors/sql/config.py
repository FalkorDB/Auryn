"""Configuration for the SQL ingestor."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SQLSourceConfig:
    dsn: str
    """A standard SQLAlchemy connection URL, e.g. postgresql+psycopg2://..., sqlite:///..."""

    database_name: str
    """Logical name for this source, used to build the Database node's id."""

    schemas: tuple[str, ...] | None = None
    """Which schemas/namespaces to ingest. None means "just the connection's default
    schema" -- important on MySQL, where the dialect reports every database on the
    server as a "schema", not just the one named in the DSN."""
