"""Pre-trade risk gate.

Pure function: (limits, market state, order) -> rejection reason or None.
The gate runs before an order reaches the matching loop, mirroring the
pre-trade risk checks a real venue or broker applies (order size limits,
price collars against the last trade, open-order caps, kill switch).

Everything here is integer arithmetic; the price collar is expressed in
basis points and evaluated with cross-multiplication to avoid division.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import OrderType, RejectReason, SubmitOrder


@dataclass(frozen=True)
class RiskLimits:
    max_order_qty: int = 1_000_000
    price_collar_bps: int | None = 2_000     # None disables the collar
    max_open_orders_per_account: int = 1_000
    kill_switch: bool = False


def check(
    limits: RiskLimits,
    order: SubmitOrder,
    last_trade_price: int | None,
    open_orders_for_account: int,
) -> RejectReason | None:
    if limits.kill_switch:
        return RejectReason.KILL_SWITCH

    if order.qty <= 0:
        return RejectReason.INVALID_QTY

    if order.order_type is OrderType.LIMIT:
        if order.price is None or order.price <= 0:
            return RejectReason.INVALID_PRICE
    elif order.price is not None:
        return RejectReason.INVALID_PRICE  # market orders carry no price

    if order.qty > limits.max_order_qty:
        return RejectReason.MAX_ORDER_QTY

    if (
        limits.price_collar_bps is not None
        and order.order_type is OrderType.LIMIT
        and last_trade_price is not None
    ):
        # |price - last| / last > collar_bps / 10_000, without division:
        deviation = abs(order.price - last_trade_price) * 10_000
        if deviation > limits.price_collar_bps * last_trade_price:
            return RejectReason.PRICE_COLLAR

    if open_orders_for_account >= limits.max_open_orders_per_account:
        return RejectReason.MAX_OPEN_ORDERS

    return None
