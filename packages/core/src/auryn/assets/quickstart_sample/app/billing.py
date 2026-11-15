"""Billing service: the domain core. `charge_card` is the function the quickstart
agent example asks about -- it has both callers (the /charges endpoint) and callees
(accounts + the db layer), so it makes a good who-calls / blast-radius example.
"""

from app.accounts import Account, get_account
from app.db import connect


def charge_card(account_id: int, amount_cents: int) -> str:
    """Authorize and record a charge against an account."""
    account = get_account(account_id)
    _guard_limit(account, amount_cents)
    conn = connect()
    conn.execute(
        "INSERT INTO charges (account_id, amount_cents) VALUES (?, ?)",
        (account.id, amount_cents),
    )
    return f"charged {amount_cents} to account {account.id}"


def refund(charge_id: int) -> str:
    """Reverse a prior charge."""
    conn = connect()
    conn.execute("UPDATE charges SET refunded = 1 WHERE id = ?", (charge_id,))
    return f"refunded charge {charge_id}"


def _guard_limit(account: Account, amount_cents: int) -> None:
    if amount_cents > account.limit_cents:
        raise ValueError(f"amount {amount_cents} exceeds limit {account.limit_cents}")
