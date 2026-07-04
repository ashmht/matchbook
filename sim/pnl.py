"""PnL projections over the ledger.

Equity of an account at a mark price m:

    equity(a, m) = cash(a) + position(a) * m

Because the ledger's trial balance is zero per unit (sum of cash postings
is zero AND sum of position postings is zero), total equity across all
accounts — including the exchange fee account — is exactly zero at any
mark price whatsoever. Trading is zero-sum; fees only redistribute it.
That identity is asserted in the simulation tests at multiple marks.
"""

from __future__ import annotations

from core.ledger import CASH, Ledger


def position(ledger: Ledger, account: str) -> int:
    return ledger.balance(f"{account}:position", ledger.instrument)


def cash(ledger: Ledger, account: str) -> int:
    return ledger.balance(f"{account}:cash", CASH)


def equity(ledger: Ledger, account: str, mark: int) -> int:
    return cash(ledger, account) + position(ledger, account) * mark


def total_equity_all_accounts(ledger: Ledger, mark: int) -> int:
    """Sum of equity over every account touched by the ledger, plus fees.

    Computed directly from raw postings (not the balance cache) so it acts
    as an independent check on the projection as well.
    """
    total_cash = 0
    total_position = 0
    for p in ledger.postings:
        if p.unit == CASH:
            total_cash += p.amount
        elif p.unit == ledger.instrument:
            total_position += p.amount
    return total_cash + total_position * mark
