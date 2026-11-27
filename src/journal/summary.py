"""Fold the tennis journal into win rate, PnL, and implied-probability edge.

Win rate is the share of closed positions with positive PnL (a stop or a
take-profit counts). The implied-probability check uses only settlements:
the entry price was the market's P(win), so it is compared with how often
those contracts actually won, not with whether an early sale was profitable.
A positive edge means settled entries won more often than their prices said.
"""

from __future__ import annotations

from typing import Dict, List, Optional


def _as_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_bool(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "win"}:
        return True
    if text in {"0", "false", "no", "loss"}:
        return False
    return None


def _pnl_series(rows: List[dict]) -> List[dict]:
    ordered = sorted(rows, key=lambda row: str(row.get("closed_at") or ""))
    cumulative = 0.0
    series = []
    for row in ordered:
        cumulative += row["pnl"]
        series.append({
            "ts": row.get("closed_at") or "",
            "pnl": row["pnl"],
            "cumulative": cumulative,
            "ticker": row.get("ticker") or "",
        })
    return series


def _last_cycles(events: List[dict]) -> Dict[str, dict]:
    found: Dict[str, dict] = {}
    for event in events:
        if (event.get("kind") or "").lower() != "cycle":
            continue
        account = event.get("account") or ""
        found[account] = {
            "ts": event.get("ts") or "",
            "status": event.get("order_status") or "ok",
            "details": event.get("details") or "",
        }
    return found


def _rule_table(closed: List[dict]) -> Dict[str, dict]:
    def _rate(rows: List[dict]) -> Optional[float]:
        if not rows:
            return None
        return sum(1 for row in rows if row["won"]) / len(rows)

    def _avg(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)

    grouped: Dict[str, List[dict]] = {}
    for row in closed:
        grouped.setdefault(row["rule"], []).append(row)
    table = {}
    for rule, rows in sorted(grouped.items()):
        resolved = [row for row in rows if row["resolved"]]
        implied = _avg([row["implied"] for row in resolved])
        realized = _rate(resolved)
        table[rule] = {
            "closed": len(rows),
            "wins": sum(1 for row in rows if row["won"]),
            "losses": sum(1 for row in rows if not row["won"]),
            "win_rate": _rate(rows),
            "avg_entry_price": _avg([row["entry_price"] for row in rows]),
            "pnl": sum(row["pnl"] for row in rows),
            "settled": len(resolved),
            "avg_implied_prob": implied,
            "settled_win_rate": realized,
            "implied_edge": None if implied is None or realized is None else realized - implied,
        }
    return table


def _pack(
    closed: List[dict],
    still_open: List[dict],
    decisions: Dict[str, int],
    last_cycle: Optional[dict],
) -> Dict:
    def _rate(rows: List[dict]) -> Optional[float]:
        if not rows:
            return None
        return sum(1 for row in rows if row["won"]) / len(rows)

    def _avg(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)

    resolved = [row for row in closed if row["resolved"]]
    avg_implied = _avg([row["implied"] for row in resolved])
    settled_rate = _rate(resolved)
    return {
        "entries": len(still_open) + len(closed),
        "open_positions": len(still_open),
        "open": [
            {
                "ticker": lot["ticker"],
                "player": lot.get("player") or "",
                "rule": lot["rule"],
                "size": lot["remaining"],
                "entry_price": lot["entry_price"],
                "opened_at": lot.get("opened_at") or "",
            }
            for lot in still_open
        ],
        "closed": len(closed),
        "wins": sum(1 for row in closed if row["won"]),
        "losses": sum(1 for row in closed if not row["won"]),
        "win_rate": _rate(closed),
        "avg_entry_price": _avg(
            [row["entry_price"] for row in closed] or [lot["entry_price"] for lot in still_open]
        ),
        "total_pnl": sum(row["pnl"] for row in closed),
        "decisions": decisions,
        "by_rule": _rule_table(closed),
        "avg_implied_prob": avg_implied,
        "settled_win_rate": settled_rate,
        "implied_edge": (
            None if avg_implied is None or settled_rate is None else settled_rate - avg_implied
        ),
        "settled": len(resolved),
        "pnl_series": _pnl_series(closed),
        "last_cycle": last_cycle,
    }


def summarize_events(events: List[dict]) -> Dict:
    """Summarize append-only journal rows. Unknown kinds are ignored."""
    ordered = sorted(
        events,
        key=lambda row: (str(row.get("ts") or ""), int(row.get("id") or 0)),
    )
    open_lots: List[dict] = []
    closed: List[dict] = []
    decisions = {"enter": 0, "skip": 0}

    def oldest_open(account: str, ticker: str) -> Optional[dict]:
        for lot in open_lots:
            if lot["account"] == account and lot["ticker"] == ticker and lot["remaining"] > 0:
                return lot
        return None

    for event in ordered:
        kind = (event.get("kind") or "").lower()
        if kind == "signal":
            decision = (event.get("decision") or "").lower()
            if decision in decisions:
                decisions[decision] += 1
            continue
        if kind == "order" and (event.get("order_action") or "").lower() == "buy":
            if (event.get("order_status") or "").lower() == "dry_run":
                continue
            filled = _as_int(event.get("fill_count"))
            if filled <= 0:
                continue
            price = _as_float(event.get("price")) or 0.0
            implied = _as_float(event.get("implied_prob"))
            open_lots.append({
                "account": event.get("account") or "",
                "ticker": event.get("ticker") or "",
                "rule": event.get("rule") or "unknown",
                "player": event.get("player") or "",
                "match_label": event.get("match_label") or "",
                "opened_at": event.get("ts") or "",
                "entry_price": price,
                "implied": price if implied is None else implied,
                "size": filled,
                "remaining": filled,
                "fees": _as_float(event.get("fees")) or 0.0,
            })
            continue
        if kind == "cycle":
            continue
        if kind not in {"exit", "settlement"}:
            continue
        account = event.get("account") or ""
        ticker = event.get("ticker") or ""
        lot = oldest_open(account, ticker)
        if lot is None:
            continue
        close_size = _as_int(event.get("size")) or lot["remaining"]
        close_size = min(close_size, lot["remaining"])
        if close_size <= 0:
            continue
        entry = lot["entry_price"]
        fee_share = lot["fees"] * (close_size / lot["size"]) if lot["size"] else 0.0
        binary = _as_bool(event.get("won")) if kind == "settlement" else None
        exit_price = _as_float(event.get("price"))
        if kind == "settlement" and binary is not None:
            proceeds = close_size * (1.0 if binary else 0.0)
            won = binary
            resolved = True
        elif exit_price is not None:
            proceeds = close_size * exit_price
            won = proceeds > entry * close_size
            resolved = False
        else:
            continue
        pnl = proceeds - entry * close_size - fee_share
        lot["remaining"] -= close_size
        closed.append({
            "account": account,
            "ticker": ticker,
            "rule": lot["rule"],
            "player": lot.get("player") or "",
            "pnl": pnl,
            "won": won,
            "resolved": resolved,
            "entry_price": entry,
            "implied": lot["implied"],
            "size": close_size,
            "closed_at": event.get("ts") or "",
        })

    still_open = [lot for lot in open_lots if lot["remaining"] > 0]
    cycles = _last_cycles(ordered)
    packed = _pack(closed, still_open, decisions, cycles.get(""))
    names = sorted({
        *(row.get("account") or "" for row in ordered if row.get("account")),
        *(lot["account"] for lot in still_open),
    })
    accounts = {}
    for name in names:
        account_closed = [row for row in closed if row["account"] == name]
        account_open = [lot for lot in still_open if lot["account"] == name]
        account_decisions = {"enter": 0, "skip": 0}
        for event in ordered:
            if (event.get("account") or "") != name:
                continue
            if (event.get("kind") or "").lower() != "signal":
                continue
            decision = (event.get("decision") or "").lower()
            if decision in account_decisions:
                account_decisions[decision] += 1
        accounts[name] = _pack(
            account_closed, account_open, account_decisions, cycles.get(name)
        )
    packed["accounts"] = accounts
    packed["open_positions"] = len(still_open)
    if cycles:
        packed["last_cycle"] = max(cycles.values(), key=lambda item: item.get("ts") or "")
    return packed


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _dollars(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def format_summary(summary: Dict) -> str:
    """Plain-text report for `cli.py tennis-report`."""
    lines = [
        "Tennis journal",
        f"  Entries:          {summary['entries']} ({summary['open_positions']} still open)",
        f"  Closed:           {summary['closed']}  "
        f"({summary['wins']}W–{summary['losses']}L, win rate {_pct(summary['win_rate'])})",
        f"  Avg entry price:  {_dollars(summary['avg_entry_price'])}",
        f"  Realized PnL:     {_dollars(summary['total_pnl'])}",
        f"  Decisions:        {summary['decisions']['enter']} enter / "
        f"{summary['decisions']['skip']} skip",
        "  Settled vs price: "
        f"won {_pct(summary['settled_win_rate'])} vs implied {_pct(summary['avg_implied_prob'])} "
        f"(edge {_pct(summary['implied_edge'])}, n={summary['settled']})",
        "  PnL by rule:",
    ]
    if not summary["by_rule"]:
        lines.append("    (no closed positions yet)")
    for rule, row in summary["by_rule"].items():
        lines.append(
            f"    {rule:<16} {row['wins']}W–{row['losses']}L  "
            f"win {_pct(row['win_rate'])}  avg entry {_dollars(row['avg_entry_price'])}  "
            f"PnL {_dollars(row['pnl'])}  settled edge {_pct(row['implied_edge'])}"
        )
    lines.append(
        "  Edge is settled win rate minus the average entry price. "
        "Positive means those entries won more often than the price implied."
    )
    return "\n".join(lines)
