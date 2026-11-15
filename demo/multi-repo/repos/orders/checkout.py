"""The **orders** web service (its own repository, project `orders`). It depends on the
shared billing library and on the platform."""

import os

from billing.core import charge


def place_order(cart_total):
    region = os.getenv("REGION", "us")   # stdlib call -> surfaces (origin=stdlib)
    txn = charge(cart_total)             # CROSS-REPO call into the billing library
    return {"txn": txn, "region": region}


def apply_promo(code, cart_total):
    factor = eval(code)                  # builtin `eval` on external input -> audit red flag
    return charge(cart_total * factor)   # CROSS-REPO call into billing
