"""Incassi previsti mese per mese, per provenienza: fatture emesse, contratti, ricorrenze, altre voci."""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Contract, PlannedItem
from app.services.util import add_months

SOURCES = [  # (chiave, etichetta, colore del grafico)
    ("invoices", "Fatture emesse da incassare", "--series-actual"),
    ("contracts", "Contratti (rate stimate)", "--series-forecast"),
    ("recurring", "Ricorrenze", "--series-3"),
    ("other", "Altre entrate previste", "--series-4"),
]


@dataclass
class MonthIn:
    key: str  # YYYY-MM
    invoices: int = 0
    contracts: int = 0
    recurring: int = 0
    other: int = 0
    cumulative: int = 0

    @property
    def total(self) -> int:
        return self.invoices + self.contracts + self.recurring + self.other


@dataclass
class Inflows:
    months: list[MonthIn] = field(default_factory=list)
    overdue: int = 0  # incassi già scaduti e non arrivati (fatture da sollecitare)
    overdue_count: int = 0

    @property
    def total(self) -> int:
        return sum(m.total for m in self.months)

    def chart(self) -> dict:
        return {"kind": "bar", "labelFormat": "month", "labels": [m.key for m in self.months], "stacked": True,
                "title": "Incassi previsti mese per mese", "series": [
                    {"label": label, "values": [getattr(m, key) / 100 for m in self.months], "color": color, "stack": "in"}
                    for key, label, color in SOURCES]}

    def legend(self) -> list[tuple[str, str, str]]:
        return [(label, color, "bar") for _, label, color in SOURCES]


def _source(it: PlannedItem) -> str:
    if it.invoice_id:
        return "invoices"
    if it.contract_id:
        return "contracts"
    if it.recurrence_id:
        return "recurring"
    return "other"


def monthly(session: Session, today: date | None = None, min_months: int = 6, max_months: int = 12) -> Inflows:
    """Dal mese corrente fino all'ultima scadenza dei contratti attivi (almeno min_months, al massimo max_months)."""
    today = today or date.today()
    first = today.replace(day=1)
    last_contract = session.scalar(select(func.max(Contract.end_date)).where(Contract.active))
    n = min_months
    if last_contract:
        n = max(n, (last_contract.year - first.year) * 12 + last_contract.month - first.month + 1)
    n = min(n, max_months)
    end = add_months(first, n)
    out = Inflows(months=[MonthIn(f"{d.year}-{d.month:02d}") for d in (add_months(first, i) for i in range(n))])
    by_key = {m.key: m for m in out.months}
    q = select(PlannedItem).where(PlannedItem.status == "previsto", PlannedItem.amount_cents > 0, PlannedItem.due_date < end)
    for it in session.scalars(q):
        if it.due_date < today:
            out.overdue += it.amount_cents
            out.overdue_count += 1
            continue
        m = by_key.get(f"{it.due_date.year}-{it.due_date.month:02d}")
        if m is not None:
            setattr(m, _source(it), getattr(m, _source(it)) + it.amount_cents)
    run = 0
    for m in out.months:
        run += m.total
        m.cumulative = run
    return out
