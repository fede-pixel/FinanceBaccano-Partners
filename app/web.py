"""Template Jinja, filtri e helper condivisi dalle pagine."""
from datetime import date
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.config import BASE_DIR
from app.services.util import MONTHS_IT, WEEKDAYS_IT, fmt_date, fmt_eur, month_label

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
env = templates.env
env.filters["eur"] = fmt_eur
env.filters["eur_sign"] = lambda c: fmt_eur(c, sign=True)
env.filters["d"] = fmt_date
env.filters["wd"] = lambda d: WEEKDAYS_IT[d.weekday()]
env.filters["cents_input"] = lambda c: "" if c is None else f"{c / 100:.2f}".replace(".", ",")
env.globals["month_label"] = month_label
env.globals["MONTHS_IT"] = MONTHS_IT

NAV = [
    ("Finance", [
        ("/", "Dashboard"),
        ("/carica", "Caricamento settimanale"),
        ("/forecast", "Forecast"),
        ("/analisi", "Analisi spese"),
        ("/previsti", "Entrate e uscite previste"),
        ("/movimenti", "Movimenti"),
        ("/riconciliazione", "Riconciliazione"),
        ("/fatture", "Fatture (Aruba)"),
        ("/fatture/situazione", "Fatture da saldare"),
        ("/ricorrenze", "Ricorrenze"),
        ("/conti", "Conti e carte"),
    ]),
    ("Administration", [
        ("/documenti", "Scadenzario documenti"),
        ("/pacchetto", "Pacchetto commercialista"),
    ]),
    ("", [("/impostazioni", "Impostazioni"), ("/banche", "Open Banking (opzionale)")]),
]
env.globals["NAV"] = NAV


def active_nav(path: str) -> str:
    """La voce di menu più specifica che corrisponde alla pagina (es. /fatture/situazione e non /fatture)."""
    hrefs = [h for _, links in NAV for h, _ in links if h == path or (h != "/" and path.startswith(h))]
    return max(hrefs, key=len) if hrefs else ""


env.globals["active_nav"] = active_nav
env.globals["ONLINE"] = config.ONLINE
env.globals["auth_enabled"] = lambda: bool(config.APP_PASSWORD)


def render(request: Request, name: str, **ctx):
    ctx.setdefault("today", date.today())
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url, status_code=303)
