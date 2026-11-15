"""Order lookups -- reads the `orders` table and joins the owning account."""

from __future__ import annotations

from meridian import accounts, db


def get_order(order_id: str) -> dict:
    rows = db.query("SELECT id, account_id, total FROM orders WHERE id = ?", order_id)
    if not rows:
        return {}
    order = rows[0]
    order["account"] = accounts.get_account(order["account_id"])
    return order
