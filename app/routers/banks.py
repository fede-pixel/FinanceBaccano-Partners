"""Pagine Open Banking: configurazione Enable Banking, collegamento banche, sincronizzazione."""
import secrets
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import get_session
from app.models import Account, BankConnection
from app.services import bank_sync
from app.services.enablebanking import EnableBankingError, account_iban, account_label
from app.services.util import get_setting, set_setting
from app.web import redirect, render

router = APIRouter()
COUNTRIES = {"IT": "Italia", "DE": "Germania", "LT": "Lituania", "LU": "Lussemburgo", "FR": "Francia", "ES": "Spagna", "NL": "Paesi Bassi", "IE": "Irlanda"}
_aspsp_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def default_redirect_url() -> str:
    return f"http://127.0.0.1:{config.PORT}/banche/callback"


def _404():
    raise HTTPException(404, "Non trovato")


def _aspsps(session: Session, country: str, psu_type: str) -> list[dict]:
    key = (country, psu_type)
    cached = _aspsp_cache.get(key)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    items = sorted(bank_sync.make_client(session).aspsps(country, psu_type), key=lambda a: a.get("name", "").lower())
    _aspsp_cache[key] = (time.time(), items)
    return items


@router.get("/banche")
def banks(request: Request, paese: str = "IT", tipo: str = "business", session: Session = Depends(get_session)):
    configured = bank_sync.is_configured(session)
    aspsps, aspsp_error = [], ""
    if configured:
        try:
            aspsps = _aspsps(session, paese, tipo)
        except EnableBankingError as exc:
            aspsp_error = str(exc)
    conns = list(session.scalars(select(BankConnection).order_by(BankConnection.created_at.desc())))
    return render(
        request, "banks.html", configured=configured, aspsps=aspsps, aspsp_error=aspsp_error, conns=conns,
        paese=paese, tipo=tipo, countries=COUNTRIES, now=datetime.now(),
        app_id=get_setting(session, "eb_application_id"), key_path=get_setting(session, "eb_key_path"),
        redirect_url=get_setting(session, "eb_redirect_url") or default_redirect_url(),
        default_redirect=default_redirect_url(),
        sync_hours=get_setting(session, "eb_sync_hours"), initial_days=get_setting(session, "eb_initial_days"),
    )


@router.post("/banche/configurazione")
async def banks_config(
    application_id: str = Form(""),
    key_file: UploadFile | None = File(None),
    redirect_url: str = Form(""),
    sync_hours: str = Form("6"),
    initial_days: str = Form("90"),
    session: Session = Depends(get_session),
):
    application_id = application_id.strip()
    if application_id:
        set_setting(session, "eb_application_id", application_id)
    if key_file is not None and key_file.filename:
        content = await key_file.read()
        if b"PRIVATE KEY" not in content:
            return redirect("/banche", err="Il file caricato non sembra una chiave privata (.pem)")
        bank_sync.KEYS_DIR.mkdir(parents=True, exist_ok=True)
        path = bank_sync.KEYS_DIR / f"{application_id or 'enablebanking'}.pem"
        path.write_bytes(content)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        set_setting(session, "eb_key_path", str(path))
    set_setting(session, "eb_redirect_url", redirect_url.strip())
    if sync_hours.strip().isdigit():
        set_setting(session, "eb_sync_hours", str(int(sync_hours) if int(sync_hours) == 0 else max(6, int(sync_hours))))
    if initial_days.strip().isdigit():
        set_setting(session, "eb_initial_days", str(min(730, int(initial_days))))
    session.commit()
    _aspsp_cache.clear()
    if not bank_sync.is_configured(session):
        return redirect("/banche", msg="Salvato. Mancano ancora Application ID o chiave privata.")
    try:
        app_info = bank_sync.make_client(session).application()
    except EnableBankingError as exc:
        return redirect("/banche", err=f"Configurazione salvata ma la verifica è fallita: {exc}")
    name = app_info.get("name") or application_id
    env = app_info.get("environment") or ""
    return redirect("/banche", msg=f"Collegamento a Enable Banking riuscito: applicazione '{name}' {env}".strip())


@router.post("/banche/collega")
def banks_connect(
    aspsp: str = Form(...),
    country: str = Form("IT"),
    psu_type: str = Form("business"),
    connection_id: int = Form(0),
    session: Session = Depends(get_session),
):
    conn = session.get(BankConnection, connection_id) if connection_id else None
    if conn is None:
        conn = BankConnection(aspsp_name=aspsp, aspsp_country=country, psu_type=psu_type)
        session.add(conn)
    conn.state = secrets.token_urlsafe(24)
    if conn.status != "attivo":
        conn.status = "in_attesa"
    session.flush()
    days = 180
    try:
        for a in _aspsps(session, conn.aspsp_country, conn.psu_type):
            if a.get("name") == conn.aspsp_name and a.get("maximum_consent_validity"):
                days = max(1, min(180, int(a["maximum_consent_validity"]) // 86400))
        auth = bank_sync.make_client(session).start_auth(
            conn.aspsp_name, conn.aspsp_country, get_setting(session, "eb_redirect_url") or default_redirect_url(),
            conn.state, conn.psu_type, days,
        )
    except EnableBankingError as exc:
        session.rollback()
        return redirect("/banche", err=str(exc))
    session.commit()
    return RedirectResponse(auth["url"], status_code=303)


def _complete(session: Session, code: str, state: str) -> RedirectResponse:
    conn = session.scalar(select(BankConnection).where(BankConnection.state == state)) if state else None
    if conn is None:
        return redirect("/banche", err="Autorizzazione non riconosciuta (state non valido o già usato): riprova a collegare la banca")
    try:
        data = bank_sync.make_client(session).create_session(code)
    except EnableBankingError as exc:
        conn.status, conn.last_error = "errore", str(exc)
        session.commit()
        return redirect("/banche", err=str(exc))
    bank_sync.store_session(session, conn, data)
    return redirect(f"/banche/{conn.id}/conti", msg=f"{conn.aspsp_name} autorizzata. Ora scegli come collegare i conti.")


@router.get("/banche/callback")
def banks_callback(code: str = "", state: str = "", error: str = "", error_description: str = "", session: Session = Depends(get_session)):
    if error or not code:
        return redirect("/banche", err=f"La banca non ha autorizzato l'accesso: {error_description or error or 'codice mancante'}")
    return _complete(session, code, state)


@router.post("/banche/codice")
def banks_manual_code(url: str = Form(...), session: Session = Depends(get_session)):
    """Se il redirect non punta a questa app, l'utente incolla l'indirizzo finale dal browser."""
    qs = parse_qs(urlparse(url.strip()).query)
    code, state = qs.get("code", [""])[0], qs.get("state", [""])[0]
    if not code:
        return redirect("/banche", err="Nell'indirizzo incollato non trovo il parametro code=")
    return _complete(session, code, state)


@router.get("/banche/{conn_id}/conti")
def banks_accounts(conn_id: int, request: Request, session: Session = Depends(get_session)):
    conn = session.get(BankConnection, conn_id) or _404()
    remote = bank_sync.session_accounts(conn)
    locals_ = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    linked = {a.external_uid: a for a in conn.accounts if a.external_uid}
    rows = []
    for acc in remote:
        uid = acc.get("uid", "")
        current = linked.get(uid)
        if current is None:  # proposta: stesso IBAN o stesso nome
            iban = account_iban(acc)
            current = next((a for a in locals_ if iban and a.iban == iban), None)
        rows.append({"uid": uid, "label": account_label(acc), "currency": acc.get("currency", "EUR"),
                     "type": acc.get("cash_account_type", ""), "current": current, "raw": acc})
    return render(request, "bank_accounts.html", conn=conn, rows=rows, locals_=locals_)


@router.post("/banche/{conn_id}/conti")
async def banks_accounts_save(conn_id: int, request: Request, session: Session = Depends(get_session)):
    conn = session.get(BankConnection, conn_id) or _404()
    form = await request.form()
    remote = {a.get("uid"): a for a in bank_sync.session_accounts(conn)}
    # riparto da zero: vale la scelta fatta in questa pagina
    for acc in list(conn.accounts):
        acc.connection, acc.external_uid = None, ""
    session.flush()
    linked = 0
    for uid, raw in remote.items():
        choice = str(form.get(f"map_{uid}", "ignora"))
        if choice == "ignora":
            continue
        if choice == "nuovo":
            acc = Account(
                name=(raw.get("name") or raw.get("product") or conn.aspsp_name)[:100],
                bank=conn.aspsp_name,
                kind="carta" if raw.get("cash_account_type") == "CARD" else "conto",
                currency=(raw.get("currency") or "EUR")[:3],
                iban=account_iban(raw),
            )
            session.add(acc)
        else:
            acc = session.get(Account, int(choice))
            if acc is None:
                continue
        acc.connection = conn
        acc.external_uid = uid
        linked += 1
    session.commit()
    if not linked:
        return redirect(f"/banche/{conn_id}/conti", msg="Nessun conto collegato")
    try:
        res = bank_sync.sync_connection(session, conn)
    except EnableBankingError as exc:
        return redirect("/banche", err=f"Conti collegati, ma la prima sincronizzazione è fallita: {exc}")
    return redirect("/banche", msg=f"Collegati {linked} conti · scaricati {res.inserted} movimenti", err=" · ".join(res.errors))


@router.post("/banche/{conn_id}/sincronizza")
def banks_sync_one(conn_id: int, session: Session = Depends(get_session)):
    conn = session.get(BankConnection, conn_id) or _404()
    try:
        res = bank_sync.sync_connection(session, conn)
    except EnableBankingError as exc:
        return redirect("/banche", err=str(exc))
    return redirect("/banche", msg=f"{conn.aspsp_name}: {res.inserted} nuovi movimenti", err=" · ".join(res.errors))


@router.post("/banche/sincronizza")
def banks_sync_all(back: str = Form("/banche"), session: Session = Depends(get_session)):
    res = bank_sync.sync_all(session)
    back = back if back.startswith("/") else "/banche"
    return redirect(back, msg=f"Sincronizzati {res.accounts} conti · {res.inserted} nuovi movimenti", err=" · ".join(res.errors))


@router.post("/banche/{conn_id}/elimina")
def banks_delete(conn_id: int, session: Session = Depends(get_session)):
    conn = session.get(BankConnection, conn_id) or _404()
    if conn.session_id and bank_sync.is_configured(session):
        try:
            bank_sync.make_client(session).delete_session(conn.session_id)
        except EnableBankingError:
            pass  # la sessione potrebbe essere già scaduta
    for acc in conn.accounts:
        acc.external_uid = ""
    session.delete(conn)
    session.commit()
    return redirect("/banche", msg="Collegamento rimosso. I conti e i movimenti restano.")
