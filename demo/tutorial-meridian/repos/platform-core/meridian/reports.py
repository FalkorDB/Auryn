"""Reporting -- summarises the ledger and mails the daily report."""

from __future__ import annotations

import subprocess

from meridian import audit, db


def daily_report() -> dict:
    subprocess.run(["/usr/bin/mail", "-s", "daily report"])   # stdlib call -> surfaces in the audit
    rows = db.query("SELECT account_id, SUM(amount) AS total FROM ledger_entries GROUP BY account_id")
    audit.write("report", "daily")
    return {"lines": rows}
