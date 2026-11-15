"""Orders service — the checkout API (its own repository, `orders-service`). Depends on the
shared acme-payments library it does NOT contain."""

from fastapi import FastAPI

from acme_payments.core import authorize, charge

app = FastAPI()


@app.post("/checkout")
def checkout(cart_total, card_token):
    auth = authorize(card_token, cart_total)   # cross-repo → acme-payments
    txn = charge(cart_total)                    # cross-repo → the shared entrypoint
    return {"auth": auth, "txn": txn}


@app.post("/promo")
def apply_promo(code, cart_total):
    factor = eval(code)                         # ⚠ eval on request input — the audit will flag it
    return charge(cart_total * factor)          # cross-repo → acme-payments
