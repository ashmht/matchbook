# matchbook

[![ci](https://github.com/ashmht/matchbook/actions/workflows/ci.yml/badge.svg)](https://github.com/ashmht/matchbook/actions/workflows/ci.yml)

A miniature trading venue built with the discipline of a payments system: a
price-time-priority matching engine, a pre-trade risk gate, a double-entry
post-trade ledger, and independent reconciliation — all as a pure,
deterministic, event-sourced core.

## Why this exists

Trading infrastructure and payments infrastructure share the same hard
problems: exactly-once semantics under retries, append-only sources of truth,
conservation invariants ("money and inventory are moved, never created"), and
reconciliation between independent views of the same activity. This project
takes the discipline of production ledger and settlement systems and applies
it to a matching engine, making those invariants executable and continuously
tested.

## Architecture in one paragraph

The core is a functional-core/imperative-shell design. `MatchingEngine` is a
pure state machine: `process(command) -> events`. It never reads a clock,
never touches I/O, and never generates randomness — timestamps arrive on
commands, so the same command stream always produces a byte-identical event
stream. The book, positions, and the double-entry ledger are all projections
of that event stream. Prices are integer ticks and quantities integer lots;
no float ever touches a money path. Order ids are client-supplied idempotency
keys, so submission is at-most-once by construction.

## The six invariants (all fuzz-tested)

The test suite fires thousands of randomized commands (seeded, reproducible)
at the engine and asserts, per run:

1. **No crossed book** — best bid < best ask after every single command.
2. **Quantity conservation** — for every accepted order, original quantity
   equals filled + canceled + still-resting. Nothing leaks, nothing appears.
3. **Determinism** — replaying the identical command stream on a fresh engine
   produces an identical event stream and an identical state snapshot.
4. **Trial balance** — the sum of all ledger postings is zero per unit
   (instrument and cash), including taker fees.
5. **Clean reconciliation** — positions computed by the ledger and positions
   recomputed independently from the raw trade stream agree exactly; the
   reconciler is also tested to *catch* an injected break.
6. **No counter drift** — the book's maintained per-account open-order
   counters always match an independent recount over the order index.

Run everything with:

```
python3 -m unittest discover -s tests
```

## Matching semantics

Price-time priority with FIFO within a level; execution always at the maker's
resting price. LIMIT GTC remainders rest; LIMIT IOC and MARKET remainders are
canceled with explicit reasons (market orders never rest). Self-trade
prevention uses cancel-resting: an incoming order that would match the same
account's resting order cancels it and continues matching. The pre-trade risk
gate applies a kill switch, max order size, per-account open-order caps, and a
price collar in basis points against the last trade — all in integer
arithmetic (cross-multiplication, no division).

## Status and roadmap

This Python core is the **executable specification**. It processes on the
order of hundreds of thousands of commands/sec single-threaded (reproduce
with `python3 -m benchmarks.bench`), which is beside the point — its job is
to be obviously correct and to serve as the differential-testing oracle for
a future hot-path port. Commands serialize to a JSONL log (`core/serde.py`),
and the recovery path — replay the log into a fresh engine — is tested to
reproduce the identical event stream and snapshot: the command log is the WAL.

The strategy layer adds an inventory-skewed market maker
(`strategy/market_maker.py`, a pure decision function) and a seeded market
simulation (`sim/session.py`) in which the MM quotes against noise and
informed order flow, with all PnL flowing through the double-entry ledger.
The simulation tests assert five more system-level properties: end-to-end
replay determinism, exact zero-sum of total equity across all accounts at
any mark price (fees only redistribute), a hard inventory bound verified at
every trade, positive expected PnL against pure noise flow, and strictly
lower PnL under fully informed flow (adverse selection) on every seed.

## Non-obvious design decisions

A few decisions are load-bearing and easy to get wrong; each is pinned by a
test and documented at the code where it lives.

- **Inventory skew is clamped to the half-spread.** Without the clamp, a
  large one-sided position can push a quote past the reference price (a bid
  above reference). In a market where the maker's own trades feed the
  reference, that becomes a self-referential ratchet that squeezes its own
  position. The clamp keeps quotes on the correct side of reference.
- **The maker quotes around the external book's mid, not its own prints.**
  It pulls its own quotes before reading the mid, so it prices against the
  market's standing liquidity rather than its last fill.
- **A one-step-stale two-sided mid beats a fresh one-sided print.** When the
  book goes one-sided, the reference holds its last good mid instead of
  falling back to the last trade, which right after a sweep can sit several
  ticks off fair and walk the maker into fresher quotes.
- **The engine snapshot includes the seen-order-id set.** Two engines with
  identical books but different duplicate-rejection state are not
  behaviorally identical — one accepts a resubmitted id, the other rejects
  it — so the ids are part of the canonical snapshot. "Equal snapshots imply
  equal future behavior" would otherwise be false.
- **Ledger application is idempotent by engine event sequence.** Event
  delivery is at-least-once; retries and replays redeliver. A ledger that
  double-posts on redelivery silently corrupts balances, so duplicates are
  detected by the event's sequence number and skipped. Every posting's
  journal id traces back to exactly one engine event.
- **The risk gate uses maintained O(1) per-account counters.** The book
  keeps per-account open-order counts on add/remove instead of scanning on
  every submit, and a fuzz invariant recounts independently to assert zero
  drift.

## Known limitations

Stated so the cut corners are deliberate rather than invisible. The venue is
single-instrument; multi-symbol support means one book per symbol behind a
router, which changes nothing structural. There is no modify/replace —
clients cancel and resubmit, losing queue priority, which is honest but
incomplete. Cancels are O(level depth) in this reference book; the planned
Rust port will use an intrusive doubly linked list per level with an
id-to-node index for O(1) cancels. The price collar applies only to limit orders. The ledger's
idempotency dedupe is an unbounded set; in production, ordered delivery per
partition lets a high-watermark sequence number replace it. The simulation's
microstructure is stylized — one market maker, one outside liquidity
provider, memoryless flow, no queue-position or latency modeling — enough to
demonstrate spread capture and adverse selection, not to estimate real
strategy PnL.

Planned next: a Rust port of the engine, differentially tested against this
core (same command stream in, identical event stream out) with criterion
benchmarks; and an imperative shell — order gateway, market data publisher,
and a live exchange connector for a crypto testnet, with daily reconciliation
against the venue's view of fills.

## Layout

```
core/
  types.py       commands, sides, order types (integer ticks/lots)
  events.py      the event vocabulary — the source of truth
  book.py        price levels + FIFO queues, canonical snapshots
  engine.py      the deterministic matching state machine
  risk.py        pre-trade checks as a pure function
  ledger.py      append-only double-entry postings + balances (idempotent apply)
  reconcile.py   independent position recomputation, break detection
  serde.py       JSONL command log — serialize, recover, replay
strategy/
  market_maker.py  inventory-skewed quoting as a pure function
sim/
  session.py     seeded end-to-end market simulation
  pnl.py         equity projections over the ledger; zero-sum identity
tests/
  test_engine.py           matching semantics, STP, cancels, idempotency
  test_ledger.py           postings, fees, trial balance, break detection
  test_fuzz_invariants.py  seeded fuzzing of the six core invariants
  test_strategy.py         quoting, skew clamp, position-limit behavior
  test_sim.py              determinism, zero-sum, inventory bound, PnL
  test_serde.py            log round-trip, replay recovery, snapshot completeness
benchmarks/
  bench.py       reproducible throughput measurement
```

Zero dependencies. Python 3.10+ (tested on 3.12), standard library only.
