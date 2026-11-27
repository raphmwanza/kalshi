"""Read-only HTML for the tennis trade journal."""

from __future__ import annotations

import html
import json
from typing import Dict, List, Optional


def _cell(value) -> str:
    return html.escape("" if value is None else str(value))


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{100.0 * value:.1f}%"


def _account_sections(summary: Dict, events: List[dict]) -> str:
    accounts = summary.get("accounts") or {}
    if not accounts:
        accounts = {"all": summary}
    blocks = []
    for name, view in accounts.items():
        cycle = view.get("last_cycle") or {}
        status = cycle.get("status") or "no cycle yet"
        when = cycle.get("ts") or "never"
        open_rows = []
        for lot in view.get("open") or []:
            open_rows.append(
                "<tr>"
                f"<td>{_cell(lot.get('ticker'))}</td>"
                f"<td>{_cell(lot.get('player'))}</td>"
                f"<td>{_cell(lot.get('rule'))}</td>"
                f"<td>{_cell(lot.get('size'))}</td>"
                f"<td>{_money(lot.get('entry_price'))}</td>"
                f"<td>{_cell(lot.get('opened_at'))}</td>"
                "</tr>"
            )
        if not open_rows:
            open_rows.append("<tr><td colspan='6'>No open positions.</td></tr>")
        rule_rows = []
        for rule, row in (view.get("by_rule") or {}).items():
            rule_rows.append(
                "<tr>"
                f"<td>{_cell(rule)}</td>"
                f"<td>{row['wins']}W–{row['losses']}L</td>"
                f"<td>{_pct(row.get('win_rate'))}</td>"
                f"<td>{_money(row.get('avg_entry_price'))}</td>"
                f"<td>{_money(row.get('pnl'))}</td>"
                f"<td>{_pct(row.get('implied_edge'))}</td>"
                "</tr>"
            )
        if not rule_rows:
            rule_rows.append("<tr><td colspan='6'>No closed positions yet.</td></tr>")
        recent = [
            event for event in reversed(events)
            if (event.get("account") or "all") == name or name == "all"
        ][:25]
        if name != "all":
            recent = [event for event in reversed(events) if (event.get("account") or "") == name][:25]
        event_rows = []
        for event in recent:
            if (event.get("kind") or "") == "cycle":
                continue
            event_rows.append(
                "<tr>"
                f"<td>{_cell(event.get('ts'))}</td>"
                f"<td>{_cell(event.get('kind'))}</td>"
                f"<td>{_cell(event.get('decision') or event.get('rule') or event.get('exit_kind'))}</td>"
                f"<td>{_cell(event.get('ticker'))}</td>"
                f"<td>{_cell(event.get('player'))}</td>"
                f"<td>{_cell(event.get('score') or event.get('match_label'))}</td>"
                f"<td>{_money(event.get('price')) if event.get('price') is not None else ''}</td>"
                f"<td>{_cell(event.get('size') or event.get('fill_count'))}</td>"
                f"<td>{_cell(event.get('order_status') or event.get('details') or '')}</td>"
                "</tr>"
            )
        if not event_rows:
            event_rows.append("<tr><td colspan='9'>No decisions or trades yet.</td></tr>")
        canvas = "chart-" + "".join(ch if ch.isalnum() else "-" for ch in str(name))
        blocks.append(
            f"<section class='account'>"
            f"<h2>{_cell(name)}</h2>"
            f"<p class='status'>Last cycle <b>{_cell(when)}</b> · status <b>{_cell(status)}</b>"
            f" · {_cell(cycle.get('details') or '')}</p>"
            f"<div class='cards'>"
            f"<div class='card'>Win rate<b>{_pct(view.get('win_rate'))}</b>"
            f"{view.get('wins', 0)}W–{view.get('losses', 0)}L</div>"
            f"<div class='card'>Realized PnL<b>{_money(view.get('total_pnl'))}</b></div>"
            f"<div class='card'>Open<b>{view.get('open_positions', 0)}</b></div>"
            f"<div class='card'>Settled edge<b>{_pct(view.get('implied_edge'))}</b>"
            f"won {_pct(view.get('settled_win_rate'))} vs implied {_pct(view.get('avg_implied_prob'))}</div>"
            f"</div>"
            f"<h3>Open positions</h3>"
            f"<table><thead><tr><th>Ticker</th><th>Player</th><th>Rule</th><th>Size</th><th>Entry</th><th>Opened</th></tr></thead>"
            f"<tbody>{''.join(open_rows)}</tbody></table>"
            f"<h3>PnL over time</h3><canvas id='{_cell(canvas)}' height='90'></canvas>"
            f"<h3>PnL by entry rule</h3>"
            f"<table><thead><tr><th>Rule</th><th>Record</th><th>Win rate</th><th>Avg entry</th><th>PnL</th><th>Settled edge</th></tr></thead>"
            f"<tbody>{''.join(rule_rows)}</tbody></table>"
            f"<h3>Recent trades and decisions</h3>"
            f"<table><thead><tr><th>When</th><th>Kind</th><th>Rule</th><th>Ticker</th><th>Player</th><th>Score</th><th>Price</th><th>Size</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(event_rows)}</tbody></table>"
            f"</section>"
        )
    return "".join(blocks)


def render_dashboard(summary: Dict, events: List[dict]) -> str:
    """Read-only page. No forms and no trading controls."""
    accounts = summary.get("accounts") or {}
    chart = {
        name: [
            {"t": point.get("ts"), "y": point.get("cumulative")}
            for point in (view.get("pnl_series") or [])
        ]
        for name, view in (accounts or {"all": summary}).items()
    }
    payload = json.dumps(chart).replace("<", "\\u003c")
    edge = summary.get("implied_edge")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tennis journal</title>
<style>
  body {{ font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 1.5rem; color: #1b1b1b; background: #fafafa; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.2rem; }}
  h2 {{ margin-top: 2rem; }}
  section.account {{ background: #fff; border: 1px solid #e6e6e6; border-radius: 10px; padding: 1rem 1.1rem; margin-top: 1rem; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 0.8rem; margin: 0.8rem 0 1rem; }}
  .card {{ border: 1px solid #eee; border-radius: 8px; padding: 0.7rem 0.9rem; min-width: 8rem; }}
  .card b {{ display: block; font-size: 1.25rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
  th, td {{ border-bottom: 1px solid #eee; text-align: left; padding: 0.35rem 0.45rem; vertical-align: top; }}
  th {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; color: #555; }}
  .status {{ color: #333; }}
  p.note {{ color: #444; max-width: 48rem; }}
</style>
</head>
<body>
<h1>Tennis journal</h1>
<p>Read-only. This page cannot place, cancel, or change orders.</p>
<p>Overall settled edge {_pct(edge)} · realized PnL {_money(summary.get('total_pnl'))} · win rate {_pct(summary.get('win_rate'))}.</p>
{_account_sections(summary, events)}
<p class="note">Settled edge is how often contracts held to settlement won, minus the average price paid. Positive means those entries beat their implied probability. Last cycle time is the heartbeat the trading process writes after each account.</p>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"></script>
<script>
const series = {payload};
for (const [name, points] of Object.entries(series)) {{
  const id = "chart-" + name.replace(/[^A-Za-z0-9]/g, "-");
  const canvas = document.getElementById(id);
  if (!canvas || typeof Chart === "undefined") continue;
  new Chart(canvas, {{
    type: "line",
    data: {{
      labels: points.map(p => p.t),
      datasets: [{{ label: "Cumulative PnL", data: points.map(p => p.y), tension: 0.2, borderColor: "#1d4e89", fill: false }}]
    }},
    options: {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ maxTicksLimit: 6 }} }} }} }}
  }});
}}
</script>
</body>
</html>
"""
