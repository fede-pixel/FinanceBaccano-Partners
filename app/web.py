"""Template Jinja, filtri e helper condivisi dalle pagine."""
from datetime import date
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app import config
from app.config import BASE_DIR
from app.services.util import MONTHS_IT, WEEKDAYS_IT, fmt_date, fmt_eur, month_label

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
env = templates.env
env.filters["eur"] = fmt_eur
env.filters["eur_sign"] = lambda c: fmt_eur(c, sign=True)
env.filters["d"] = fmt_date
env.filters["wd"] = lambda d: WEEKDAYS_IT[d.weekday()]
def markdown_lite(text: str) -> Markup:
    """Markdown essenziale per le risposte dell'assistente: grassetto, corsivo, codice, titoli, elenchi, tabelle.
    Il testo viene prima reso sicuro (escape), quindi non può iniettare HTML."""
    import re

    def inline(t: str) -> str:
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"<em>\1</em>", t)
        return t

    out, lst, table = [], None, []

    def flush():
        nonlocal lst, table
        if lst:
            out.append(f"</{lst}>")
            lst = None
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            html = ["<div class='table-wrap'><table class='small'>"]
            for i, r in enumerate(rows):
                cells = [c.strip() for c in r.strip().strip("|").split("|")]
                tag = "th" if i == 0 else "td"
                html.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            out.append("".join(html) + "</table></div>")
            table = []

    for raw in str(escape(text or "")).splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            if lst:
                out.append(f"</{lst}>")
                lst = None
            table.append(line)
            continue
        if table:
            flush()
        m_ul = re.match(r"\s*[-*•]\s+(.*)", line)
        m_ol = re.match(r"\s*\d+[.)]\s+(.*)", line)
        m_h = re.match(r"#{1,4}\s+(.*)", line)
        if m_ul or m_ol:
            kind = "ul" if m_ul else "ol"
            if lst != kind:
                flush()
                out.append(f"<{kind}>")
                lst = kind
            out.append(f"<li>{inline((m_ul or m_ol).group(1))}</li>")
            continue
        flush()
        if m_h:
            out.append(f"<h4>{inline(m_h.group(1))}</h4>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush()
    return Markup("".join(out))


env.filters["md"] = markdown_lite
env.filters["cents_input"] = lambda c: "" if c is None else f"{c / 100:.2f}".replace(".", ",")
env.globals["month_label"] = month_label
env.globals["MONTHS_IT"] = MONTHS_IT

NAV = [
    ("Finance", [
        ("/", "Dashboard"),
        ("/carica", "Caricamento settimanale"),
        ("/forecast", "Forecast"),
        ("/kpi", "KPI economici"),
        ("/investimenti", "Capacità di investimento"),
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
    ("", [("/verifica", "Verifica dati"), ("/assistente", "Assistente AI"), ("/impostazioni", "Impostazioni"), ("/banche", "Open Banking (opzionale)")]),
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
