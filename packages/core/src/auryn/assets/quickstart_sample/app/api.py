"""HTTP layer: three FastAPI routes that delegate into the billing/accounts services.

The route decorators become `APIEndpoint` nodes in the graph (via the python-routes
enrichment), so each handler is a first-class entry point you can `trace_entrypoint`
end to end.
"""

from fastapi import FastAPI

from app.accounts import get_account
from app.billing import charge_card, refund

app = FastAPI()


@app.post("/charges")
def create_charge(account_id: int, amount_cents: int):
    """Charge a card. Entry point whose call-closure reaches charge_card -> db."""
    return {"result": charge_card(account_id, amount_cents)}


@app.post("/refunds")
def create_refund(charge_id: int):
    return {"result": refund(charge_id)}


@app.get("/accounts/{account_id}")
def read_account(account_id: int):
    account = get_account(account_id)
    return {"id": account.id, "limit_cents": account.limit_cents}
