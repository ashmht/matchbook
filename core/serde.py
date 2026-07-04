"""Command log serialization (JSON Lines).

The determinism guarantee is only useful if the command stream survives a
process restart. This module round-trips commands through a durable text
format so that a session can be replayed from a log file: serialize every
inbound command, and recovery is `replay(read_log(path))` on a fresh
engine — which the tests assert reproduces the identical event stream and
snapshot.

JSON is the readable choice for the reference implementation; the Rust
port will use a length-prefixed binary format, differentially tested to
decode to the same commands.
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator, TextIO

from .types import (
    CancelOrder,
    Command,
    OrderType,
    Side,
    SubmitOrder,
    TimeInForce,
)


def dumps(cmd: Command) -> str:
    if isinstance(cmd, SubmitOrder):
        return json.dumps(
            {
                "t": "submit",
                "ts": cmd.ts,
                "id": cmd.order_id,
                "acct": cmd.account,
                "side": cmd.side.value,
                "type": cmd.order_type.value,
                "qty": cmd.qty,
                "px": cmd.price,
                "tif": cmd.tif.value,
            },
            separators=(",", ":"),
        )
    if isinstance(cmd, CancelOrder):
        return json.dumps(
            {"t": "cancel", "ts": cmd.ts, "id": cmd.order_id, "acct": cmd.account},
            separators=(",", ":"),
        )
    raise TypeError(f"unknown command: {cmd!r}")


def loads(line: str) -> Command:
    d = json.loads(line)
    if d["t"] == "submit":
        return SubmitOrder(
            ts=d["ts"],
            order_id=d["id"],
            account=d["acct"],
            side=Side(d["side"]),
            order_type=OrderType(d["type"]),
            qty=d["qty"],
            price=d["px"],
            tif=TimeInForce(d["tif"]),
        )
    if d["t"] == "cancel":
        return CancelOrder(ts=d["ts"], order_id=d["id"], account=d["acct"])
    raise ValueError(f"unknown command tag: {d['t']!r}")


def write_log(commands: Iterable[Command], fp: TextIO) -> None:
    for cmd in commands:
        fp.write(dumps(cmd))
        fp.write("\n")


def read_log(fp: TextIO) -> Iterator[Command]:
    for line in fp:
        line = line.strip()
        if line:
            yield loads(line)
