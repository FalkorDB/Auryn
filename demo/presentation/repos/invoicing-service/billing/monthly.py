"""Invoicing service — monthly billing runs (its own repository, `invoicing-service`). Also
depends on acme-payments, so `charge` ends up with callers in TWO different services."""

import subprocess

from acme_payments.core import charge, refund


def run_monthly(customers):
    subprocess.run(["/usr/bin/notify", "billing-start"])   # ⚠ shells out — the audit will flag it
    results = []
    for customer in customers:
        results.append(charge(customer["due"]))            # cross-repo → the same shared entrypoint
    return results


def reverse(txn_id):
    return refund(txn_id)                                  # cross-repo → acme-payments
