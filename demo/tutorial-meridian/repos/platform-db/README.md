# platform-db (SQL schema)

The data model behind platform-core. Five tables:

- `accounts` — customers and their credit limits
- `orders` — placed orders (→ accounts)
- `transactions` — completed charges (→ accounts)
- `ledger_entries` — append-only money movements, written by `meridian.ledger.record`
- `audit_log` — every domain event, written by `meridian.audit.write`

Ingested so the architect can inventory what the system persists and how the audit/ledger
trail is backed.
