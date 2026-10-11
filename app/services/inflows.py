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


# ---------- previsione di fatturato ----------

SALES_VAT = 10  # aliquota delle vendite usata per scorporare l'IVA dalle entrate previste senza contratto


@dataclass
class RevenuePoint:
    end: date
    issued: int  # fatturato emesso dal 1° gennaio (imponibile, note di credito sottratte)
    contracts: int  # rate dei contratti ancora da fatturare entro la data (imponibile)
    other: int  # ricorrenze e altre entrate previste entro la data (imponibile stimato)
    gross_to_come: int  # le stesse entrate future IVA compresa

    @property
    def to_come(self) -> int:
        return self.contracts + self.other

    @property
    def total(self) -> int:
        return self.issued + self.to_come


@dataclass
class RevenueOutlook:
    year_end: RevenuePoint
    last: RevenuePoint | None  # fino all'ultimo incasso previsto (se oltre il 31/12)
    last_due: date | None  # scadenza dell'ultimo incasso previsto
    last_item: str = ""
    next_year: int = 0  # parte dopo il 31/12 (imponibile)


def revenue_outlook(session: Session, today: date | None = None) -> RevenueOutlook:
    """Fatturato dell'anno = fatture emesse finora + entrate previste ancora da fatturare (rate dei contratti,
    ricorrenze, voci manuali), IVA scorporata. Le fatture già emesse e non incassate sono già nel fatturato."""
    from app.models import Invoice
    from app.services.fatturapa import SELF_INVOICE_TYPES
    from app.services.kpi import _signed_taxable

    today = today or date.today()
    start, dec31 = date(today.year, 1, 1), date(today.year, 12, 31)
    issued = sum(_signed_taxable(i)[0] for i in session.scalars(select(Invoice).where(
        Invoice.direction == "attiva", Invoice.date >= start, Invoice.date <= today)) if i.doc_type not in SELF_INVOICE_TYPES)
    future = [it for it in session.scalars(select(PlannedItem).where(
        PlannedItem.status == "previsto", PlannedItem.amount_cents > 0, PlannedItem.invoice_id.is_(None),
        PlannedItem.due_date >= today).order_by(PlannedItem.due_date))]
    vat = {c.id: c.vat_rate for c in session.scalars(select(Contract))}

    def point(end: date) -> RevenuePoint:
        contracts = other = gross = 0
        for it in future:
            if it.due_date > end:
                continue
            rate = vat.get(it.contract_id, SALES_VAT) if it.contract_id else SALES_VAT
            net = round(it.amount_cents * 100 / (100 + rate))
            gross += it.amount_cents
            if it.contract_id:
                contracts += net
            else:
                other += net
        return RevenuePoint(end, issued, contracts, other, gross)

    year_end = point(dec31)
    # ultimo incasso previsto: anche le fatture emesse da incassare
    last_item = session.scalars(select(PlannedItem).where(
        PlannedItem.status == "previsto", PlannedItem.amount_cents > 0, PlannedItem.due_date >= today)
        .order_by(PlannedItem.due_date.desc())).first()
    last_due = last_item.due_date if last_item else None
    last = point(last_due) if last_due and last_due > dec31 else None
    return RevenueOutlook(year_end, last, last_due, last_item.title if last_item else "",
                          (last.to_come - year_end.to_come) if last else 0)
