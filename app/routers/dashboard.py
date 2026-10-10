import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, BankConnection, PlannedItem
from app.services.admin_docs import ensure_checklist, open_deadlines, period_label
from app.services.forecast import Forecast, account_statuses, build_forecast
from app.services.util import add_months, get_setting, month_bounds, month_label
from app.web import render

router = APIRouter()


def chart_payload(fc: Forecast) -> str:
    return json.dumps(
        {
            "labels": [d.date.isoformat() for d in fc.days],
            "actual": [None if d.actual is None else d.actual / 100 for d in fc.days],
            "projected": [None if d.projected is None else d.projected / 100 for d in fc.days],
            "inflow": [d.inflow / 100 for d in fc.days],
            "outflow": [d.outflow / 100 for d in fc.days],
            "threshold": fc.threshold / 100,
            "today": fc.today.isoformat(),
        }
    )


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    today = date.today()
    start, end = month_bounds(today.year, today.month)
    fc = build_forecast(session, start, end, today)
    next30 = build_forecast(session, today, today + timedelta(days=30), today)
    statuses = account_statuses(session, today)
    upcoming = list(
        session.scalars(
            select(PlannedItem)
            .where(PlannedItem.status == "previsto", PlannedItem.due_date >= today, PlannedItem.due_date <= today + timedelta(days=14))
            .order_by(PlannedItem.due_date)
            .limit(15)
        )
    )
    ensure_checklist(session, today)
    reminder_days = int(get_setting(session, "reminder_days") or 5)
    deadlines = open_deadlines(session, today, within_days=reminder_days)
    has_accounts = session.scalar(select(Account.id).limit(1)) is not None
    now = datetime.now()
    bank_alerts = [
        c for c in session.scalars(select(BankConnection).where(BankConnection.session_id != ""))
        if c.status in ("scaduto", "errore") or c.last_error or (c.valid_until and c.valid_until - now < timedelta(days=14))
    ]
    return render(
        request,
        "dashboard.html",
        fc=fc,
        next30=next30,
        statuses=statuses,
        upcoming=upcoming,
        deadlines=deadlines,
        period_label=period_label,
        chart=chart_payload(fc),
        month_title=month_label(today.year, today.month),
        has_accounts=has_accounts,
        bank_alerts=bank_alerts,
        has_banks=session.scalar(select(BankConnection.id).limit(1)) is not None,
        stale_after=today - timedelta(days=7),
        last_upload=_last_upload(session),
    )


def _last_upload(session: Session) -> datetime | None:
    raw = get_setting(session, "last_upload_at")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@router.get("/forecast")
def forecast(
    request: Request,
    mese: str = "",
    conto: int = 0,
    giorni: int = 0,
    session: Session = Depends(get_session),
):
    today = date.today()
    if giorni:
        start, end = today, today + timedelta(days=giorni)
        title = f"Prossimi {giorni} giorni"
        cur = today.replace(day=1)
    else:
        try:
            y, m = (int(x) for x in mese.split("-"))
            cur = date(y, m, 1)
        except ValueError:
            cur = today.replace(day=1)
        start, end = month_bounds(cur.year, cur.month)
        title = month_label(cur.year, cur.month)
    fc = build_forecast(session, start, end, today, [conto] if conto else None)
    accounts = list(session.scalars(select(Account).where(Account.active, Account.include_in_forecast).order_by(Account.id)))
    prev_m, next_m = add_months(cur, -1), add_months(cur, 1)
    return render(
        request,
        "forecast.html",
        fc=fc,
        title=title,
        chart=chart_payload(fc),
        accounts=accounts,
        conto=conto,
        giorni=giorni,
        mese=f"{cur.year}-{cur.month:02d}",
        prev_mese=f"{prev_m.year}-{prev_m.month:02d}",
        next_mese=f"{next_m.year}-{next_m.month:02d}",
    )
