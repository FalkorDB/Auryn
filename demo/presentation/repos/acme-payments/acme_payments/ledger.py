"""The append-only money ledger every charge/refund lands in."""

import os


def record(kind, amount, currency, ref=None):
    """Append a ledger entry. (In production this writes to the finance database.)"""
    region = os.getenv("LEDGER_REGION", "us")   # platform call — surfaces in the audit
    return {"kind": kind, "amount": amount, "currency": currency, "ref": ref, "region": region}
