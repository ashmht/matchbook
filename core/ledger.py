"""Post-trade double-entry ledger.

Every TradeExecuted event journals an atomic set of signed postings that
sums to zero per unit (instrument lots, cash ticks). The ledger is
append-only: postings are never mutated or deleted, and balances are a
projection over them. This is the same discipline as a payments ledger —
money (and inventory) is only ever moved, never created or destroyed.

Postings per trade (buyer B, seller S, price p, qty q, taker T):
    instrument:  +q  B.position        -q  S.position
    cash:        -pq B.cash            +pq S.cash
    taker fee:   -f  T.cash            +f  exchange.fees   (f in cash units)

The trial balance (sum of all postings per unit == 0) is a structural
invariant checked in tests after every fuzzed run, and `reconcile`
cross-checks ledger position balances against positions recomputed
independently from the trade stream — the classic "two independent
paths must agree" reconciliation pattern.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .events import Event, TradeExecuted
from .types import Side

CASH = "CASH"
FEE_ACCOUNT = "exchange:fees"


@dataclass(frozen=True)
class Posting:
    journal_id: int         # groups postings created by one event
    account: str
    unit: str               # CASH or the instrument symbol
    amount: int             # signed; each journal sums to zero per unit


class Ledger:
    def __init__(self, instrument: str, taker_fee_bps: int = 0) -> None:
        self.instrument = instrument
        self.taker_fee_bps = taker_fee_bps
        self.postings: list[Posting] = []
        self._balances: dict[tuple[str, str], int] = defaultdict(int)
        self._applied_event_seqs: set[int] = set()

    # -- event application ------------------------------------------------------

    def apply(self, event: Event) -> None:
        """Apply an event. Idempotent by event sequence number.

        Event delivery in a distributed system is at-least-once: retries,
        replays, and consumer restarts all redeliver. A ledger that
        double-posts on redelivery silently corrupts balances, so
        duplicates are detected by the event's engine-assigned seq and
        skipped — the same dedupe-by-key discipline as idempotent order
        submission, applied on the consumer side.
        """
        if isinstance(event, TradeExecuted):
            if event.seq in self._applied_event_seqs:
                return
            self._applied_event_seqs.add(event.seq)
            self._apply_trade(event)

    def _apply_trade(self, t: TradeExecuted) -> None:
        buyer, seller = (
            (t.taker_account, t.maker_account)
            if t.taker_side is Side.BUY
            else (t.maker_account, t.taker_account)
        )
        notional = t.price * t.qty
        fee = (notional * self.taker_fee_bps) // 10_000

        # Journal id = the event's sequence number: every posting traces
        # back to exactly one engine event.
        jid = t.seq
        postings = [
            Posting(jid, f"{buyer}:position", self.instrument, +t.qty),
            Posting(jid, f"{seller}:position", self.instrument, -t.qty),
            Posting(jid, f"{buyer}:cash", CASH, -notional),
            Posting(jid, f"{seller}:cash", CASH, +notional),
        ]
        if fee > 0:
            postings.append(Posting(jid, f"{t.taker_account}:cash", CASH, -fee))
            postings.append(Posting(jid, FEE_ACCOUNT, CASH, +fee))

        # Atomicity: validate the journal balances before committing it.
        sums: dict[str, int] = defaultdict(int)
        for p in postings:
            sums[p.unit] += p.amount
        assert all(v == 0 for v in sums.values()), "unbalanced journal"

        self.postings.extend(postings)
        for p in postings:
            self._balances[(p.account, p.unit)] += p.amount

    # -- projections ------------------------------------------------------------

    def balance(self, account: str, unit: str) -> int:
        return self._balances[(account, unit)]

    def trial_balance(self) -> dict[str, int]:
        """Sum of all postings per unit. Must be all zeros, always."""
        sums: dict[str, int] = defaultdict(int)
        for p in self.postings:
            sums[p.unit] += p.amount
        return dict(sums)

    def position_balances(self) -> dict[str, int]:
        """account -> instrument position, from the ledger's view."""
        out: dict[str, int] = {}
        for (account, unit), amount in self._balances.items():
            if unit == self.instrument and account.endswith(":position"):
                out[account.removesuffix(":position")] = amount
        return {k: v for k, v in out.items() if v != 0}
