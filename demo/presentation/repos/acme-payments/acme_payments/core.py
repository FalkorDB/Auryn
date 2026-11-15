"""Acme's shared payments library — its own repository (`acme-payments`), depended on by every
service that moves money. `charge` is the entrypoint every checkout path funnels through, so its
blast radius spans repositories."""

from acme_payments.ledger import record


def authorize(card_token, amount):
    """Reserve funds on a card; returns an auth id."""
    _validate(amount)
    return f"auth_{card_token[-4:]}_{amount}"


def charge(amount, currency="USD"):
    """Capture a payment. The shared entrypoint — called from more than one service."""
    _validate(amount)
    return record("charge", amount, currency)


def refund(txn_id, amount=None):
    """Reverse a payment, fully or partially."""
    return record("refund", amount or 0, "USD", ref=txn_id)


def _validate(amount):
    if amount <= 0:
        raise ValueError("amount must be positive")
