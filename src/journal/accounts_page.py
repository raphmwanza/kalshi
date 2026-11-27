"""HTML for the dashboard Accounts page. Private keys are never rendered."""

from __future__ import annotations

import html
from typing import List

from src.journal.account_store import AccountRecord


def _cell(value) -> str:
    return html.escape("" if value is None else str(value))


def render_accounts_page(
    accounts: List[AccountRecord],
    *,
    csrf_token: str,
    flash: str = "",
    error: str = "",
    unavailable: str = "",
) -> str:
    token = html.escape(csrf_token)
    notice = ""
    if error:
        notice = f"<p class='error'>{_cell(error)}</p>"
    elif flash:
        notice = f"<p class='flash'>{_cell(flash)}</p>"

    if unavailable:
        body = (
            f"<p class='error'>{_cell(unavailable)}</p>"
            "<p class='note'>Key storage is off. Set KEYS_ENCRYPTION_KEY to a Fernet "
            "key and point DATABASE_URL at Postgres on both this service and the "
            "worker. Nothing was written.</p>"
        )
    else:
        rows = []
        for account in accounts:
            live_label = "Live" if account.live else "Dry run"
            enabled_label = "Enabled" if account.enabled else "Disabled"
            next_enabled = "false" if account.enabled else "true"
            enabled_button = "Disable" if account.enabled else "Enable"
            next_live = "false" if account.live else "true"
            live_button = "Switch to dry run" if account.live else "Switch to live"
            rows.append(
                "<tr>"
                f"<td>{_cell(account.name)}</td>"
                f"<td>{_cell(account.api_key_id)}</td>"
                f"<td>ending {_cell(account.key_hint or 'unknown')}</td>"
                f"<td>{enabled_label}</td>"
                f"<td>{live_label}</td>"
                "<td class='actions'>"
                f"<form method='post' action='/accounts/{account.id}/enabled'>"
                f"<input type='hidden' name='csrf_token' value='{token}'>"
                f"<input type='hidden' name='enabled' value='{next_enabled}'>"
                f"<button type='submit'>{enabled_button}</button></form>"
                f"<form method='post' action='/accounts/{account.id}/live'>"
                f"<input type='hidden' name='csrf_token' value='{token}'>"
                f"<input type='hidden' name='live' value='{next_live}'>"
                f"<button type='submit'>{live_button}</button></form>"
                f"<form method='post' action='/accounts/{account.id}/test'>"
                f"<input type='hidden' name='csrf_token' value='{token}'>"
                "<button type='submit'>Test connection</button></form>"
                f"<form method='post' action='/accounts/{account.id}/delete'>"
                f"<input type='hidden' name='csrf_token' value='{token}'>"
                "<button type='submit'>Delete</button></form>"
                "</td></tr>"
            )
        if not rows:
            rows.append("<tr><td colspan='6'>No stored accounts yet.</td></tr>")
        body = f"""
<p class="note">Paste a Kalshi API key ID and its private key. The key is encrypted before it is saved and is not shown again. The table keeps the key ID and the last four characters of the key body. New accounts are enabled and set to dry run. Live orders for a stored account also require <code>BOT_LIVE=true</code> on the worker. The worker reloads enabled accounts every cycle.</p>
<table>
<thead><tr><th>Name</th><th>Key ID</th><th>Private key</th><th>State</th><th>Orders</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<h2>Add an account</h2>
<form method="post" action="/accounts" autocomplete="off">
  <input type="hidden" name="csrf_token" value="{token}">
  <label>Name <input name="name" required maxlength="40" autocomplete="off"></label>
  <label>API key ID <input name="api_key_id" required maxlength="200" autocomplete="off"></label>
  <label>Private key PEM
    <textarea name="private_key" required rows="8" spellcheck="false" autocomplete="off"></textarea>
  </label>
  <button type="submit">Save account</button>
</form>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi accounts</title>
<style>
  body {{ font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 1.5rem; color: #1b1b1b; background: #fafafa; }}
  h1 {{ font-size: 1.4rem; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; }}
  th, td {{ border-bottom: 1px solid #eee; text-align: left; padding: 0.4rem; vertical-align: top; }}
  form {{ margin: 0.4rem 0; }}
  label {{ display: block; margin: 0.6rem 0; }}
  input, textarea {{ width: min(40rem, 100%); font: inherit; }}
  .actions form {{ display: inline; margin-right: 0.4rem; }}
  .note {{ max-width: 46rem; color: #333; }}
  .error {{ color: #8a1f1f; }}
  .flash {{ color: #14532d; }}
  button {{ font: inherit; }}
</style>
</head>
<body>
<p><a href="/">Journal</a> · <a href="/accounts">Accounts</a></p>
<form method="post" action="/logout">
  <input type="hidden" name="csrf_token" value="{token}">
  <button type="submit">Log out</button>
</form>
<h1>Kalshi accounts</h1>
{notice}
{body}
</body>
</html>
"""


def render_login_page(*, csrf_token: str, error: str = "") -> str:
    notice = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    token = html.escape(csrf_token)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard login</title>
<style>
  body {{ font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 2rem; color: #1b1b1b; background: #fafafa; }}
  form {{ max-width: 22rem; }}
  label {{ display: block; margin: 0.7rem 0; }}
  input {{ width: 100%; font: inherit; }}
  .error {{ color: #8a1f1f; }}
</style>
</head>
<body>
<h1>Dashboard login</h1>
{notice}
<form method="post" action="/login" autocomplete="off">
  <input type="hidden" name="csrf_token" value="{token}">
  <label>Password <input type="password" name="password" required autocomplete="current-password"></label>
  <button type="submit">Log in</button>
</form>
</body>
</html>
"""
