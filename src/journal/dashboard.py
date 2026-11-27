"""Small local HTML dashboard for the tennis journal.

This is a localhost viewer (`python cli.py tennis-dashboard`). The Railway
process stays a worker and does not listen on a port.
"""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from src.journal.summary import summarize_events


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


def render_dashboard(summary: Dict, events: List[dict]) -> str:
    """Self-contained HTML report. Recent rows are the newest 40 events."""
    rule_rows = []
    for rule, row in summary.get("by_rule", {}).items():
        rule_rows.append(
            "<tr>"
            f"<td>{_cell(rule)}</td>"
            f"<td>{row['wins']}W–{row['losses']}L</td>"
            f"<td>{_pct(row['win_rate'])}</td>"
            f"<td>{_money(row['avg_entry_price'])}</td>"
            f"<td>{_money(row['pnl'])}</td>"
            f"<td>{_pct(row['implied_edge'])}</td>"
            "</tr>"
        )
    if not rule_rows:
        rule_rows.append("<tr><td colspan='6'>No closed positions yet.</td></tr>")

    recent = list(reversed(events[-40:]))
    event_rows = []
    for event in recent:
        event_rows.append(
            "<tr>"
            f"<td>{_cell(event.get('ts'))}</td>"
            f"<td>{_cell(event.get('account'))}</td>"
            f"<td>{_cell(event.get('kind'))}</td>"
            f"<td>{_cell(event.get('rule') or event.get('exit_kind') or event.get('decision'))}</td>"
            f"<td>{_cell(event.get('ticker'))}</td>"
            f"<td>{_cell(event.get('player'))}</td>"
            f"<td>{_cell(event.get('score') or event.get('match_label'))}</td>"
            f"<td>{_money(event.get('price')) if event.get('price') is not None else ''}</td>"
            f"<td>{_cell(event.get('size'))}</td>"
            f"<td>{_cell(event.get('order_status') or event.get('won'))}</td>"
            "</tr>"
        )
    if not event_rows:
        event_rows.append("<tr><td colspan='10'>No journal rows yet.</td></tr>")

    edge = summary.get("implied_edge")
    edge_class = "pos" if (edge or 0) > 0 else "neg" if (edge or 0) < 0 else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Tennis journal</title>
<style>
  body {{ font: 15px/1.4 ui-sans-serif, system-ui, sans-serif; margin: 2rem; color: #1b1b1b; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.2rem; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 0.8rem; margin: 1rem 0 1.5rem; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 0.8rem 1rem; min-width: 10rem; }}
  .card b {{ display: block; font-size: 1.3rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }}
  th, td {{ border-bottom: 1px solid #eee; text-align: left; padding: 0.35rem 0.5rem; vertical-align: top; }}
  th {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.03em; color: #555; }}
  .pos {{ color: #0a7a32; }} .neg {{ color: #a12626; }}
  p.note {{ color: #444; max-width: 46rem; }}
</style>
</head>
<body>
<h1>Tennis journal</h1>
<p>Entries versus the price they paid. A positive settled edge means those contracts won more often than the entry price implied.</p>
<div class="cards">
  <div class="card">Closed<b>{summary['closed']}</b>{summary['wins']}W–{summary['losses']}L</div>
  <div class="card">Win rate<b>{_pct(summary['win_rate'])}</b>positive PnL</div>
  <div class="card">Avg entry<b>{_money(summary['avg_entry_price'])}</b></div>
  <div class="card">Realized PnL<b>{_money(summary['total_pnl'])}</b></div>
  <div class="card">Settled edge<b class="{edge_class}">{_pct(edge)}</b>won {_pct(summary['settled_win_rate'])} vs implied {_pct(summary['avg_implied_prob'])}</div>
  <div class="card">Still open<b>{summary['open_positions']}</b>{summary['entries']} entries logged</div>
</div>
<h2>PnL by rule</h2>
<table>
<thead><tr><th>Rule</th><th>Record</th><th>Win rate</th><th>Avg entry</th><th>PnL</th><th>Settled edge</th></tr></thead>
<tbody>
{''.join(rule_rows)}
</tbody>
</table>
<h2>Recent decisions and trades</h2>
<table>
<thead><tr><th>When</th><th>Account</th><th>Kind</th><th>Rule</th><th>Ticker</th><th>Player</th><th>Score</th><th>Price</th><th>Size</th><th>Status</th></tr></thead>
<tbody>
{''.join(event_rows)}
</tbody>
</table>
<p class="note">Decisions logged: {summary['decisions']['enter']} entries attempted, {summary['decisions']['skip']} skips. Semifinals and finals are not entered. Each row is tagged with the Kalshi account that owned it.</p>
</body>
</html>
"""


def serve_dashboard(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Reload the journal on every request and serve it on localhost."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            import asyncio

            from src.journal.tennis_log import open_journal

            async def _load():
                journal = await open_journal()
                try:
                    return await journal.load_events()
                finally:
                    await journal.close()

            events = asyncio.run(_load())
            body = render_dashboard(summarize_events(events), events).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Tennis dashboard at http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
