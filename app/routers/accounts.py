import hashlib
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.db import get_session
from app.models import ACCOUNT_KINDS, Account, BalanceSnapshot, Transaction
from app.services.bank_import import ImportProfile, guess_profile, import_transactions, parse_rows, read_table
from app.services.forecast import account_statuses
from app.services.util import month_bounds, parse_amount, parse_date
from app.web import redirect, render

router = APIRouter()


# ---------------- conti ----------------

@router.get("/conti")
def accounts_list(request: Request, session: Session = Depends(get_session)):
    statuses = account_statuses(session, date.today())
    inactive = list(session.scalars(select(Account).where(~Account.active)))
    return render(request, "accounts.html", statuses=statuses, inactive=inactive, kinds=ACCOUNT_KINDS)


@router.get("/conti/nuovo")
def account_new(request: Request):
    return render(request, "account_form.html", account=None, kinds=ACCOUNT_KINDS)


@router.get("/conti/{account_id}")
def account_edit(account_id: int, request: Request, session: Session = Depends(get_session)):
    account = session.get(Account, account_id) or _404()
    return render(request, "account_form.html", account=account, kinds=ACCOUNT_KINDS)


def _404():
    raise HTTPException(404, "Non trovato")


@router.post("/conti/salva")
def account_save(
    account_id: int = Form(0),
    name: str = Form(...),
    bank: str = Form(""),
    kind: str = Form("conto"),
    currency: str = Form("EUR"),
    iban: str = Form(""),
    color: str = Form("#2a78d6"),
    include_in_forecast: str = Form(""),
    active: str = Form(""),
    notes: str = Form(""),
    opening_balance: str = Form(""),
    opening_date: str = Form(""),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id) if account_id else Account()
    if account is None:
        _404()
    account.name, account.bank, account.kind = name.strip(), bank.strip(), kind
    account.currency = (currency or "EUR").upper()[:3]
    account.iban, account.color, account.notes = iban.replace(" ", "").upper(), color, notes
    account.include_in_forecast = bool(include_in_forecast)
    account.active = bool(active) if account_id else True
    session.add(account)
    session.flush()
    if not account_id:
        bal = parse_amount(opening_balance) or 0
        d = parse_date(opening_date) or date.today()
        session.add(BalanceSnapshot(account_id=account.id, date=d, balance_cents=bal, source="apertura"))
    session.commit()
    return redirect("/conti", msg=f"Conto '{account.name}' salvato")


@router.post("/conti/{account_id}/saldo")
def account_snapshot(
    account_id: int,
    balance: str = Form(...),
    snap_date: str = Form(""),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id) or _404()
    cents = parse_amount(balance)
    d = parse_date(snap_date) or date.today()
    if cents is None:
        return redirect(f"/conti/{account_id}", err="Importo non valido")
    row = session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == d))
    if row:
        row.balance_cents, row.source = cents, "manuale"
    else:
        session.add(BalanceSnapshot(account_id=account.id, date=d, balance_cents=cents, source="manuale"))
    session.commit()
    return redirect(f"/conti/{account_id}", msg="Saldo registrato")


@router.post("/conti/{account_id}/saldo/{snap_id}/elimina")
def account_snapshot_delete(account_id: int, snap_id: int, session: Session = Depends(get_session)):
    snap = session.get(BalanceSnapshot, snap_id)
    if snap and snap.account_id == account_id:
        session.delete(snap)
        session.commit()
    return redirect(f"/conti/{account_id}", msg="Saldo eliminato")


@router.post("/conti/{account_id}/elimina")
def account_delete(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id) or _404()
    n = session.scalar(select(func.count()).select_from(Transaction).where(Transaction.account_id == account_id))
    if n:
        account.active = False
        session.commit()
        return redirect("/conti", msg=f"'{account.name}' ha {n} movimenti: l'ho disattivato invece di eliminarlo")
    session.delete(account)
    session.commit()
    return redirect("/conti", msg="Conto eliminato")


# ---------------- movimenti ----------------

@router.get("/movimenti")
def movements(
    request: Request,
    conto: int = 0,
    mese: str = "",
    q: str = "",
    da_abbinare: int = 0,
    session: Session = Depends(get_session),
):
    stmt = select(Transaction).order_by(Transaction.date.desc(), Transaction.id.desc())
    if conto:
        stmt = stmt.where(Transaction.account_id == conto)
    if mese:
        try:
            y, m = (int(x) for x in mese.split("-"))
            s, e = month_bounds(y, m)
            stmt = stmt.where(Transaction.date >= s, Transaction.date <= e)
        except ValueError:
            pass
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Transaction.description.ilike(like) | Transaction.counterparty.ilike(like) | Transaction.category.ilike(like))
    if da_abbinare:
        stmt = stmt.where(Transaction.planned_item_id.is_(None))
    rows = list(session.scalars(stmt.limit(1000)))
    accounts = list(session.scalars(select(Account).order_by(Account.active.desc(), Account.id)))
    tot_in = sum(t.amount_cents for t in rows if t.amount_cents > 0)
    tot_out = sum(t.amount_cents for t in rows if t.amount_cents < 0)
    return render(
        request, "movements.html", rows=rows, accounts=accounts, conto=conto, mese=mese, q=q,
        da_abbinare=da_abbinare, tot_in=tot_in, tot_out=tot_out,
    )


@router.post("/movimenti/nuovo")
def movement_add(
    account_id: int = Form(...),
    tx_date: str = Form(...),
    amount: str = Form(...),
    direction: str = Form("uscita"),
    description: str = Form(""),
    counterparty: str = Form(""),
    category: str = Form(""),
    session: Session = Depends(get_session),
):
    cents = parse_amount(amount)
    d = parse_date(tx_date)
    if cents is None or d is None:
        return redirect("/movimenti", err="Data o importo non validi")
    cents = abs(cents) if direction == "entrata" else -abs(cents)
    key = hashlib.sha256(f"manuale|{uuid.uuid4()}".encode()).hexdigest()[:40]
    session.add(
        Transaction(
            account_id=account_id, date=d, amount_cents=cents, description=description.strip(),
            counterparty=counterparty.strip(), category=category.strip(), source="manuale", dedupe_key=key,
        )
    )
    session.commit()
    return redirect(f"/movimenti?conto={account_id}", msg="Movimento registrato")


@router.post("/movimenti/{tx_id}/categoria")
def movement_category(tx_id: int, category: str = Form(""), session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id) or _404()
    tx.category = category.strip()
    session.commit()
    return redirect(f"/movimenti?conto={tx.account_id}")


@router.post("/movimenti/{tx_id}/elimina")
def movement_delete(tx_id: int, session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id) or _404()
    acc = tx.account_id
    if tx.planned_item_id:
        from app.services.reconcile import unmatch

        unmatch(session, tx)
    session.delete(tx)
    session.commit()
    return redirect(f"/movimenti?conto={acc}", msg="Movimento eliminato")


@router.post("/movimenti/import/{batch}/annulla")
def import_undo(batch: str, session: Session = Depends(get_session)):
    rows = list(session.scalars(select(Transaction).where(Transaction.import_batch == batch)))
    for tx in rows:
        if tx.planned_item_id:
            from app.services.reconcile import unmatch

            unmatch(session, tx)
        session.delete(tx)
    session.commit()
    return redirect("/movimenti", msg=f"Annullato l'import: eliminati {len(rows)} movimenti")


# ---------------- import da file ----------------

@router.get("/movimenti/import")
def import_form(request: Request, conto: int = 0, session: Session = Depends(get_session)):
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    return render(request, "import_upload.html", accounts=accounts, conto=conto)


@router.post("/movimenti/import")
async def import_upload(account_id: int = Form(...), file: UploadFile = File(...), session: Session = Depends(get_session)):
    content = await file.read()
    if not content:
        return redirect("/movimenti/import", err="File vuoto")
    token = uuid.uuid4().hex
    suffix = Path(file.filename or "file.csv").suffix.lower() or ".csv"
    (config.IMPORTS_DIR / f"{token}{suffix}").write_bytes(content)
    return redirect(f"/movimenti/import/{token}{suffix}?conto={account_id}")


def _load_rows(token: str) -> list[list[str]]:
    path = config.IMPORTS_DIR / Path(token).name
    if not path.exists():
        raise HTTPException(404, "File di import non trovato: ricaricalo")
    return read_table(path.read_bytes(), path.name)


def _profile_from_form(form) -> ImportProfile:
    return ImportProfile(
        header_row=int(form.get("header_row") or 0),
        date_col=form.get("date_col", ""),
        date_format=form.get("date_format", "auto"),
        amount_mode=form.get("amount_mode", "single"),
        amount_col=form.get("amount_col", ""),
        in_col=form.get("in_col", ""),
        out_col=form.get("out_col", ""),
        desc_cols=[c for c in form.getlist("desc_cols") if c],
        counterparty_col=form.get("counterparty_col", ""),
        id_col=form.get("id_col", ""),
        balance_col=form.get("balance_col", ""),
        status_col=form.get("status_col", ""),
        status_ok=form.get("status_ok", ""),
        decimal_sep=form.get("decimal_sep", "auto"),
        invert_sign=bool(form.get("invert_sign")),
    )


@router.api_route("/movimenti/import/{token}", methods=["GET", "POST"])
async def import_preview(token: str, request: Request, conto: int = 0, session: Session = Depends(get_session)):
    rows = _load_rows(token)
    account = session.get(Account, conto) or _404()
    if request.method == "POST":
        form = await request.form()
        profile = _profile_from_form(form)
    else:
        saved = ImportProfile.from_json(account.csv_profile)
        profile = saved if saved and saved.header_row < len(rows) else guess_profile(rows)
    header = rows[profile.header_row] if rows else []
    parsed, errors = parse_rows(rows, profile) if profile.date_col else ([], ["Scegli la colonna della data"])
    return render(
        request, "import_preview.html", token=token, account=account, rows=rows[:40], header=[h for h in header if h],
        profile=profile, parsed=parsed[:30], parsed_count=len(parsed), errors=errors[:20], error_count=len(errors),
        total=sum(t.amount_cents for t in parsed),
    )


@router.post("/movimenti/import/{token}/conferma")
async def import_confirm(token: str, request: Request, conto: int = 0, session: Session = Depends(get_session)):
    rows = _load_rows(token)
    account = session.get(Account, conto) or _404()
    form = await request.form()
    profile = _profile_from_form(form)
    parsed, errors = parse_rows(rows, profile)
    if not parsed:
        return redirect(f"/movimenti/import/{token}?conto={conto}", err="Nessun movimento valido da importare")
    account.csv_profile = profile.to_json()
    batch = datetime.now().strftime("%Y%m%d%H%M%S")
    res = import_transactions(session, account, parsed, batch, update_balance=bool(form.get("update_balance")))
    (config.IMPORTS_DIR / Path(token).name).unlink(missing_ok=True)
    msg = f"Importati {res.inserted} movimenti su '{account.name}'"
    if res.skipped:
        msg += f" ({res.skipped} già presenti, saltati)"
    if res.snapshot:
        msg += f". Saldo aggiornato al {res.snapshot[0].strftime('%d/%m/%Y')}"
    return redirect(f"/riconciliazione?batch={batch}", msg=msg)
