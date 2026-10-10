"""Abbinamento tra movimenti reali e voci previste."""
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PlannedItem, Transaction


@dataclass
class Suggestion:
    transaction: Transaction
    item: PlannedItem
    score: float
    certain: bool


def _words(s: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in s).split() if len(w) > 3}


def suggest_matches(session: Session, tolerance_days: int = 7, since: date | None = None) -> list[Suggestion]:
    """Per ogni movimento non abbinato propone la voce prevista più plausibile:
    stesso segno, importo uguale (±1%), data entro la tolleranza."""
    since = since or date.today() - timedelta(days=120)
    txs = list(
        session.scalars(
            select(Transaction).where(Transaction.planned_item_id.is_(None), Transaction.date >= since).order_by(Transaction.date)
        )
    )
    items = list(session.scalars(select(PlannedItem).where(PlannedItem.status == "previsto")))
    used: set[int] = set()
    out: list[Suggestion] = []
    for tx in txs:
        best: tuple[float, PlannedItem] | None = None
        tx_words = _words(f"{tx.description} {tx.counterparty}")
        for it in items:
            if it.id in used or (it.amount_cents > 0) != (tx.amount_cents > 0):
                continue
            if it.account_id and it.account_id != tx.account_id:
                continue
            diff = abs(abs(it.amount_cents) - abs(tx.amount_cents))
            if diff > max(100, abs(it.amount_cents) // 100):
                continue
            days = abs((tx.date - it.due_date).days)
            if days > tolerance_days:
                continue
            score = 100 - days * 5 - (diff / max(abs(it.amount_cents), 1)) * 1000
            if tx_words & _words(f"{it.title} {it.counterparty}"):
                score += 15
            if best is None or score > best[0]:
                best = (score, it)
        if best:
            score, it = best
            used.add(it.id)
            certain = abs(it.amount_cents) == abs(tx.amount_cents) and abs((tx.date - it.due_date).days) <= 3
            out.append(Suggestion(tx, it, score, certain))
    return out


def match(session: Session, tx: Transaction, item: PlannedItem) -> None:
    tx.planned_item_id = item.id
    item.status = "pagato"
    item.paid_date = tx.date
    item.settlement = "movimento"
    if item.account_id is None:
        item.account_id = tx.account_id


def unmatch(session: Session, tx: Transaction) -> None:
    item = tx.planned_item
    tx.planned_item = None
    if item and not item.transactions:
        item.status = "previsto"
        item.paid_date = None
        item.settlement = ""
        item.payment_tx_id = None
    # altre fatture saldate dallo stesso bonifico
    for other in session.scalars(select(PlannedItem).where(PlannedItem.payment_tx_id == tx.id)):
        other.status, other.paid_date, other.settlement, other.payment_tx_id = "previsto", None, "", None
