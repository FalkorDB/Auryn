"""Billing domain -- the heart of the platform.

`charge` is the shared entry point every money-moving path in the company funnels through
(checkout, promotions, and re-orders all reach it), so its blast radius spans repositories.
Change its signature and every caller across every service is affected.
"""

from __future__ import annotations

from meridian import accounts, audit, ledger


def charge(account_id: int, amount: float, currency: str = "USD") -> str:
    """Charge an account. Returns a transaction id.

    The shared funnel: authorizes against the account limit, writes the ledger entry, and
    records an audit event. Called by more than one service -- exactly the kind of shared
    entry point whose blast radius spans repositories.
    """
    authorize(account_id, amount)
    txn = f"txn:{currency}:{amount}"
    ledger.record(account_id, amount, txn)
    audit.write("charge", f"{account_id}:{amount}{currency}")
    return txn


def refund(txn_id: str) -> dict:
    """Reverse a prior transaction."""
    ledger.reverse(txn_id)
    audit.write("refund", txn_id)
    return {"refunded": txn_id}


def apply_promo(code: str, amount: float) -> str:
    """Apply a promo code, then charge the discounted amount.

    NOTE: `code` is evaluated with the builtin `eval` -- arbitrary user input reaching
    `eval` is a remote-code-execution risk. This is the finding the audit is meant to
    surface.
    """
    factor = eval(code)                       # audit red flag: eval on caller-supplied input
    return charge(0, amount * factor)


def authorize(account_id: int, amount: float) -> bool:
    """Authorize a charge against the account's credit limit."""
    limit = accounts.get_limit(account_id)
    audit.write("authorize", f"{account_id}:{amount}<={limit}")
    return amount <= limit
