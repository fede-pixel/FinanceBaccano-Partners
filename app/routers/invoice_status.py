"""Situazione fatture: da pagare, da incassare, pagate (con il movimento che lo prova)."""
import io
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import PlannedItem, Transaction
from app.services import invoice_match, reconcile
from app.services.util import parse_date
from app.web import redirect, render

router = APIRouter()

VIEWS = {
    "pagare": ("passiva", ("aperta",), "Da pagare"),
    "incassare": ("attiva", ("aperta",), "Da incassare"),
    "presunte": (None, ("presunta",), "Pagate senza movimento"),
    "pagate": (None, ("pagata", "compensata"), "Pagate e incassate"),
}


def _rows(session: Session, vista: str, q: str = "") -> list[invoice_match.InvoiceStatus]:
    direction, states, _ = VIEWS[vista]
    rows = [s for s in invoice_match.invoice_statuses(session, direction) if s.state in states]
    if q:
        ql = q.lower()
        rows = [s for s in rows if ql in s.invoice.counterparty.lower() or ql in s.invoice.number.lower()]
    if states == ("aperta",):
        rows.sort(key=lambda s: (s.due or date.max, s.invoice.counterparty))
    else:
        rows.sort(key=lambda s: (s.paid_date or s.invoice.date), reverse=True)
    return rows


@router.get("/fatture/situazione")
def situation(request: Request, vista: str = "pagare", q: str = "", session: Session = Depends(get_session)):
    if vista not in VIEWS:
        vista = "pagare"
    result = invoice_match.run(session)  # gli abbinamenti certi si fanno da soli a ogni apertura
    session.commit()
    all_rows = invoice_match.invoice_statuses(session)
    today = date.today()

    def kpi(direction, state="aperta"):
        rows = [s for s in all_rows if s.state == state and (direction is None or s.invoice.direction == direction)]
        return {
            "count": len(rows),
            "total": sum(s.open_cents if state == "aperta" else s.paid_cents for s in rows),
            "overdue": sum(s.open_cents for s in rows if s.due and s.due < today),
            "overdue_count": sum(1 for s in rows if s.due and s.due < today),
        }

    msg = request.query_params.get("msg", "")
    if result.matched or result.compensated:
        auto = f"Controllo incrociato: {result.matched} scadenze abbinate ai movimenti"
        if result.compensated:
            auto += f", {result.compensated} note di credito compensate"
        msg = f"{msg} · {auto}" if msg else auto
    return render(
        request, "invoice_status.html", vista=vista, views=VIEWS, q=q, rows=_rows(session, vista, q),
        proposals=result.proposals, msg=msg,
        k_pay=kpi("passiva"), k_inc=kpi("attiva"), k_pres=kpi(None, "presunta"), k_paid=kpi(None, "pagata"),
    )


@router.post("/fatture/situazione/abbina")
def confirm(tx_id: int = Form(...), item_ids: str = Form(...), session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id)
    items = [session.get(PlannedItem, int(i)) for i in item_ids.split(",") if i.strip().isdigit()]
    if tx is None or not items or any(i is None for i in items):
        raise HTTPException(404, "Non trovato")
    if tx.planned_item_id:
        reconcile.unmatch(session, tx)
    invoice_match.settle(session, tx, items)
    session.commit()
    return redirect("/fatture/situazione?vista=pagate", msg=f"Abbinato: {len(items)} fatture saldate dal movimento del {tx.date:%d/%m/%Y}")


@router.post("/fatture/situazione/scollega")
def unlink(tx_id: int = Form(...), back: str = Form("/fatture/situazione?vista=pagate"), session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id)
    if tx is None:
        raise HTTPException(404, "Non trovato")
    reconcile.unmatch(session, tx)
    session.commit()
    return redirect(back, msg="Abbinamento annullato: le fatture tornano da saldare")


@router.post("/fatture/situazione/segna")
def mark(item_ids: str = Form(...), action: str = Form("pagata"), paid_date: str = Form(""),
         back: str = Form("/fatture/situazione"), session: Session = Depends(get_session)):
    """Segna a mano come pagate (es. pagamento in contanti o da un conto non caricato) o riapre."""
    n = 0
    for i in item_ids.split(","):
        it = session.get(PlannedItem, int(i)) if i.strip().isdigit() else None
        if it is None:
            continue
        if action == "riapri":
            it.status, it.paid_date, it.settlement = "previsto", None, ""
        else:
            it.status, it.settlement = "pagato", "manuale"
            it.paid_date = parse_date(paid_date) or date.today()
        n += 1
    session.commit()
    return redirect(back, msg="Fattura riaperta" if action == "riapri" else "Fattura segnata come pagata")


@router.get("/fatture/situazione.xlsx")
def export(vista: str = "pagare", session: Session = Depends(get_session)):
    import openpyxl
    from openpyxl.styles import Font

    if vista not in VIEWS:
        vista = "pagare"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = VIEWS[vista][2][:30]
    ws.append(["Tipo", "Data fattura", "Numero", "Documento", "Controparte", "P.IVA", "Scadenza", "Giorni di ritardo",
               "Totale fattura", "Da saldare", "Pagata il", "Movimento"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for s in _rows(session, vista):
        inv = s.invoice
        ws.append([
            "Ricevuta" if inv.direction == "passiva" else "Emessa", inv.date, inv.number, inv.doc_type, inv.counterparty,
            inv.counterparty_vat, s.due, s.overdue_days or None, inv.total_cents / 100, s.open_cents / 100 if s.open_cents else None,
            s.paid_date, "; ".join(f"{t.date:%d/%m/%Y} {t.account.name} {t.description[:60]}" for t in s.transactions),
        ])
    for col, w in zip("ABCDEFGHIJKL", (9, 12, 16, 8, 38, 14, 12, 10, 13, 13, 12, 60)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in (row[1], row[6], row[10]):
            c.number_format = "DD/MM/YYYY"
        for c in (row[8], row[9]):
            c.number_format = "#,##0.00"
    buf = io.BytesIO()
    wb.save(buf)
    name = f"fatture_{vista}_{date.today():%Y%m%d}.xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
