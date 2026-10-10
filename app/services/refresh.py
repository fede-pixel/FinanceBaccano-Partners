"""Aggiornamento dei dati collegati dopo ogni modifica.

Quasi tutto nell'app è calcolato al momento (saldi, forecast, KPI, IVA, investimenti, situazione fatture, schede
cliente): si aggiorna da solo. Alcune cose invece sono salvate e vanno riallineate quando cambia un dato da cui
dipendono. Dopo ogni salvataggio, caricamento o cancellazione:
1. le ricorrenze e i piani dei contratti rigenerano le voci previste future;
2. le categorie automatiche dei movimenti (e i giroconti tra conti propri) vengono ricalcolate;
3. il controllo incrociato fatture/movimenti fa gli abbinamenti certi;
4. gli Excel dei movimenti generati per il commercialista vengono rifatti se i movimenti del periodo sono cambiati
   (solo per i periodi non ancora inviati; per quelli inviati compare un avviso).

Le scelte fatte a mano (categorie, abbinamenti scollegati, fatture riaperte) non vengono mai scavalcate."""
import hashlib
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, ChecklistItem, DocCategory, Transaction
from app.services import admin_docs, categorize, contracts, invoice_match, recurrence
from app.services.util import get_setting, set_setting

log = logging.getLogger(__name__)


def exports_fingerprint(session: Session, item: ChecklistItem) -> str:
    start, end = admin_docs.period_range(item.period_key)
    kind = item.category.auto_export
    rows = session.execute(
        select(Transaction.id, Transaction.date, Transaction.amount_cents, Transaction.description, Transaction.category)
        .join(Account).where(Account.kind == kind, Account.active, Transaction.date >= start, Transaction.date <= end)
        .order_by(Transaction.id)
    ).all()
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:20]


def refresh_exports(session: Session) -> tuple[int, list[ChecklistItem]]:
    """Rigenera gli export automatici diventati vecchi. Restituisce (rigenerati, periodi già inviati ma cambiati)."""
    regenerated, stale_sent = 0, []
    q = select(ChecklistItem).join(DocCategory).where(DocCategory.auto_export != "")
    for item in session.scalars(q):
        if not any(d.generated for d in item.documents):
            continue
        fp = exports_fingerprint(session, item)
        key = f"export_fp_{item.id}"
        saved = get_setting(session, key)
        if not saved:  # export generato prima di questo controllo: prendo la situazione attuale come riferimento
            set_setting(session, key, fp)
            continue
        if saved == fp:
            continue
        if item.status == "inviato":
            stale_sent.append(item)
            continue
        admin_docs.generate_auto_exports(session, item)
        set_setting(session, key, fp)
        regenerated += 1
    return regenerated, stale_sent


def stale_sent(session: Session) -> list[ChecklistItem]:
    """Periodi già inviati al commercialista i cui movimenti sono cambiati dopo l'invio (sola lettura)."""
    out = []
    q = select(ChecklistItem).join(DocCategory).where(DocCategory.auto_export != "", ChecklistItem.status == "inviato")
    for item in session.scalars(q):
        saved = get_setting(session, f"export_fp_{item.id}")
        if saved and any(d.generated for d in item.documents) and saved != exports_fingerprint(session, item):
            out.append(item)
    return out


def remember_export(session: Session, item: ChecklistItem) -> None:
    """Da chiamare quando gli export vengono generati a mano."""
    set_setting(session, f"export_fp_{item.id}", exports_fingerprint(session, item))


def refresh_all(session: Session) -> dict:
    t0 = time.monotonic()
    recurrence.sync_all(session)
    contracts.sync_all(session)
    recategorized = categorize.apply(session)
    session.flush()
    matched = invoice_match.run(session)
    regenerated, _ = refresh_exports(session)
    session.commit()
    out = {"categorie": recategorized, "abbinamenti": matched.matched + matched.compensated, "export": regenerated,
           "secondi": round(time.monotonic() - t0, 2)}
    log.info("Dati collegati aggiornati: %s", out)
    return out
