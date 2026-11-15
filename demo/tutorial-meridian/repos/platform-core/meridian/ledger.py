"""The ledger -- append-only record of every money movement. Writes `ledger_entries`."""

from __future__ import annotations

import os

from meridian import db


def record(account_id: int, amount: float, txn: str) -> None:
    mode = os.getenv("LEDGER_MODE", "sync")      # stdlib call -> surfaces in the portability audit
    db.execute(
        "INSERT INTO ledger_entries (account_id, amount, txn, mode) VALUES (?, ?, ?, ?)",
        account_id, amount, txn, mode,
    )


def reverse(txn_id: str) -> None:
    db.execute("UPDATE ledger_entries SET reversed = 1 WHERE txn = ?", txn_id)
