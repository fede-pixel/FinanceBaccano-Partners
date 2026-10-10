from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.backup import run_backup
from app.db import get_session
from app.models import ChecklistItem, DocCategory
from app.services.admin_docs import docs_root, due_date_for, packages_root
from app.services.util import SETTING_DEFAULTS, get_setting, parse_amount, set_setting
from app.web import redirect, render

router = APIRouter()


@router.get("/impostazioni")
def settings_page(request: Request, session: Session = Depends(get_session)):
    values = {k: get_setting(session, k) for k in SETTING_DEFAULTS}
    values["admin_start_period"] = get_setting(session, "admin_start_period")
    cats = list(session.scalars(select(DocCategory).order_by(DocCategory.sort_order)))
    return render(
        request, "settings.html", v=values, cats=cats, data_dir=config.DATA_DIR,
        docs_dir=docs_root(session), packages_dir=packages_root(session), backup_dir=config.BACKUP_DIR,
        ai_enabled=__import__("app.services.ai_extract", fromlist=["is_enabled"]).is_enabled(),
    )


@router.post("/impostazioni")
async def settings_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for key in ("company_name", "company_vat", "accountant_email", "docs_dir", "packages_dir", "admin_start_period"):
        if key in form:
            set_setting(session, key, str(form[key]).strip())
    for key in ("default_payment_days", "reminder_days", "match_tolerance_days"):
        if str(form.get(key, "")).strip().isdigit():
            set_setting(session, key, str(form[key]).strip())
    threshold = parse_amount(str(form.get("low_balance_threshold", "")))
    if threshold is not None:
        set_setting(session, "low_balance_threshold", str(threshold))
    set_setting(session, "include_overdue_in_forecast", "1" if form.get("include_overdue_in_forecast") else "0")
    for cat in session.scalars(select(DocCategory)):
        day = str(form.get(f"cat_{cat.id}_due_day", "")).strip()
        if day.isdigit() and 1 <= int(day) <= 31 and int(day) != cat.due_day:
            cat.due_day = int(day)
            for item in session.scalars(select(ChecklistItem).where(ChecklistItem.category_id == cat.id, ChecklistItem.status != "inviato")):
                item.due_date = due_date_for(cat, item.period_end)
        cat.active = bool(form.get(f"cat_{cat.id}_active"))
    session.commit()
    return redirect("/impostazioni", msg="Impostazioni salvate")


@router.get("/impostazioni/backup.zip")
def backup_download(session: Session = Depends(get_session)):
    """Copia completa da conservare altrove: database coerente + cartella documenti."""
    import tempfile
    import zipfile
    from datetime import datetime

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    db_copy = run_backup(force=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    docs = docs_root(session)
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        if db_copy:
            zf.write(db_copy, "finance.db")
        for f in docs.rglob("*"):
            if f.is_file():
                zf.write(f, f"documenti/{f.relative_to(docs).as_posix()}")
    from pathlib import Path

    name = f"backup-finance-{datetime.now():%Y-%m-%d}.zip"
    return FileResponse(tmp.name, filename=name, background=BackgroundTask(Path(tmp.name).unlink, missing_ok=True))


@router.post("/impostazioni/backup")
def backup_now():
    path = run_backup(force=True)
    return redirect("/impostazioni", msg=f"Backup creato: {path}")
