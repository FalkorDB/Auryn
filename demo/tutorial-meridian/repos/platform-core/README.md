# platform-core (Python / FastAPI)

The shared domain service every other repo depends on. Owns:

- **billing** — `charge` (the shared funnel), `refund`, `apply_promo`, `authorize`
- **accounts** — account + credit-limit lookups
- **ledger** — append-only money movements (`ledger_entries`)
- **audit** — the audit trail (`audit_log`)
- **orders** — order lookups
- **db** — the access layer over `platform-db`

Exposed over HTTP (`meridian/api.py`): `POST /charge`, `POST /refund`, `POST /promo`,
`GET /orders/{id}`, `GET /reports/daily`. The api-gateway calls these routes.
