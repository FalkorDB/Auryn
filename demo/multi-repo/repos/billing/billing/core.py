"""The shared **billing** library. Published as a package and reused by several services;
its own repository, ingested as project `billing`."""


def charge(amount, currency="USD"):
    """Charge a customer. Returns a transaction id. Called by more than one service --
    exactly the kind of shared entrypoint whose blast radius spans repositories."""
    _audit(amount)
    return f"txn:{currency}:{amount}"


def refund(txn_id):
    """Reverse a prior transaction."""
    return {"refunded": txn_id}


def _audit(amount):
    return amount > 0
