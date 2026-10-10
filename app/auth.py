"""Accesso con password (obbligatorio online, facoltativo in locale).

Il cookie di sessione è firmato con HMAC: contiene solo la scadenza, nessun dato personale.
Dopo 5 tentativi errati dallo stesso indirizzo il login si blocca per 15 minuti."""
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config

COOKIE = "fbp_session"
SESSION_DAYS = 30
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
OPEN_PATHS = ("/login", "/static/", "/healthz")
_fails: dict[str, tuple[int, float]] = {}
router = APIRouter()


def enabled() -> bool:
    return bool(config.APP_PASSWORD)


def _secret() -> bytes:
    if config.SESSION_SECRET:
        return config.SESSION_SECRET.encode()
    path = config.DATA_DIR / ".session_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip().encode()


def make_token(now: float | None = None) -> str:
    expires = str(int((time.time() if now is None else now) + SESSION_DAYS * 86400))
    sig = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    good = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


def is_open_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in OPEN_PATHS)


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


LOGIN_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Accesso · Baccano Partners</title>
<link rel="stylesheet" href="/static/css/app.css"></head>
<body><div style="max-width:360px;margin:12vh auto;padding:0 16px">
<div class="card"><div class="brand" style="padding:0 0 14px">Baccano Partners<span>Finance &amp; Administration</span></div>
{error}<form method="post" action="/login"><input type="hidden" name="next" value="{next}">
<div class="field"><label>Password</label><input type="password" name="password" autofocus required style="width:100%"></div>
<button style="width:100%">Entra</button></form></div></div></body></html>"""


def _page(error: str = "", next_url: str = "/", status: int = 200) -> HTMLResponse:
    from html import escape

    err = f'<div class="alert err small">{escape(error)}</div>' if error else ""
    return HTMLResponse(LOGIN_PAGE.format(error=err, next=escape(next_url, quote=True)), status_code=status)


def _safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


@router.get("/login")
def login_form(next: str = "/"):
    return _page(next_url=_safe_next(next))


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    ip = _client(request)
    fails, until = _fails.get(ip, (0, 0.0))
    if until > time.time():
        return _page("Troppi tentativi errati: riprova tra qualche minuto.", status=429)
    if not enabled() or not hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode()):
        fails += 1
        _fails[ip] = (fails, time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0.0)
        time.sleep(0.5)
        return _page("Password errata.", _safe_next(next), status=401)
    _fails.pop(ip, None)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        COOKIE, make_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
