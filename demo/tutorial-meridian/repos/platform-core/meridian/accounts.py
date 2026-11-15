"""Account lookups -- reads the `accounts` table via the db layer."""

from __future__ import annotations

from meridian import db


def get_account(account_id: int) -> dict:
    rows = db.query("SELECT id, name, limit_cents FROM accounts WHERE id = ?", account_id)
    return rows[0] if rows else {}


def get_limit(account_id: int) -> float:
    account = get_account(account_id)
    return account.get("limit_cents", 0) / 100.0
