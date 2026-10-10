"""Caricamento settimanale: tutti i file in un colpo solo."""
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, DocCategory
from app.services import weekly
from app.services.admin_docs import period_label
from app.web import redirect, render

router = APIRouter()


@router.get("/carica")
def upload_page(request: Request, session: Session = Depends(get_session)):
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    return render(request, "upload.html", accounts=accounts)


@router.post("/carica")
async def upload_files(files: list[UploadFile] = File(...), session: Session = Depends(get_session)):
    payload = [(f.filename or "file", await f.read()) for f in files]
    batch = weekly.create_batch(session, payload)
    if not batch.items:
        return redirect("/carica", err="Nessun file ricevuto")
    return redirect(f"/carica/{batch.id}")


def _load(batch_id: str) -> weekly.Batch:
    batch = weekly.load_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "Caricamento non trovato o già completato")
    return batch


@router.get("/carica/{batch_id}")
def upload_review(batch_id: str, request: Request, session: Session = Depends(get_session)):
    batch = _load(batch_id)
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    cats = list(session.scalars(select(DocCategory).where(DocCategory.active).order_by(DocCategory.sort_order)))
    return render(request, "upload_review.html", batch=batch, accounts=accounts, cats=cats, period_label=period_label,
                  profiles={a.id: bool(a.csv_profile) for a in accounts})


@router.post("/carica/{batch_id}")
async def upload_process(batch_id: str, request: Request, session: Session = Depends(get_session)):
    batch = _load(batch_id)
    form = await request.form()
    for i, item in enumerate(batch.items):
        kind = str(form.get(f"kind_{i}", item.kind))
        item.kind = kind if kind in ("banca", "fattura", "documento", "ignora") else item.kind
        if str(form.get(f"account_{i}", "")).isdigit():
            item.account_id = int(form[f"account_{i}"])
        item.category = str(form.get(f"category_{i}", item.category)) or item.category
        period = str(form.get(f"period_{i}", item.period))
        if len(period) == 7 and period[4] == "-":
            item.period = period
    res = weekly.process_batch(session, batch)
    accounts = {a.id: a for a in session.scalars(select(Account))}
    return render(request, "upload_done.html", res=res, batch=batch, accounts=accounts)
