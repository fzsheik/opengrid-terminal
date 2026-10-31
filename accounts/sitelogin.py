"""Site login: a signed session cookie behind a styled /login form, instead of the browser's basic-auth popup.

The cookie is "<expiry>.<HMAC>" keyed from APP_USER + APP_PASSWORD, so changing the password signs everyone out.
Basic auth still works for scripts. One shared operator login: no per-user accounts (use API keys for those).
"""
import hashlib
import hmac
import html
import time

from config import settings

COOKIE = "og_session"
MAX_AGE = 7 * 86400


def _sign(exp: str) -> str:
    key = hashlib.sha256(f"opengrid-site-session|{settings.app_user}|{settings.app_password}".encode()).digest()
    return hmac.new(key, exp.encode(), "sha256").hexdigest()


def make_token() -> str:
    exp = str(int(time.time()) + MAX_AGE)
    return f"{exp}.{_sign(exp)}"


def valid(request) -> bool:
    if not settings.app_password:
        return False
    exp, _, sig = request.cookies.get(COOKIE, "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in · OpenGrid Terminal</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%23131920'/%3E%3Cpath d='M6 6h12M6 12h12M6 18h12M6 6v12M12 6v12M18 6v12' stroke='%234d94ff' stroke-width='1.6' fill='none'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;background:#090b0e;
  color:#e7ebef;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background-image:radial-gradient(ellipse at 50% -10%,#14233a 0,#090b0e 60%)}
main{width:100%;max-width:360px}
.brand{display:flex;align-items:center;gap:10px;justify-content:center;margin-bottom:28px;font-size:17px;font-weight:600;letter-spacing:.2px}
.brand svg{width:28px;height:28px}
form{background:#0e1217;border:1px solid #1a2129;border-radius:10px;padding:26px 24px;box-shadow:0 10px 40px rgba(0,0,0,.45)}
h1{margin:0 0 4px;font-size:18px;font-weight:600}
p.sub{margin:0 0 20px;color:#929da9;font-size:13px}
label{display:block;margin:0 0 6px;color:#b9c2cc;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.8px}
input{width:100%;margin:0 0 16px;padding:10px 12px;background:#090b0e;color:#e7ebef;border:1px solid #242d37;border-radius:6px;font:inherit;outline:none}
input:focus{border-color:#4d94ff;box-shadow:0 0 0 3px rgba(77,148,255,.18)}
button{width:100%;padding:10px 12px;background:#2a78d6;color:#fff;border:0;border-radius:6px;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#4d94ff}
.err{margin:0 0 16px;padding:9px 12px;background:rgba(239,83,80,.1);border:1px solid rgba(239,83,80,.35);border-radius:6px;color:#ef9a98;font-size:13px}
.foot{margin-top:18px;text-align:center;color:#7a8591;font-size:12px}
.foot a{color:#929da9;text-decoration:none}.foot a:hover{color:#e7ebef}
</style></head><body><main>
<div class="brand"><svg viewBox="0 0 24 24"><rect width="24" height="24" rx="5" fill="#131920"/><path d="M6 6h12M6 12h12M6 18h12M6 6v12M12 6v12M18 6v12" stroke="#4d94ff" stroke-width="1.6" fill="none"/></svg>OpenGrid Terminal</div>
<form method="post" action="/login">
<h1>Sign in</h1><p class="sub">Access to the terminal is restricted.</p>
{error}<input type="hidden" name="next" value="{next}">
<label for="u">Username</label><input id="u" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form>
<div class="foot"><a href="https://tryopengrid.com">&larr; Back to tryopengrid.com</a></div>
</main></body></html>"""


def page(next_path: str, error: str = "") -> str:
    err = f'<div class="err" role="alert">{html.escape(error)}</div>' if error else ""
    return _PAGE.replace("{error}", err).replace("{next}", html.escape(next_path, quote=True))
