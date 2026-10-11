"""Cantieri: assegnazione delle fatture ed EBITDA per cantiere."""
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Invoice, Project
from app.services import projects
from app.services.fatturapa import SELF_INVOICE_TYPES
from app.web import redirect, render

router = APIRouter()


def _year(anno: int) -> int:
    return anno if 2000 <= anno <= 2100 else date.today().year


@router.get("/cantieri")
def projects_page(request: Request, anno: int = 0, edit: int = 0, vista: str = "", session: Session = Depends(get_session)):
    year = _year(anno)
    rep = projects.report(session, year)
    q = select(Invoice).where(Invoice.date >= date(year, 1, 1), Invoice.date <= date(year, 12, 31))
    if vista == "emesse":
        q = q.where(Invoice.direction == "attiva", Invoice.project_id.is_(None), Invoice.doc_type.not_in(SELF_INVOICE_TYPES))
    else:
        q = q.where(Invoice.project_source == "", (Invoice.direction == "passiva") | Invoice.doc_type.in_(SELF_INVOICE_TYPES))
    todo = list(session.scalars(q.order_by(Invoice.total_cents.desc()).limit(150)))
    plist = list(session.scalars(select(Project).order_by(Project.active.desc(), Project.name)))
    from app.services.ai_extract import is_enabled

    return render(request, "projects.html", rep=rep, year=year, todo=todo, plist=plist, vista=vista,
                  editing=session.get(Project, edit) if edit else None, ai_enabled=is_enabled())


@router.get("/cantieri/{project_id}")
def project_detail(project_id: int, request: Request, anno: int = 0, session: Session = Depends(get_session)):
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404)
    year = _year(anno)
    invs = list(session.scalars(select(Invoice).where(Invoice.project_id == p.id, Invoice.date >= date(year, 1, 1),
                                                      Invoice.date <= date(year, 12, 31)).order_by(Invoice.date)))
    row = next((r for r in projects.report(session, year).rows if r.project and r.project.id == p.id), None)
    plist = list(session.scalars(select(Project).order_by(Project.name)))
    return render(request, "project_detail.html", p=p, invs=invs, row=row, year=year, plist=plist)


@router.post("/cantieri/salva")
def project_save(project_id: int = Form(0), name: str = Form(...), client: str = Form(""), keywords: str = Form(""),
                 active: int = Form(0), notes: str = Form(""), session: Session = Depends(get_session)):
    p = session.get(Project, project_id) if project_id else Project()
    if p is None:
        raise HTTPException(404)
    p.name, p.client, p.keywords, p.notes = name.strip(), client.strip(), keywords.strip(), notes
    p.active = bool(active) or not project_id
    session.add(p)
    session.flush()
    n = projects.auto_assign(session)
    session.commit()
    return redirect("/cantieri", msg=f"Cantiere salvato: {n} fatture riassegnate dalle regole")


@router.post("/cantieri/{project_id}/elimina")
def project_delete(project_id: int, session: Session = Depends(get_session)):
    p = session.get(Project, project_id)
    if p is not None:
        for inv in session.scalars(select(Invoice).where(Invoice.project_id == p.id)):
            inv.project_id, inv.project_source = None, ""
        session.delete(p)
        session.commit()
    return redirect("/cantieri", msg="Cantiere eliminato: le sue fatture tornano da assegnare")


@router.post("/cantieri/crea-da-clienti")
def from_clients(anno: int = Form(0), session: Session = Depends(get_session)):
    n = projects.seed_from_clients(session, _year(anno))
    m = projects.auto_assign(session)
    session.commit()
    return redirect("/cantieri", msg=f"Creati {n} cantieri dai clienti e dai contratti; {m} fatture assegnate dalle regole")


@router.post("/cantieri/assegna-ai")
def assign_ai(anno: int = Form(0), session: Session = Depends(get_session)):
    from app.services.ai_extract import AIExtractionError, AIUnavailable

    try:
        a, g = projects.ai_assign(session, _year(anno))
    except (AIUnavailable, AIExtractionError) as exc:
        return redirect("/cantieri", err=str(exc))
    session.commit()
    return redirect("/cantieri", msg=f"L'AI ha assegnato {a} fatture ai cantieri e {g} alle spese generali. Controlla e correggi dove serve.")


@router.post("/cantieri/fattura")
def assign_invoice(invoice_id: int = Form(...), target: str = Form(""), back: str = Form("/cantieri"),
                   session: Session = Depends(get_session)):
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404)
    if target == projects.GENERAL:
        inv.project_id, inv.project_source = None, projects.GENERAL
    elif target.isdigit() and session.get(Project, int(target)):
        inv.project_id, inv.project_source = int(target), "manuale"
    else:  # torna alle regole automatiche
        inv.project_id, inv.project_source = None, ""
        projects.auto_assign(session, [inv])
    session.commit()
    return redirect(back if back.startswith("/") else "/cantieri", msg=f"Fattura n. {inv.number} aggiornata")
