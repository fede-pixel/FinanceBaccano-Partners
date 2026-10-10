from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, Invoice, PlannedItem, Recurrence, Transaction
from app.services import reconcile
from app.services.fatturapa import import_invoices
from app.services.recurrence import FREQUENCIES, sync_recurrence
from app.services.util import get_setting, month_bounds, parse_amount, parse_date
from app.web import redirect, render

router = APIRouter()


def _404():
    raise HTTPException(404, "Non trovato")


def _accounts(session: Session) -> list[Account]:
    return list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))


# ---------------- voci previste ----------------

@router.get("/previsti")
def planned_list(
    request: Request,
    mese: str = "",
    stato: str = "previsto",
    tipo: str = "",
    session: Session = Depends(get_session),
):
    today = date.today()
    stmt = select(PlannedItem).order_by(PlannedItem.due_date, PlannedItem.id)
    if mese:
        try:
            y, m = (int(x) for x in mese.split("-"))
            s, e = month_bounds(y, m)
            stmt = stmt.where(PlannedItem.due_date >= s, PlannedItem.due_date <= e)
        except ValueError:
            pass
    elif stato == "previsto":
        stmt = stmt.where(PlannedItem.due_date <= today + timedelta(days=120))
    if stato:
        stmt = stmt.where(PlannedItem.status == stato)
    if tipo == "entrate":
        stmt = stmt.where(PlannedItem.amount_cents > 0)
    elif tipo == "uscite":
        stmt = stmt.where(PlannedItem.amount_cents < 0)
    rows = list(session.scalars(stmt.limit(800)))
    return render(
        request, "planned.html", rows=rows, accounts=_accounts(session), mese=mese, stato=stato, tipo=tipo,
        tot_in=sum(r.amount_cents for r in rows if r.amount_cents > 0),
        tot_out=sum(r.amount_cents for r in rows if r.amount_cents < 0),
    )


@router.get("/previsti/{item_id}")
def planned_edit(item_id: int, request: Request, session: Session = Depends(get_session)):
    item = session.get(PlannedItem, item_id) or _404()
    return render(request, "planned_form.html", item=item, accounts=_accounts(session))


@router.post("/previsti/salva")
def planned_save(
    item_id: int = Form(0),
    title: str = Form(...),
    amount: str = Form(...),
    direction: str = Form("uscita"),
    due_date: str = Form(...),
    account_id: int = Form(0),
    counterparty: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    back: str = Form("/previsti"),
    session: Session = Depends(get_session),
):
    cents, d = parse_amount(amount), parse_date(due_date)
    if cents is None or d is None:
        return redirect(back, err="Data o importo non validi")
    item = session.get(PlannedItem, item_id) if item_id else PlannedItem(source="manuale")
    if item is None:
        _404()
    item.title, item.due_date = title.strip(), d
    item.amount_cents = abs(cents) if direction == "entrata" else -abs(cents)
    item.account_id = account_id or None
    item.counterparty, item.category, item.notes = counterparty.strip(), category.strip(), notes
    item.estimated_date = False
    if item.source == "ricorrente":
        item.source = "modificata"  # la ricorrenza non la sovrascrive più
    session.add(item)
    session.commit()
    return redirect(back if back.startswith("/") else "/previsti", msg="Voce salvata")


@router.post("/previsti/{item_id}/stato")
def planned_status(item_id: int, status: str = Form(...), back: str = Form("/previsti"), session: Session = Depends(get_session)):
    item = session.get(PlannedItem, item_id) or _404()
    if status not in ("previsto", "pagato", "annullato"):
        return redirect(back, err="Stato non valido")
    item.status = status
    item.paid_date = date.today() if status == "pagato" else None
    session.commit()
    return redirect(back if back.startswith("/") else "/previsti", msg="Stato aggiornato")


@router.post("/previsti/{item_id}/elimina")
def planned_delete(item_id: int, session: Session = Depends(get_session)):
    item = session.get(PlannedItem, item_id) or _404()
    if item.recurrence_id:
        # le occorrenze di una ricorrenza si annullano (altrimenti la regola le ricreerebbe)
        item.status = "annullato"
    else:
        for tx in item.transactions:
            tx.planned_item_id = None
        session.delete(item)
    session.commit()
    return redirect("/previsti", msg="Voce eliminata")


# ---------------- ricorrenze ----------------

@router.get("/ricorrenze")
def recurrences(request: Request, edit: int = 0, session: Session = Depends(get_session)):
    rows = list(session.scalars(select(Recurrence).order_by(Recurrence.active.desc(), Recurrence.day_of_month)))
    current = session.get(Recurrence, edit) if edit else None
    monthly_total = sum(r.amount_cents * _per_month(r.frequency) for r in rows if r.active)
    return render(
        request, "recurrences.html", rows=rows, accounts=_accounts(session), frequencies=FREQUENCIES,
        current=current, monthly_total=int(monthly_total),
    )


def _per_month(freq: str) -> float:
    return {"settimanale": 52 / 12, "mensile": 1, "bimestrale": 0.5, "trimestrale": 1 / 3, "semestrale": 1 / 6, "annuale": 1 / 12}.get(freq, 1)


@router.post("/ricorrenze/salva")
def recurrence_save(
    rec_id: int = Form(0),
    title: str = Form(...),
    amount: str = Form(...),
    direction: str = Form("uscita"),
    frequency: str = Form("mensile"),
    day_of_month: int = Form(1),
    start_date: str = Form(""),
    end_date: str = Form(""),
    business_day_rule: str = Form("nessuna"),
    account_id: int = Form(0),
    category: str = Form(""),
    active: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    cents = parse_amount(amount)
    if cents is None:
        return redirect("/ricorrenze", err="Importo non valido")
    rec = session.get(Recurrence, rec_id) if rec_id else Recurrence()
    if rec is None:
        _404()
    rec.title = title.strip()
    rec.amount_cents = abs(cents) if direction == "entrata" else -abs(cents)
    rec.frequency = frequency if frequency in FREQUENCIES else "mensile"
    rec.day_of_month = max(1, min(31, day_of_month))
    rec.start_date = parse_date(start_date) or date.today().replace(day=1)
    rec.end_date = parse_date(end_date)
    rec.business_day_rule = business_day_rule
    rec.account_id = account_id or None
    rec.category, rec.notes = category.strip(), notes
    rec.active = bool(active) if rec_id else True
    session.add(rec)
    session.flush()
    sync_recurrence(session, rec)
    session.commit()
    return redirect("/ricorrenze", msg=f"Ricorrenza '{rec.title}' salvata e scadenze aggiornate")


@router.post("/ricorrenze/{rec_id}/elimina")
def recurrence_delete(rec_id: int, session: Session = Depends(get_session)):
    rec = session.get(Recurrence, rec_id) or _404()
    # le occorrenze già pagate restano nello storico come voci manuali
    for item in session.scalars(select(PlannedItem).where(PlannedItem.recurrence_id == rec.id)):
        if item.status == "pagato":
            item.recurrence_id, item.occurrence_key, item.source = None, None, "manuale"
    session.flush()
    session.delete(rec)
    session.commit()
    return redirect("/ricorrenze", msg="Ricorrenza eliminata")


# ---------------- fatture ----------------

@router.get("/fatture")
def invoices(request: Request, direzione: str = "", session: Session = Depends(get_session)):
    stmt = select(Invoice).order_by(Invoice.date.desc(), Invoice.id.desc())
    if direzione:
        stmt = stmt.where(Invoice.direction == direzione)
    rows = list(session.scalars(stmt.limit(500)))
    return render(
        request, "invoices.html", rows=rows, accounts=_accounts(session), direzione=direzione,
        company_vat=get_setting(session, "company_vat"),
    )


@router.post("/fatture/import")
async def invoices_import(
    files: list[UploadFile] = File(...),
    direction: str = Form("auto"),
    account_id: int = Form(0),
    paid_before: str = Form(""),
    session: Session = Depends(get_session),
):
    payload = [(f.filename or "fattura.xml", await f.read()) for f in files]
    res = import_invoices(
        session,
        payload,
        company_vat=get_setting(session, "company_vat"),
        forced_direction=direction,
        default_payment_days=int(get_setting(session, "default_payment_days") or 30),
        account_id=account_id or None,
        paid_before=parse_date(paid_before),
    )
    msg = f"Importate {len(res.imported)} fatture"
    if res.duplicates:
        msg += f", {len(res.duplicates)} già presenti"
    if res.vat_completed:
        msg += f" (a {res.vat_completed} è stata aggiunta l'IVA mancante)"
    if res.rejected:
        msg += f" · {len(res.rejected)} scartate dallo SdI ignorate (non sono fatture valide)"
    from app.services import invoice_match

    matched = invoice_match.run(session).matched
    session.commit()
    if matched:
        msg += f" · {matched} già abbinate al pagamento"
    err = " · ".join(res.errors[:5]) + (f" (e altri {len(res.errors) - 5} errori)" if len(res.errors) > 5 else "")
    return redirect("/fatture", msg=msg, err=err)


@router.post("/fatture/{invoice_id}/elimina")
def invoice_delete(invoice_id: int, session: Session = Depends(get_session)):
    inv = session.get(Invoice, invoice_id) or _404()
    for item in inv.planned_items:
        for tx in item.transactions:
            tx.planned_item_id = None
    session.delete(inv)
    session.commit()
    return redirect("/fatture", msg="Fattura e relative scadenze eliminate")


# ---------------- riconciliazione ----------------

@router.get("/riconciliazione")
def reconciliation(request: Request, batch: str = "", session: Session = Depends(get_session)):
    tol = int(get_setting(session, "match_tolerance_days") or 7)
    suggestions = reconcile.suggest_matches(session, tol)
    suggested_tx = {s.transaction.id for s in suggestions}
    unmatched = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.planned_item_id.is_(None), Transaction.date >= date.today() - timedelta(days=60))
            .order_by(Transaction.date.desc())
            .limit(200)
        )
    )
    unmatched = [t for t in unmatched if t.id not in suggested_tx]
    open_items = list(
        session.scalars(
            select(PlannedItem)
            .where(PlannedItem.status == "previsto", PlannedItem.due_date <= date.today() + timedelta(days=30))
            .order_by(PlannedItem.due_date)
        )
    )
    return render(
        request, "reconcile.html", suggestions=suggestions, unmatched=unmatched, open_items=open_items, batch=batch,
    )


@router.post("/riconciliazione/abbina")
def reconcile_match(tx_id: int = Form(...), item_id: int = Form(...), session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id) or _404()
    item = session.get(PlannedItem, item_id) or _404()
    reconcile.match(session, tx, item)
    session.commit()
    return redirect("/riconciliazione", msg="Abbinato")


@router.post("/riconciliazione/abbina-certi")
def reconcile_auto(session: Session = Depends(get_session)):
    tol = int(get_setting(session, "match_tolerance_days") or 7)
    n = 0
    for s in reconcile.suggest_matches(session, tol):
        if s.certain:
            reconcile.match(session, s.transaction, s.item)
            n += 1
    session.commit()
    return redirect("/riconciliazione", msg=f"Abbinati automaticamente {n} movimenti")


@router.post("/riconciliazione/separa")
def reconcile_unmatch(tx_id: int = Form(...), back: str = Form("/riconciliazione"), session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id) or _404()
    reconcile.unmatch(session, tx)
    tx.no_auto_match = True  # scollegato a mano: l'app non lo riabbina da sola
    session.commit()
    return redirect(back if back.startswith("/") else "/riconciliazione", msg="Abbinamento rimosso")
