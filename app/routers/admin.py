import os
import platform
import subprocess
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import get_session
from app.models import ChecklistItem, DocCategory, Document
from app.services import admin_docs as ad
from app.services.util import add_months, get_setting, parse_amount, parse_date, parse_month
from app.web import redirect, render

router = APIRouter()


def _404():
    raise HTTPException(404, "Non trovato")


@router.get("/documenti")
def checklist(request: Request, anno: int = 0, session: Session = Depends(get_session)):
    today = date.today()
    ad.ensure_checklist(session, today)
    items = list(session.scalars(select(ChecklistItem).join(DocCategory).order_by(ChecklistItem.period_end.desc(), DocCategory.sort_order)))
    years = sorted({int(i.period_key[:4]) for i in items}, reverse=True) or [today.year]
    anno = anno or years[0]
    items = [i for i in items if int(i.period_key[:4]) == anno]
    periods: dict[str, list[ChecklistItem]] = {}
    for it in items:
        periods.setdefault(it.period_key, []).append(it)
    monthly = {k: v for k, v in periods.items() if "-Q" not in k}
    quarterly = {k: v for k, v in periods.items() if "-Q" in k}
    deadlines = ad.open_deadlines(session, today, within_days=10_000)
    from app.services.refresh import stale_sent

    return render(
        request, "checklist.html", monthly=monthly, quarterly=quarterly, period_label=ad.period_label,
        status_labels=ad.STATUS_LABELS, years=years, anno=anno, deadlines=deadlines, stale_sent=stale_sent(session),
    )


@router.get("/documenti/voce/{item_id}")
def checklist_item(item_id: int, request: Request, session: Session = Depends(get_session)):
    item = session.get(ChecklistItem, item_id) or _404()
    return render(
        request, "checklist_item.html", item=item, period_label=ad.period_label, status_labels=ad.STATUS_LABELS,
        folder=ad.docs_root(session) / ad.item_folder(item),
    )


@router.post("/documenti/voce/{item_id}/carica")
async def checklist_upload(
    item_id: int,
    files: list[UploadFile] = File(...),
    doc_date: str = Form(""),
    amount: str = Form(""),
    supplier: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    item = session.get(ChecklistItem, item_id) or _404()
    n = 0
    for f in files:
        content = await f.read()
        if not content:
            continue
        ad.store_document(
            session, item, f.filename or "documento", content,
            doc_date=parse_date(doc_date), amount_cents=parse_amount(amount), supplier=supplier.strip(), notes=notes.strip(),
        )
        n += 1
    session.commit()
    return redirect(f"/documenti/voce/{item_id}", msg=f"Caricati {n} documenti")


@router.post("/documenti/voce/{item_id}/genera")
def checklist_generate(item_id: int, session: Session = Depends(get_session)):
    item = session.get(ChecklistItem, item_id) or _404()
    docs = ad.generate_auto_exports(session, item)
    from app.services.refresh import remember_export

    remember_export(session, item)
    session.commit()
    if not docs:
        return redirect(f"/documenti/voce/{item_id}", err="Nessun conto di questo tipo: aggiungilo in 'Conti e carte'")
    return redirect(f"/documenti/voce/{item_id}", msg=f"Generati {len(docs)} file Excel dai movimenti")


@router.post("/documenti/voce/{item_id}/stato")
def checklist_status(item_id: int, status: str = Form(...), notes: str = Form(None), session: Session = Depends(get_session)):
    item = session.get(ChecklistItem, item_id) or _404()
    if status in ad.STATUS_LABELS:
        item.status = status
        if status == "completato":
            item.completed_at = datetime.now()
        if status == "inviato":
            item.sent_at = datetime.now()
    if notes is not None:
        item.notes = notes
    session.commit()
    return redirect(f"/documenti/voce/{item_id}", msg="Aggiornato")


@router.get("/documenti/file/{doc_id}")
def document_download(doc_id: int, session: Session = Depends(get_session)):
    doc = session.get(Document, doc_id) or _404()
    path = ad.document_path(session, doc)
    if not path.exists():
        raise HTTPException(404, "File non più presente nella cartella documenti")
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@router.post("/documenti/file/{doc_id}/elimina")
def document_delete(doc_id: int, session: Session = Depends(get_session)):
    doc = session.get(Document, doc_id) or _404()
    item_id = doc.checklist_item_id
    ad.delete_document(session, doc)
    session.commit()
    return redirect(f"/documenti/voce/{item_id}", msg="Documento eliminato")


# ---------------- pacchetto ----------------

@router.get("/pacchetto")
def package(request: Request, mese: str = "", session: Session = Depends(get_session)):
    today = date.today()
    ad.ensure_checklist(session, today)
    ym = parse_month(mese)
    if ym:
        mese = f"{ym[0]}-{ym[1]:02d}"
    else:
        prev = add_months(today.replace(day=1), -1)
        mese = f"{prev.year}-{prev.month:02d}"
    items = ad.package_items(session, mese)
    zip_path = ad.packages_root(session) / f"{ad._safe(get_setting(session, 'company_name') or 'Documenti')} - Documenti {mese}.zip"
    email = get_setting(session, "accountant_email")
    company = get_setting(session, "company_name")
    subject = f"{company} – documenti {ad.period_label(mese)}"
    body = (
        f"Buongiorno,\n\nin allegato (e nella cartella Drive condivisa) i documenti di {ad.period_label(mese)}.\n\n"
        + "\n".join(f"- {i.category.name}: {len(i.documents)} file" + (" (nessuna operazione)" if i.status == "non_applicabile" else "") for i in items)
        + "\n\nCordiali saluti"
    )
    mailto = f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"
    months = []
    d = today.replace(day=1)
    for _ in range(12):
        months.append(f"{d.year}-{d.month:02d}")
        d = add_months(d, -1)
    return render(
        request, "package.html", items=items, mese=mese, months=months, period_label=ad.period_label,
        status_labels=ad.STATUS_LABELS, zip_path=zip_path, zip_exists=zip_path.exists(), mailto=mailto,
        packages_dir=ad.packages_root(session), missing=[i for i in items if i.status == "da_fare"],
    )


@router.post("/pacchetto/crea")
def package_build(mese: str = Form(...), session: Session = Depends(get_session)):
    if not parse_month(mese):
        return redirect("/pacchetto", err="Mese non valido")
    path = ad.build_package(session, mese)
    return redirect(f"/pacchetto?mese={mese}", msg=f"Pacchetto creato: {path}")


@router.get("/pacchetto/scarica")
def package_download(mese: str, session: Session = Depends(get_session)):
    if not parse_month(mese):
        raise HTTPException(404, "Mese non valido")
    path = ad.packages_root(session) / f"{ad._safe(get_setting(session, 'company_name') or 'Documenti')} - Documenti {mese}.zip"
    if not path.exists():
        raise HTTPException(404, "Crea prima il pacchetto")
    return FileResponse(path, filename=path.name)


@router.post("/pacchetto/inviato")
def package_sent(mese: str = Form(...), session: Session = Depends(get_session)):
    if not parse_month(mese):
        return redirect("/pacchetto", err="Mese non valido")
    ad.mark_sent(session, ad.package_items(session, mese))
    return redirect(f"/pacchetto?mese={mese}", msg="Documenti segnati come inviati")


@router.post("/apri-cartella")
def open_folder(path: str = Form(...), back: str = Form("/"), session: Session = Depends(get_session)):
    """Apre la cartella nel file manager del computer (l'app gira in locale)."""
    if config.ONLINE:
        return redirect(back if back.startswith("/") else "/", err="Online le cartelle non si aprono: scarica i file")
    p = Path(path).resolve()
    if p.is_file():
        p = p.parent
    back = back if back.startswith("/") else "/"
    allowed = [ad.docs_root(session).resolve(), ad.packages_root(session).resolve(), config.DATA_DIR.resolve()]
    if not any(p == a or a in p.parents for a in allowed):
        return redirect(back, err="Posso aprire solo le cartelle dell'app")
    if not p.exists():
        return redirect(back, err="Cartella non trovata")
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(p)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as exc:
        return redirect(back, err=f"Impossibile aprire la cartella: {exc}")
    return redirect(back)
