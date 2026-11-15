# quickstart sample

A tiny payments API — the codebase `auryn quickstart` graphs when you run it with
no path. It's intentionally small so the graph builds in a second or two, but
structurally realistic:

```
app/api.py       3 FastAPI routes  -> become APIEndpoint entry points
app/billing.py   charge_card / refund   (the domain core)
app/accounts.py  Account <- PremiumAccount   (EXTENDS)
app/db.py        connect() / Connection.execute   (billing's callees)
```

It ships inside the `auryn` package so `auryn quickstart` works the same whether
you cloned the repo or `pip install`ed it. See the top-level
[README](../../../../../../README.md#quickstart-no-server-no-docker) for the full
walkthrough, including letting a coding agent query the same graph over MCP.
