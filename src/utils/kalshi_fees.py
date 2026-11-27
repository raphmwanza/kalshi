"""Kalshi taker-fee estimate used for sizing and balance checks.

Verified against Kalshi's fee schedule (effective July 7, 2026):

    fees = round_up(M × 0.07 × C × P × (1 − P))

P is the contract price in dollars, C is the contract count, and M is the
series multiplier (1 for standard markets, including the tennis match-winner
series). ``round_up`` matches the published general trading-fees table, which
rounds the fee up to the next cent. Examples from that table:

    1 contract at 50¢ → $0.02
    1 contract at 10¢ → $0.01
    100 contracts at 50¢ → $1.75
    100 contracts at 25¢ → $1.32

Reserving this amount keeps an "all cash" order from being rejected for
``insufficient_balance``. Maker fees are not included: the tennis entries and
stop-loss sells are immediate-or-cancel taker orders.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

TAKER_FEE_RATE = Decimal("0.07")
DEFAULT_FEE_MULTIPLIER = Decimal("1")


def taker_fee_dollars(
    contracts: int,
    price_dollars: float,
    *,
    multiplier: Decimal = DEFAULT_FEE_MULTIPLIER,
    rate: Decimal = TAKER_FEE_RATE,
) -> float:
    """Taker fee in dollars, rounded up to the next cent. 0 when not a trade."""
    count = int(contracts)
    if count <= 0:
        return 0.0
    try:
        price = Decimal(str(price_dollars))
    except Exception:
        return 0.0
    if price <= 0 or price >= 1:
        return 0.0
    raw = multiplier * rate * Decimal(count) * price * (Decimal(1) - price)
    cents = (raw * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return float(cents / Decimal(100))


def buy_debit_dollars(contracts: int, price_dollars: float) -> float:
    """Contracts × price plus the taker fee, in dollars."""
    count = int(contracts)
    if count <= 0 or price_dollars <= 0:
        return 0.0
    return count * float(price_dollars) + taker_fee_dollars(count, price_dollars)
