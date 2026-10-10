import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.backup import run_backup
from app.db import SessionLocal, init_engine
from app.routers import accounts, admin, banks, dashboard, planning, settings, upload
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


def _sync_banks_job() -> None:
    from app.services.bank_sync import sync_all as sync_banks

    with SessionLocal() as session:
        res = sync_banks(session)
        if res.accounts or res.errors:
            log.info("Open Banking: %s conti, %s nuovi movimenti, errori: %s", res.accounts, res.inserted, res.errors)


async def bank_sync_loop() -> None:
    """Sincronizza i conti collegati all'avvio e poi ogni N ore (impostazione eb_sync_hours, 0 = mai).
    PSD2 consente circa 4 accessi al giorno senza l'utente presente: per questo il minimo è 6 ore."""
    from app.services.util import get_setting

    await asyncio.sleep(10)
    while True:
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

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # L'app gira sul tuo computer: blocco i POST provenienti da altri siti aperti nel browser.
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).hostname not in LOCAL_HOSTS | {request.url.hostname}:
                return PlainTextResponse("Richiesta bloccata: origine non locale", status_code=403)
        return await call_next(request)

    for r in (dashboard, upload, accounts, banks, planning, admin, settings):
        app.include_router(r.router)
    return app


app = create_app()
