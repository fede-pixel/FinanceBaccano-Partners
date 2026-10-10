"""Generazione delle voci previste a partire dalle ricorrenze."""
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import PlannedItem, Recurrence
from app.services.util import add_months, adjust_business_day

FREQUENCIES = {
    "settimanale": "Settimanale",
    "mensile": "Mensile",
    "bimestrale": "Bimestrale",
    "trimestrale": "Trimestrale",
    "semestrale": "Semestrale",
    "annuale": "Annuale",
}
_MONTH_STEP = {"mensile": 1, "bimestrale": 2, "trimestrale": 3, "semestrale": 6, "annuale": 12}
HORIZON_DAYS = 400


def occurrences(rec: Recurrence, until: date) -> list[date]:
    """Date 'nominali' (prima dello spostamento al giorno lavorativo) fino a `until` incluso."""
    end = min(until, rec.end_date) if rec.end_date else until
    out: list[date] = []
    if rec.frequency == "settimanale":
        d = rec.start_date
        while d <= end:
            out.append(d)
            d += timedelta(days=7)
        return out
    step = _MONTH_STEP.get(rec.frequency, 1)
    i = 0
    while True:
        d = add_months(rec.start_date.replace(day=1), i * step, day=rec.day_of_month)
        if d > end:
            break
        if d >= rec.start_date:
            out.append(d)
        i += 1
    return out


def sync_recurrence(session: Session, rec: Recurrence, today: date | None = None) -> None:
    """Allinea le voci previste di una ricorrenza.

    Le occorrenze già pagate o modificate a mano (source 'modificata') restano; quelle future
    ancora 'previste' vengono rigenerate dalla regola (così una modifica di importo/giorno si propaga)."""
    today = today or date.today()

    def period(d: date):
        # per le frequenze mensili un'occorrenza è "il mese": cambiare il giorno non deve duplicarla
        return d if rec.frequency == "settimanale" else (d.year, d.month)

    existing = {
        period(p.occurrence_key): p
        for p in session.scalars(select(PlannedItem).where(PlannedItem.recurrence_id == rec.id))
        if p.occurrence_key
    }
    wanted = set()
    if rec.active:
        for nominal in occurrences(rec, today + timedelta(days=HORIZON_DAYS)):
            wanted.add(period(nominal))
            due = adjust_business_day(nominal, rec.business_day_rule)
            item = existing.get(period(nominal))
            if item is None and due < today:
                continue  # non creo occorrenze passate: risulterebbero "scadute" anche se già pagate
            if item is None:
                session.add(
                    PlannedItem(
                        title=rec.title,
                        amount_cents=rec.amount_cents,
                        due_date=due,
                        account_id=rec.account_id,
                        category=rec.category,
                        source="ricorrente",
                        recurrence_id=rec.id,
                        occurrence_key=nominal,
                    )
                )
            elif item.source == "ricorrente" and item.status == "previsto" and item.due_date >= today:
                item.title, item.amount_cents, item.due_date = rec.title, rec.amount_cents, due
                item.account_id, item.category, item.occurrence_key = rec.account_id, rec.category, nominal
    # Elimina occorrenze future non più previste dalla regola (mai quelle pagate).
    stale = [
        p.id for k, p in existing.items()
        if k not in wanted and p.source == "ricorrente" and p.status == "previsto" and p.due_date >= today
        and not p.transactions
    ]
    if stale:
        session.execute(delete(PlannedItem).where(PlannedItem.id.in_(stale)))


def sync_all(session: Session, today: date | None = None) -> None:
    for rec in session.scalars(select(Recurrence)):
        sync_recurrence(session, rec, today)
    session.commit()
