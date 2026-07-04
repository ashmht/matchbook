"""Reconciliation: two independent computation paths must agree.

Path A: positions as recorded by the double-entry ledger.
Path B: positions recomputed directly from the raw TradeExecuted stream,
        with no shared code with the ledger's posting logic.

Any divergence is reported as a break. In production this generalizes to
internal-vs-exchange reconciliation (our fills vs the venue's drop copy);
here it catches any bug in posting construction the moment it happens.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .events import Event, TradeExecuted
from .ledger import Ledger
from .types import Side


@dataclass(frozen=True)
class Break:
    account: str
    ledger_position: int
    recomputed_position: int


def positions_from_trades(events: list[Event]) -> dict[str, int]:
    positions: dict[str, int] = defaultdict(int)
    for e in events:
        if isinstance(e, TradeExecuted):
            buy_acct = e.taker_account if e.taker_side is Side.BUY else e.maker_account
            sell_acct = e.maker_account if e.taker_side is Side.BUY else e.taker_account
            positions[buy_acct] += e.qty
            positions[sell_acct] -= e.qty
    return {k: v for k, v in positions.items() if v != 0}


def reconcile(ledger: Ledger, events: list[Event]) -> list[Break]:
    from_ledger = ledger.position_balances()
    from_trades = positions_from_trades(events)
    breaks: list[Break] = []
    for account in sorted(set(from_ledger) | set(from_trades)):
        a, b = from_ledger.get(account, 0), from_trades.get(account, 0)
        if a != b:
            breaks.append(Break(account, a, b))
    return breaks
