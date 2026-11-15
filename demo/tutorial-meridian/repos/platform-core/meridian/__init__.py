"""Meridian platform-core: the shared domain service.

Owns billing, accounts, the ledger and the audit trail. Every money-moving path in the
company funnels through `billing.charge`, so this package is where the system's blast
radius concentrates. Exposed to the rest of the company as a small HTTP service (api.py).
"""

__version__ = "1.4.0"
