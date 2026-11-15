"""The audit trail -- every domain event lands here. Writes `audit_log`."""

from __future__ import annotations

from meridian import db


def write(event: str, detail: str) -> None:
    db.execute("INSERT INTO audit_log (event, detail) VALUES (?, ?)", event, detail)
