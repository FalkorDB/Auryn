"""The **reporting** service (its own repository, project `reporting`). Also depends on the
shared billing library -- so `billing.charge` has callers in two different repos."""

import subprocess

from billing.core import charge, refund


def daily_report(transactions):
    subprocess.run(["/usr/bin/mail", "-s", "daily"])   # stdlib call -> surfaces (origin=stdlib)
    total = 0
    for t in transactions:
        if charge(t):                                   # CROSS-REPO call into billing
            total += 1
    return total


def reverse(txn_id):
    return refund(txn_id)                               # CROSS-REPO call into billing
