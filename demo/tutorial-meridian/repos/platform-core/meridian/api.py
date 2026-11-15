"""HTTP layer for platform-core.

FastAPI routes are the platform's public contract to the rest of the company: the
api-gateway (C# BFF) reaches this service over HTTP. The route decorators become
`APIEndpoint` nodes in the code graph (via the python-routes enrichment), so each handler
is a first-class entry point you can trace end to end -- and an inbound HTTP call from
another repo heals onto the matching endpoint here.
"""

from __future__ import annotations

from fastapi import FastAPI

from meridian import billing, orders, reports

app = FastAPI(title="meridian-platform-core")


@app.post("/charge")
def charge_endpoint(account_id: int, amount: float, currency: str = "USD"):
    """Charge an account. The single entry point every checkout path funnels into."""
    return {"result": billing.charge(account_id, amount, currency)}


@app.post("/refund")
def refund_endpoint(txn_id: str):
    return {"result": billing.refund(txn_id)}


@app.post("/promo")
def promo_endpoint(code: str, amount: float):
    """Apply a promotion, then charge the discounted amount."""
    return {"result": billing.apply_promo(code, amount)}


@app.get("/orders/{id}")
def get_order_endpoint(id: str):
    return {"order": orders.get_order(id)}


@app.get("/reports/daily")
def daily_report_endpoint():
    return {"report": reports.daily_report()}
