"""Reproducible throughput benchmark for the reference engine.

    python3 -m benchmarks.bench

Measures raw commands/sec through the matching engine on the fuzz
workload (mixed limits, markets, cancels, duplicates). The number is for
tracking regressions in the reference implementation and as the baseline
the Rust port is compared against — the Python core's job is correctness,
not speed.
"""

from __future__ import annotations

import statistics
import time

from tests.test_fuzz_invariants import generate_commands
from core.engine import MatchingEngine
from core.risk import RiskLimits

N = 30_000
ROUNDS = 5


def run_once(commands) -> float:
    engine = MatchingEngine(RiskLimits(price_collar_bps=None))
    start = time.perf_counter()
    for cmd in commands:
        engine.process(cmd)
    return time.perf_counter() - start


def main() -> None:
    base = generate_commands(seed=99)
    commands = (base * (N // len(base) + 1))[:N]
    # Duplicated ids in repeated batches exercise the duplicate-rejection
    # path heavily, which is fine — it is part of the realistic mix.
    rates = []
    for i in range(ROUNDS):
        elapsed = run_once(commands)
        rates.append(N / elapsed)
        print(f"round {i + 1}: {rates[-1]:,.0f} commands/sec")
    print(f"median: {statistics.median(rates):,.0f} commands/sec over {ROUNDS} rounds")


if __name__ == "__main__":
    main()
