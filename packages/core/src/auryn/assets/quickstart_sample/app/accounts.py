"""Accounts model. `PremiumAccount` extends `Account` -- an EXTENDS edge the graph
captures, so inheritance questions ("what subclasses Account?") resolve structurally.
"""

from dataclasses import dataclass


@dataclass
class Account:
    id: int
    limit_cents: int = 50_000


class PremiumAccount(Account):
    """Vetted customers get a higher charge limit."""

    def __init__(self, id: int) -> None:
        super().__init__(id=id, limit_cents=500_000)


_ACCOUNTS: dict[int, Account] = {1: Account(1), 2: PremiumAccount(2)}


def get_account(account_id: int) -> Account:
    return _ACCOUNTS[account_id]
