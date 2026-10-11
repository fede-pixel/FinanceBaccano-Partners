import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth, config
from app.backup import run_backup
from sqlalchemy import select

from app.db import SessionLocal, init_engine
from app.models import Transaction
from app.routers import accounts, admin, analysis, assistant, banks, contracts, economics, notes, dashboard, invoice_status, planning, settings, upload
from app.services.admin_docs import ensure_checklist, seed_categories
from app.services.recurrence import sync_all

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
log = logging.getLogger("finance")


def bootstrap(db_path=None) -> None:
    config.ensure_dirs()
    init_engine(db_path or config.DB_PATH)
    with SessionLocal() as session:
        seed_categories(session)
        ensure_checklist(session)
        sync_all(session)
        from app.services import categorize

        categorize.seed_rules(session)
        from app.services.fatturapa import fix_self_invoices

        if fix_self_invoices(session):
            session.commit()
        from app.services import contracts

        contracts.seed_dmg(session)
        contracts.seed_october_2026(session)
        contracts.label_dmg(session)
        contracts.sync_all(session)  # le rate partono dal mese corrente: si spostano col passare dei mesi
        session.commit()
        from app.services.util import get_setting, set_setting

        if get_setting(session, "rules_version") != categorize.RULES_VERSION:
            # logica di classificazione aggiornata: ricalcolo le categorie automatiche (mai quelle scelte a mano)
            categorize.apply(session)
            set_setting(session, "rules_version", categorize.RULES_VERSION)
            session.commit()
        elif categorize.apply(session, list(session.scalars(select(Transaction).where(Transaction.category_source == "")))):
            session.commit()


def _sync_banks_job() -> None:
    from app.services.bank_sync import sync_all as sync_banks

    with SessionLocal() as session:
        res = sync_banks(session)
        if res.accounts or res.errors:
            log.info("Open Banking: %s conti, %s nuovi movimenti, errori: %s", res.accounts, res.inserted, res.errors)
        if res.inserted:
            from app.services.refresh import refresh_all

            refresh_all(session)


async def bank_sync_loop() -> None:
    """Sincronizza i conti collegati all'avvio e poi ogni N ore (impostazione eb_sync_hours, 0 = mai).
    PSD2 consente circa 4 accessi al giorno senza l'utente presente: per questo il minimo è 6 ore."""
    from app.services.util import get_setting

    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.to_thread(run_backup)  # backup giornaliero anche se l'app resta accesa per settimane
        except Exception:
            log.exception("Backup fallito")
        with SessionLocal() as session:
            hours = int(get_setting(session, "eb_sync_hours") or 0)
        if hours:
            try:
                await asyncio.to_thread(_sync_banks_job)
            except Exception:  # la sincronizzazione non deve mai fermare l'app
                log.exception("Sincronizzazione Open Banking fallita")
        await asyncio.sleep(max(hours, 6) * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if not getattr(app.state, "bootstrapped", False):
        bootstrap()
        run_backup()
        task = asyncio.create_task(bank_sync_loop())
    yield
    if task:
        task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="Finance & Admin – Baccano Partners", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # Invece di "Internal Server Error": messaggio comprensibile e dettaglio tecnico da inoltrare
        from html import escape

        from fastapi.responses import HTMLResponse

        log.exception("Errore su %s", request.url.path)
        detail = escape(f"{exc.__class__.__name__}: {exc}")[:500]
        return HTMLResponse(
            '<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<link rel="stylesheet" href="/static/css/app.css"><title>Errore</title></head><body><div style="max-width:640px;margin:10vh auto;padding:0 16px">'
            '<div class="card"><h1>Qualcosa è andato storto</h1><p>L\'operazione non è stata completata. I dati già salvati non sono stati toccati.</p>'
            f'<p class="small muted">Dettaglio tecnico (da inoltrare a chi gestisce l\'app): <code>{detail}</code></p>'
            '<p><a class="btn" href="javascript:history.back()">← Torna indietro</a> <a class="btn btn-ghost" href="/">Dashboard</a></p></div></div></body></html>',
            status_code=500,
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if config.ONLINE and not auth.enabled() and request.url.path != "/healthz":
            return PlainTextResponse(
                "App online senza password: imposta la variabile APP_PASSWORD nel pannello di hosting.", status_code=503
            )
        if auth.enabled() and not auth.is_open_path(request.url.path) and not auth.valid_token(request.cookies.get(auth.COOKIE)):
            if request.method == "GET":
                from urllib.parse import quote

                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
            return PlainTextResponse("Sessione scaduta: accedi di nuovo", status_code=401)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    # Dopo ogni modifica andata a buon fine (salvataggi, caricamenti, cancellazioni) si riallineano i dati collegati:
    # ricorrenze, categorie, giroconti, abbinamenti fatture/pagamenti, Excel per il commercialista.
    NO_REFRESH = ("/login", "/logout", "/assistente/chiedi", "/impostazioni/backup", "/apri-cartella", "/pacchetto", "/commercialista")

    @app.middleware("http")
    async def refresh_related(request: Request, call_next):
        response = await call_next(request)
        if request.method == "POST" and response.status_code < 400 and not request.url.path.startswith(NO_REFRESH):
            from app.services.refresh import refresh_all

            def job():
                with SessionLocal() as session:
                    refresh_all(session)

            try:
                await asyncio.to_thread(job)
            except Exception:  # l'aggiornamento non deve mai far fallire la richiesta dell'utente
                log.exception("Aggiornamento dei dati collegati non riuscito")
        return response

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # L'app gira sul tuo computer: blocco i POST provenienti da altri siti aperti nel browser.
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).hostname not in LOCAL_HOSTS | {request.url.hostname}:
                return PlainTextResponse("Richiesta bloccata: origine non locale", status_code=403)
        return await call_next(request)

    for r in (auth, dashboard, upload, accounts, analysis, contracts, notes, assistant, banks, economics, invoice_status, planning, admin, settings):
        app.include_router(r.router)
    return app


app = create_app()
