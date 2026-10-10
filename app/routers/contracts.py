"""Contratti con i clienti e piani di incasso."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, Contract
from app.services import contracts, inflows
from app.services.util import fmt_eur, parse_amount, parse_date
from app.web import redirect, render

router = APIRouter()


@router.get("/contratti")
def contracts_page(request: Request, edit: int = 0, session: Session = Depends(get_session)):
    rows = list(session.scalars(select(Contract).order_by(Contract.active.desc(), Contract.end_date)))
    plans = [contracts.plan(session, c) for c in rows]
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    return render(request, "contracts.html", plans=plans, accounts=accounts, editing=session.get(Contract, edit) if edit else None,
                  inflows=inflows.monthly(session), total_to_plan=sum(p.to_plan for p in plans if p.contract.active))


@router.post("/contratti/salva")
def contract_save(
    contract_id: int = Form(0),
    client: str = Form(...),
    title: str = Form(""),
    total: str = Form(...),
    paid: str = Form("0"),
    vat_rate: int = Form(10),
    vat_included: int = Form(0),
    as_of: str = Form(...),
    end_date: str = Form(...),
    account_id: int = Form(0),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    tot, pd, a, e = parse_amount(total), parse_amount(paid or "0"), parse_date(as_of), parse_date(end_date)
    if tot is None or pd is None or a is None or e is None or not client.strip():
        return redirect("/contratti", err="Cliente, importi o date non validi")
    if e < a:
        return redirect("/contratti", err="La data di fine deve essere dopo la data di aggiornamento")
    c = session.get(Contract, contract_id) if contract_id else Contract()
    if c is None:
        raise HTTPException(404)
    c.client, c.title, c.notes = client.strip(), title.strip(), notes
    c.total_taxable_cents, c.paid_taxable_cents, c.vat_rate = abs(tot), abs(pd), max(0, min(vat_rate, 30))
    c.as_of, c.end_date, c.account_id = a, e, account_id or None
    c.vat_included = bool(vat_included)
    session.add(c)
    session.flush()
    contracts.sync_contract(session, c)
    session.commit()
    return redirect("/contratti", msg=f"Contratto salvato: residuo {fmt_eur(c.residual_gross_cents)} IVA compresa "
                    f"diviso fino a {e:%m/%Y}")


@router.post("/contratti/{contract_id}/fattura")
def contract_invoice(contract_id: int, invoice_id: int = Form(...), inside: int = Form(0), session: Session = Depends(get_session)):
    """Segna una fattura al cliente come fuori contratto (non scala il residuo) o la rimette nel contratto."""
    c = session.get(Contract, contract_id)
    if c is None:
        raise HTTPException(404)
    ids = c.excluded_ids - {invoice_id} if inside else c.excluded_ids | {invoice_id}
    c.excluded_invoices = ",".join(str(i) for i in sorted(ids))
    contracts.sync_contract(session, c)
    session.commit()
    return redirect("/contratti", msg="Fattura rimessa nel contratto" if inside else "Fattura segnata fuori contratto: non scala più il residuo")


@router.post("/contratti/{contract_id}/stato")
def contract_toggle(contract_id: int, session: Session = Depends(get_session)):
    c = session.get(Contract, contract_id)
    if c is None:
        raise HTTPException(404)
    c.active = not c.active
    contracts.sync_contract(session, c)
    session.commit()
    return redirect("/contratti", msg="Contratto riattivato" if c.active else "Contratto chiuso: le rate stimate sono state tolte")


@router.post("/contratti/{contract_id}/elimina")
def contract_delete(contract_id: int, session: Session = Depends(get_session)):
    c = session.get(Contract, contract_id)
    if c is not None:
        session.delete(c)  # le rate previste si cancellano con il contratto (ON DELETE CASCADE)
        session.commit()
    return redirect("/contratti", msg="Contratto eliminato")
