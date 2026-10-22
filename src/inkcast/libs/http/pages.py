# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The few HTML pages Inkcast serves: sign-in, consent, connect a platform, create a token, delete the account."""

from __future__ import annotations

from html import escape
from typing import Iterable

from starlette.responses import HTMLResponse

_CSS = """
:root{--bg:#f7f5f0;--fg:#1d1b18;--muted:#6b665d;--card:#fff;--line:#e3ded3;--accent:#c2410c;--accent-fg:#fff;--err:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#141210;--fg:#f2efe9;--muted:#a49d90;--card:#1f1c19;--line:#34302a;--accent:#fb923c;--accent-fg:#1d1b18;--err:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 ui-sans-serif,system-ui,sans-serif}
main{max-width:460px;margin:8vh auto;padding:0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
.brand{font-weight:700;letter-spacing:.02em;color:var(--accent);margin-bottom:18px}h1{font-size:1.35rem;margin:0 0 8px}
p{margin:.5rem 0}.muted{color:var(--muted);font-size:.92rem}code{font-size:.9em;background:var(--bg);padding:1px 5px;border-radius:5px}
label{display:block;font-weight:600;margin:16px 0 6px}input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:flex;gap:10px;margin-top:22px}button{flex:1;padding:11px;border-radius:9px;border:1px solid var(--line);background:transparent;color:var(--fg);font:inherit;font-weight:600;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}.err{color:var(--err);font-weight:600}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (
        f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} · Inkcast</title><style>{_CSS}</style></head>"
        f"<body><main><div class=card><div class=brand>Inkcast</div>{body}</div></main></body></html>"
    )
    return HTMLResponse(html, status_code=status, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' http: https:; frame-ancestors 'none'",
    })


def message(title: str, text: str, *, error: bool = False, status: int = 200) -> HTMLResponse:
    cls = "err" if error else ""
    return _page(title, f"<h1>{escape(title)}</h1><p class='{cls}'>{escape(text)}</p>", status)


def password_form(pending_id: str, error: str | None = None) -> HTMLResponse:
    err = f"<p class=err>{escape(error)}</p>" if error else ""
    return _page("Sign in", f"""
<h1>Sign in</h1>{err}
<form method=post action=/auth/callback>
<input type=hidden name=pending value="{escape(pending_id)}">
<label for=pw>Password</label><input id=pw type=password name=password autocomplete=current-password autofocus required>
<div class=row><button class=primary type=submit>Continue</button></div></form>""")


def consent(pending_id: str, csrf: str, client_name: str, redirect_host: str, user_display: str) -> HTMLResponse:
    return _page("Allow access", f"""
<h1>Allow {escape(client_name)} to use Inkcast?</h1>
<p>Signed in as <strong>{escape(user_display)}</strong>.</p>
<p>It will be able to publish, schedule and cancel stories on the platforms you connect.</p>
<p class=muted>Approving sends you back to <code>{escape(redirect_host)}</code>. Only approve if you just started this
from your own tool (e.g. <code>/mcp</code> in Claude Code).</p>
<form method=post action=/auth/consent>
<input type=hidden name=pending value="{escape(pending_id)}"><input type=hidden name=csrf value="{escape(csrf)}">
<div class=row><button name=decision value=deny>Deny</button><button class=primary name=decision value=approve>Approve</button></div>
</form>""")


def connect_form(ticket: str, platform_name: str, help_text: str, fields: Iterable, error: str | None = None) -> HTMLResponse:
    err = f"<p class=err>{escape(error)}</p>" if error else ""
    inputs = []
    for f in fields:
        kind = "password" if f.secret else "text"
        hint = f"<p class=muted>{escape(f.help)}</p>" if f.help else ""
        req = " required" if f.required else ""
        inputs.append(
            f"<label for='{escape(f.key)}'>{escape(f.label)}</label>{hint}"
            f"<input id='{escape(f.key)}' name='{escape(f.key)}' type={kind} autocomplete=off placeholder='{escape(f.placeholder)}'{req}>"
        )
    return _page(f"Connect {platform_name}", f"""
<h1>Connect {escape(platform_name)}</h1>{err}<p class=muted>{escape(help_text)}</p>
<form method=post action="/connect/{escape(ticket)}">{''.join(inputs)}
<div class=row><button class=primary type=submit>Connect</button></div></form>
<p class=muted>Credentials are encrypted on the Inkcast server and are never shown to the AI assistant.</p>""")


def token_confirm(pending_id: str, csrf: str, name: str, scopes: list[str], days: int | None,
                  user_display: str) -> HTMLResponse:
    items = "".join(f"<li><code>{escape(s)}</code></li>" for s in scopes)
    expiry = f"in {days} days" if days else "never (revoke it when you no longer need it)"
    return _page("Create a token", f"""
<h1>Create a personal access token?</h1>
<p>Signed in as <strong>{escape(user_display)}</strong>.</p>
<p>Name: <strong>{escape(name)}</strong><br>Expires: {escape(expiry)}</p>
<p>It will be able to:</p><ul>{items}</ul>
<p class=muted>Use it in CI (for example the Inkcast GitHub Action) as a secret named <code>INKCAST_TOKEN</code>.
Only create it if you asked for it just now.</p>
<form method=post action=/auth/consent>
<input type=hidden name=pending value="{escape(pending_id)}"><input type=hidden name=csrf value="{escape(csrf)}">
<div class=row><button name=decision value=deny>Cancel</button><button class=primary name=decision value=approve>Create token</button></div>
</form>""")


def token_created(token: str, name: str) -> HTMLResponse:
    return _page("Token created", f"""
<h1>Token created</h1>
<p>Copy <strong>{escape(name)}</strong> now; it won't be shown again.</p>
<p><code id=token style="word-break:break-all;display:block;padding:10px">{escape(token)}</code></p>
<p class=muted>In GitHub: Settings → Secrets and variables → Actions → New repository secret, name
<code>INKCAST_TOKEN</code>. Don't paste it into a chat with an AI assistant.</p>""")


def deletion_confirm(pending_id: str, csrf: str, user_display: str) -> HTMLResponse:
    """What goes, what stays, and Continue to Keycloak (E9.F9.5.S4). The export (E3.F3.3.S7) is not built yet."""
    return _page("Delete your account", f"""
<h1>Delete your Inkcast account?</h1>
<p>Signed in as <strong>{escape(user_display)}</strong>.</p>
<p><strong>What goes</strong>, for good: your posts and schedules (a scheduled post can no longer go out), the Git
publishing ledger, your connected platforms and the credentials stored for them, your OAuth grants and personal
access tokens, and your usage numbers.</p>
<p><strong>What stays:</strong> articles already published on the platforms. Inkcast never deletes anything on
DEV, Medium or any other platform; remove those there if you want them gone.</p>
<p class=muted>Export my data (a copy of everything above) is coming soon; if you want a copy, wait for it before
deleting.</p>
<p>Continue takes you to the sign-in provider, which asks you to sign in once more and to confirm. It deletes your
account there; Inkcast then erases your data here.</p>
<form method=post action=/auth/consent>
<input type=hidden name=pending value="{escape(pending_id)}"><input type=hidden name=csrf value="{escape(csrf)}">
<div class=row><button name=decision value=deny>Keep my account</button><button class=primary name=decision value=approve>Continue</button></div>
</form>""")
