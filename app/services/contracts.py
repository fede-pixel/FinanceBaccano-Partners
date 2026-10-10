"""Contratti con i clienti: il residuo da incassare diventa entrate previste (forecast, capacità di investimento).

Il residuo IVA compresa alla data di aggiornamento del piano, meno le fatture emesse al cliente dopo quella data
(il loro incasso è già previsto dalla fattura), è diviso in parti uguali per i mesi da quello corrente fino
alla data di fine. Le rate cadono a fine mese e vengono ricalcolate dopo ogni modifica, quindi una nuova fattura
o una data di fine diversa si riflettono subito."""
import calendar
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Contract, Invoice, PlannedItem
from app.services.fatturapa import CREDIT_NOTE_TYPES, SELF_INVOICE_TYPES
from app.services.invoice_match import name_matches

CATEGORY = "Incassi da contratti"


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def months_until(start: date, end: date) -> list[date]:
    """Fine mese di ogni mese da quello di start a quello di end compresi."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(_month_end(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def invoices_after(session: Session, c: Contract, excluded: bool = False) -> list[Invoice]:
    """Fatture emesse al cliente dopo la data di aggiornamento del piano (excluded=True: quelle segnate fuori contratto)."""
    q = select(Invoice).where(Invoice.direction == "attiva", Invoice.date > c.as_of)
    return [i for i in session.scalars(q.order_by(Invoice.date))
            if i.doc_type not in SELF_INVOICE_TYPES and name_matches(c.client, i.counterparty)
            and (i.id in c.excluded_ids) == excluded]


@dataclass
class Plan:
    contract: Contract
    residual: int  # IVA compresa alla data di aggiornamento
    invoiced_after: int  # fatture emesse dopo, IVA compresa
    invoices: list[Invoice] = field(default_factory=list)
    outside: list[Invoice] = field(default_factory=list)  # fatture al cliente segnate fuori contratto
    kept: int = 0  # rate già segnate incassate a mano
    to_plan: int = 0
    items: list[PlannedItem] = field(default_factory=list)


def plan(session: Session, c: Contract) -> Plan:
    invs = invoices_after(session, c)
    invoiced = sum(-abs(i.total_cents) if i.doc_type in CREDIT_NOTE_TYPES else abs(i.total_cents) for i in invs)
    items = list(session.scalars(select(PlannedItem).where(PlannedItem.contract_id == c.id).order_by(PlannedItem.due_date)))
    kept = sum(it.amount_cents for it in items if it.status != "previsto")
    p = Plan(c, c.residual_gross_cents, invoiced, invs, invoices_after(session, c, excluded=True), kept)
    p.to_plan = max(0, p.residual - invoiced - kept)
    p.items = items
    return p


def sync_contract(session: Session, c: Contract, today: date | None = None) -> None:
    """Rigenera le rate previste del contratto (quelle segnate incassate a mano restano)."""
    today = today or date.today()
    p = plan(session, c)
    for it in p.items:
        if it.status == "previsto" and not it.transactions:
            session.delete(it)
    if not c.active or p.to_plan <= 0:
        return
    months = months_until(max(today, c.as_of), max(c.end_date, today))
    base, rest = divmod(p.to_plan, len(months))
    label = f"{c.client}" + (f" – {c.title}" if c.title else "")
    for n, due in enumerate(months, start=1):
        session.add(PlannedItem(
            title=f"Incasso previsto {label} ({n}/{len(months)})", amount_cents=base + (1 if n <= rest else 0),
            due_date=due, account_id=c.account_id, counterparty=c.client, category=CATEGORY, source="contratto",
            estimated_date=True, contract_id=c.id,
            notes="Rata stimata dal piano del contratto: si ricalcola quando emetti fatture al cliente.",
        ))


def sync_all(session: Session, today: date | None = None) -> None:
    for c in session.scalars(select(Contract)):
        sync_contract(session, c, today)
    session.flush()


def seed_dmg(session: Session) -> bool:
    """Piano pagamenti DMG Analytics & Consulting (Del Moro) aggiornato al 05/10/2026, caricato una sola volta
    e solo se in app ci sono già fatture di questo cliente."""
    from app.services.util import get_setting, set_setting

    if get_setting(session, "seed_contract_dmg_2026"):
        return False
    if not any(name_matches("DMG Analytics & Consulting", i.counterparty)
               for i in session.scalars(select(Invoice).where(Invoice.direction == "attiva"))):
        return False
    set_setting(session, "seed_contract_dmg_2026", "1")
    c = Contract(
        client="DMG Analytics & Consulting S.r.l.s.", title="Via Gramsci 85, Monterotondo – interni 2 e 3",
        total_taxable_cents=9_649_000, paid_taxable_cents=4_028_887, vat_rate=10,
        as_of=date(2026, 10, 5), end_date=date(2027, 2, 28),
        notes="Contratto del 18/08/2026 (preventivo 8563) + integrazione 2.990 €: interno 2 56.000 €, interno 3 40.490 €. "
              "Versati 40.288,87 € imponibile (acconti FPR 48/26, 50/26, 54/26 e IV acconto del 05/10/2026). "
              "Saldo finale 5% (4.824,50 € + IVA) compreso nel residuo. La FPR 64/26 (sanitari) è fuori contratto.",
    )
    session.add(c)
    session.flush()
    sync_contract(session, c)
    return True
