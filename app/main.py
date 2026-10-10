from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.backup import run_backup
from app.db import SessionLocal, init_engine
from app.routers import accounts, admin, dashboard, planning, settings
from app.services.admin_docs import ensure_checklist, seed_categories
from app.services.recurrence import sync_all

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def bootstrap(db_path=None) -> None:
    config.ensure_dirs()
    init_engine(db_path or config.DB_PATH)
    with SessionLocal() as session:
        seed_categories(session)
        ensure_checklist(session)
        sync_all(session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not getattr(app.state, "bootstrapped", False):
        bootstrap()
        run_backup()
    yield


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

    for r in (dashboard, accounts, planning, admin, settings):
        app.include_router(r.router)
    return app


app = create_app()
