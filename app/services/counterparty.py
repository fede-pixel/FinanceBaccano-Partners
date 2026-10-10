"""Scheda di un cliente o fornitore: fatture, incassi/pagamenti e saldo ("tornano i conti con Barone?").

Gli importi sono IVA compresa, perché è quello che passa dalla banca."""
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Invoice, PlannedItem, Transaction
from app.services import categorize, invoice_match
from app.services.fatturapa import CREDIT_NOTE_TYPES, SELF_INVOICE_TYPES


@dataclass
class Ledger:
    query: str
    names: list[str]
    direction: str  # attiva (cliente) | passiva (fornitore) | mista
    invoices: list[invoice_match.InvoiceStatus]
    invoiced: int = 0  # fatture − note di credito, IVA compresa
    settled_by_movements: int = 0  # incassi/pagamenti collegati alle fatture
    settled_without_movement: int = 0  # segnate pagate a mano, compensate o presunte
    open_amount: int = 0
    unlinked: list[Transaction] = field(default_factory=list)  # movimenti col nome non collegati a fatture
    unlinked_total: int = 0

    @property
    def collected(self) -> int:
        """Incassato (o pagato) risultante dalla banca: abbinato alle fatture + movimenti col nome non abbinati."""
        return self.settled_by_movements + self.unlinked_total

    @property
    def residual(self) -> int:
        """Quanto resta davvero da incassare/pagare: fatturato − banca − saldato senza movimento."""
        return self.invoiced - self.collected - self.settled_without_movement

    @property
    def difference(self) -> int:
        """Fatturato − incassato (collegato + non collegato) − saldato senza movimento − aperto: 0 se i conti tornano."""
        return self.invoiced - self.settled_by_movements - self.unlinked_total - self.settled_without_movement - self.open_amount


def names_like(session: Session, q: str) -> list[str]:
    q = q.strip()
    if not q:
        return []
    return sorted(set(session.scalars(select(Invoice.counterparty).where(func.lower(Invoice.counterparty).contains(q.lower())))))


def build(session: Session, q: str) -> Ledger:
    names = names_like(session, q)
    statuses = [s for s in invoice_match.invoice_statuses(session) if s.invoice.counterparty in names
                and s.invoice.doc_type not in SELF_INVOICE_TYPES]
    statuses.sort(key=lambda s: s.invoice.date)
    dirs = {s.invoice.direction for s in statuses}
    led = Ledger(q, names, dirs.pop() if len(dirs) == 1 else ("mista" if dirs else ""), statuses)
    seen_tx: set[int] = set()
    for s in statuses:
        sign = -1 if s.invoice.doc_type in CREDIT_NOTE_TYPES else 1
        led.invoiced += sign * abs(s.invoice.total_cents)
        if s.state == "aperta":
            led.open_amount += s.open_cents
        for t in s.transactions:
            if t.id not in seen_tx:
                seen_tx.add(t.id)
                led.settled_by_movements += abs(t.amount_cents) * (1 if (t.amount_cents > 0) == (s.invoice.direction == "attiva") else -1)
        # rate saldate senza movimento: a mano, compensate con nota di credito o presunte (storico, carta, contanti)
        led.settled_without_movement += sign * sum(abs(it.amount_cents) for it in s.items
                                                   if it.status == "pagato" and not it.transactions and not it.payment_tx_id)
    # movimenti che nominano la controparte ma non sono collegati a nessuna fattura (incassi da abbinare)
    words = set()
    for n in names or [q]:
        words |= invoice_match._name_words(n)
    key = {invoice_match._key_word(n) for n in names} - {None}
    q_words = {w for w in invoice_match._tokens(q) if len(w) >= 3}  # la parola cercata (es. "barone") basta da sola
    direction_out = led.direction == "passiva"
    for t in session.scalars(select(Transaction).where(Transaction.planned_item_id.is_(None))):
        if t.id in seen_tx or t.category == categorize.INTERNAL:
            continue
        if led.direction in ("attiva", "passiva") and (t.amount_cents < 0) != direction_out:
            continue
        text_words = set(invoice_match._tokens(f"{t.counterparty} {t.description}"))
        if (key & text_words) or (words and len(words & text_words) >= min(2, len(words))) or \
                (q_words and q_words <= text_words):
            led.unlinked.append(t)
    group_paid = set(session.scalars(select(PlannedItem.payment_tx_id).where(
        PlannedItem.payment_tx_id.is_not(None))))
    led.unlinked = sorted((t for t in led.unlinked if t.id not in group_paid), key=lambda t: t.date)
    led.unlinked_total = sum(abs(t.amount_cents) for t in led.unlinked)
    return led
