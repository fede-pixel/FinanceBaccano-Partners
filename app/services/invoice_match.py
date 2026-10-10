"""Controllo incrociato fatture ↔ movimenti bancari.

Per ogni scadenza di fattura non ancora collegata a un movimento cerca il pagamento (o l'incasso) tra i movimenti:
- importo uguale alla scadenza (o al totale documento) e data compatibile con quella della fattura;
- con importo identico l'abbinamento è automatico; se più fatture o più movimenti hanno lo stesso importo vince quello
  con il nome della controparte o il numero della fattura nella descrizione, altrimenti lo confermi tu.
Un bonifico che paga più fatture dello stesso fornitore viene riconosciuto se la somma delle fatture è esattamente
l'importo del bonifico. Più acconti alla stessa controparte che sommati danno esattamente l'importo della fattura
vengono proposti da confermare a mano. Note di credito e fatture di pari importo dello stesso fornitore si compensano."""
import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Invoice, PlannedItem, Transaction
from app.services import categorize, merchant
from app.services.fatturapa import CREDIT_NOTE_TYPES

DAYS_BEFORE = 30  # pagamenti con carta: il movimento può precedere la data della fattura
DAYS_AFTER = 365
ACCONTI_DAYS_BEFORE = 180  # gli acconti possono essere pagati mesi prima della fattura di saldo
MAX_ACCONTI = 6
# movimenti che non sono mai il pagamento di una fattura: col solo importo non vengono abbinati
NOT_INVOICE = {"Prelievi contanti", "Tasse e contributi", "Banca e commissioni"}
MAX_GROUP = 6

_STOP = {
    "srl", "srls", "spa", "sas", "snc", "sapa", "scarl", "scrl", "coop", "ltd", "limited", "inc", "llc", "gmbh", "sarl",
    "the", "and", "per", "con", "del", "della", "dei", "degli", "di", "da", "ditta", "impresa", "societa", "società",
    "studio", "ass", "associato", "associati", "italia", "italy", "group", "gruppo", "servizi", "service", "services",
    "unipersonale", "semplificata", "europe", "ireland", "emea", "s.r.l", "s.p.a",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9àèéìòù]+", (text or "").lower())


_LEGAL_FORM = re.compile(r"\b(s\.?r\.?l\.?s?|s\.?p\.?a|s\.?n\.?c|s\.?a\.?s|soc\.?|ltd|gmbh|limited|inc)\b", re.I)
# parole troppo comuni nelle ragioni sociali per riconoscere da sole un'azienda
_GENERIC = {
    "logistica", "impianti", "costruzioni", "edilizia", "edile", "motors", "ceramica", "design", "consulting", "sistemi",
    "tecnica", "tecnologie", "commerciale", "trasporti", "global", "italiana", "internazionale", "immobiliare", "studio",
    "gruppo", "group", "service", "servizi", "general", "nuova", "centro", "ferramenta", "elettrica", "termoidraulica",
}


def _key_word(name: str) -> str | None:
    """Per le aziende: la prima parola distintiva della ragione sociale (es. "RENOVATIO SNC DI…" -> "renovatio").
    Le banche spesso troncano il nome del beneficiario: questa parola da sola basta a riconoscerlo."""
    if not _LEGAL_FORM.search(name or ""):
        return None
    for w in _tokens(name):
        if w in _STOP or w.isdigit() or len(w) < 3:
            continue
        return w if len(w) >= 6 and w not in _GENERIC else None
    return None


def _name_words(name: str) -> set[str]:
    words = {w for w in _tokens(name) if len(w) >= 3 and w not in _STOP and not w.isdigit()}
    if words and words <= _GENERIC:
        # nome fatto solo di parole comuni ("AB LOGISTICA"): servono anche le sigle corte per riconoscerlo
        words |= {w for w in _tokens(name) if len(w) == 2 and w not in _STOP and not w.isdigit()}
    return words


def _name_match(words: set[str], brand: str | None, tx_words: set[str], tx_brand: str | None, key: str | None = None) -> bool:
    if brand and brand == tx_brand:
        return True
    if key and key in tx_words:
        return True
    return bool(words) and len(words & tx_words) >= min(2, len(words))


def name_matches(counterparty: str, tx_text: str) -> bool:
    return _name_match(_name_words(counterparty), merchant.brand_of(counterparty), set(_tokens(tx_text)),
                       merchant.brand_of(tx_text), _key_word(counterparty))


def number_matches(number: str, tx_text: str) -> bool:
    parts = [p.lstrip("0") or "0" if p.isdigit() else p for p in _tokens(number)]
    if not parts or len("".join(parts)) < 2:
        return False
    have = {p.lstrip("0") or "0" if p.isdigit() else p for p in _tokens(tx_text)}
    return all(p in have for p in parts)


@dataclass
class Proposal:
    transactions: list[Transaction]  # più movimenti = acconti che insieme saldano la fattura
    items: list[PlannedItem]
    reason: str

    @property
    def transaction(self) -> Transaction:
        return self.transactions[-1]

    @property
    def total_cents(self) -> int:
        return sum(t.amount_cents for t in self.transactions)


@dataclass
class MatchResult:
    matched: int = 0  # scadenze collegate in automatico a un movimento
    compensated: int = 0  # coppie fattura/nota di credito compensate
    proposals: list[Proposal] = field(default_factory=list)


def _open_items(session: Session) -> list[PlannedItem]:
    """Scadenze di fattura senza un movimento collegato (aperte o segnate pagate senza prova)."""
    q = (
        select(PlannedItem)
        .join(Invoice)
        .where(PlannedItem.status != "annullato", PlannedItem.payment_tx_id.is_(None),
               PlannedItem.settlement.not_in(["compensata", "manuale"]))
        .options(selectinload(PlannedItem.invoice), selectinload(PlannedItem.transactions))
    )
    return [it for it in session.scalars(q) if not it.transactions]


def _free_transactions(session: Session) -> list[Transaction]:
    used = set(session.scalars(select(PlannedItem.payment_tx_id).where(PlannedItem.payment_tx_id.is_not(None))))
    q = select(Transaction).where(Transaction.planned_item_id.is_(None)).order_by(Transaction.date)
    return [t for t in session.scalars(q) if t.id not in used and t.category != categorize.INTERNAL]


def _in_window(tx: Transaction, inv: Invoice) -> bool:
    return inv.date - timedelta(days=DAYS_BEFORE) <= tx.date <= inv.date + timedelta(days=DAYS_AFTER)


def _amount_ok(tx: Transaction, it: PlannedItem) -> bool:
    if (tx.amount_cents > 0) != (it.amount_cents > 0):
        return False
    a = abs(tx.amount_cents)
    return abs(a - abs(it.amount_cents)) <= 1 or (it.invoice and abs(a - abs(it.invoice.total_cents)) <= 1
                                                    and len(it.invoice.planned_items) == 1)


def _remember_presumed(it: PlannedItem) -> None:
    if it.status == "pagato" and it.settlement == "" and not it.transactions and not it.payment_tx_id:
        it.prior_paid_date = it.paid_date or it.due_date


def _manual_only(tx: Transaction, items: list[PlannedItem]) -> bool:
    """Abbinamento che l'utente ha già rifiutato (scollegato o "non è pagata"): si propone, non si fa da solo."""
    return bool(tx.no_auto_match) or any(it.no_auto_match for it in items)


def settle(session: Session, tx: Transaction, items: list[PlannedItem]) -> None:
    """Collega il movimento alle scadenze che salda."""
    for n, it in enumerate(items):
        _remember_presumed(it)
        it.status, it.paid_date, it.settlement = "pagato", tx.date, "movimento"
        if it.account_id is None:
            it.account_id = tx.account_id
        if n == 0:
            tx.planned_item = it  # relazione (non solo l'id): così anche item.transactions è subito aggiornato
        if len(items) > 1:
            it.payment_tx_id = tx.id
    session.flush()


def settle_installments(session: Session, txs: list[Transaction], item: PlannedItem) -> None:
    """Più movimenti (acconti) che insieme saldano la stessa scadenza."""
    _remember_presumed(item)
    for tx in txs:
        tx.planned_item = item
    item.status, item.paid_date, item.settlement = "pagato", max(t.date for t in txs), "movimento"
    if item.account_id is None:
        item.account_id = txs[-1].account_id
    session.flush()


def compensate_credit_notes(session: Session, items: list[PlannedItem]) -> int:
    """Nota di credito aperta che annulla una fattura aperta dello stesso fornitore/cliente e di pari importo."""
    by_party: dict[tuple[str, str], list[PlannedItem]] = defaultdict(list)
    for it in items:
        if it.status == "previsto" and not it.no_auto_match:
            by_party[(it.invoice.direction, it.invoice.counterparty_vat or it.invoice.counterparty.lower())].append(it)
    n = 0
    for group in by_party.values():
        notes = [it for it in group if it.invoice.doc_type in CREDIT_NOTE_TYPES]
        invoices = [it for it in group if it.invoice.doc_type not in CREDIT_NOTE_TYPES]
        for note in notes:
            twin = next((i for i in invoices if i.amount_cents == -note.amount_cents and i.status == "previsto"), None)
            if twin is None:
                continue
            for it, other in ((note, twin), (twin, note)):
                it.status, it.paid_date, it.settlement = "pagato", max(note.invoice.date, twin.invoice.date), "compensata"
                it.notes = f"Compensata con {other.invoice.doc_type} n. {other.invoice.number}"
            invoices.remove(twin)
            n += 1
    return n


def run(session: Session, apply: bool = True) -> MatchResult:
    """Esegue il controllo incrociato. Con apply=True salva gli abbinamenti certi; restituisce anche le proposte."""
    res = MatchResult()
    items = _open_items(session)
    if apply:
        res.compensated = compensate_credit_notes(session, items)
        items = [it for it in items if it.settlement != "compensata"]
    txs = _free_transactions(session)
    if not items or not txs:
        return res

    by_amount: dict[int, list[PlannedItem]] = defaultdict(list)
    for it in items:
        by_amount[abs(it.amount_cents)].append(it)
        if len(it.invoice.planned_items) == 1 and abs(it.invoice.total_cents) != abs(it.amount_cents):
            by_amount[abs(it.invoice.total_cents)].append(it)

    def candidates(tx: Transaction) -> list[PlannedItem]:
        a = abs(tx.amount_cents)
        seen, out = set(), []
        for k in (a - 1, a, a + 1):
            for it in by_amount.get(k, []):
                if it.id not in seen and _amount_ok(tx, it) and _in_window(tx, it.invoice):
                    seen.add(it.id)
                    out.append(it)
        return out

    texts = {tx.id: f"{tx.counterparty} {tx.description}" for tx in txs}
    # parole e marchio calcolati una volta sola (le fatture sono centinaia, i movimenti migliaia)
    tx_words = {tx.id: set(_tokens(t)) for tx, t in ((tx, texts[tx.id]) for tx in txs)}
    tx_brand = {tx.id: merchant.brand_of(texts[tx.id]) for tx in txs}
    party: dict[str, tuple[set[str], str | None]] = {}
    for it in items:
        name = it.invoice.counterparty
        if name not in party:
            party[name] = (_name_words(name), merchant.brand_of(name), _key_word(name))
    by_word: dict[str, list[PlannedItem]] = defaultdict(list)
    by_brand: dict[str, list[PlannedItem]] = defaultdict(list)
    for it in items:
        words, brand, _ = party[it.invoice.counterparty]
        for w in words:
            by_word[w].append(it)
        if brand:
            by_brand[brand].append(it)

    def same_party(tx: Transaction, it: PlannedItem) -> bool:
        words, brand, key = party[it.invoice.counterparty]
        return _name_match(words, brand, tx_words[tx.id], tx_brand[tx.id], key)
    certain: list[tuple[float, Transaction, PlannedItem, str]] = []
    amount_only: dict[int, list[PlannedItem]] = {}
    for tx in txs:
        cands = candidates(tx)
        strong = []
        for it in cands:
            by_name = same_party(tx, it)
            by_number = number_matches(it.invoice.number, texts[tx.id])
            if by_name or by_number:
                reason = " e ".join(r for r, ok in (("nome", by_name), ("numero fattura", by_number)) if ok)
                score = (2 if by_name else 0) + (2 if by_number else 0) + (1 if it.status == "previsto" else 0)
                score -= abs((tx.date - it.due_date).days) / 1000  # a parità, la scadenza più vicina
                strong.append((score, tx, it, reason))
        if strong:
            certain.extend(strong)
        elif cands:
            amount_only[tx.id] = cands

    used_tx: set[int] = set()
    used_it: set[int] = set()
    for score, tx, it, reason in sorted(certain, key=lambda c: -c[0]):
        if tx.id in used_tx or it.id in used_it:
            continue
        used_tx.add(tx.id)
        used_it.add(it.id)
        if apply and not _manual_only(tx, [it]):
            settle(session, tx, [it])
            res.matched += 1
        else:
            res.proposals.append(Proposal([tx], [it], f"Stesso importo, {reason}"))

    # un bonifico che paga più fatture della stessa controparte
    for tx in txs:
        if tx.id in used_tx:
            continue
        direction = "passiva" if tx.amount_cents < 0 else "attiva"
        pool = {it.id: it for w in tx_words[tx.id] for it in by_word.get(w, ())}
        if tx_brand[tx.id]:
            pool.update((it.id, it) for it in by_brand.get(tx_brand[tx.id], ()))
        same = sorted(
            (it for it in pool.values() if it.id not in used_it and it.invoice.direction == direction
             and it.invoice.date <= tx.date + timedelta(days=DAYS_BEFORE) and _in_window(tx, it.invoice)
             and same_party(tx, it)),
            key=lambda it: it.due_date,
        )
        if len(same) < 2:
            continue
        same = same[:14]
        found = []
        for size in range(2, min(MAX_GROUP, len(same)) + 1):
            for combo in itertools.combinations(same, size):
                if abs(sum(it.amount_cents for it in combo) - tx.amount_cents) <= len(combo):
                    found.append(combo)
                    if len(found) > 1:
                        break
            if found:
                break
        if not found:
            continue
        combo = list(found[0])
        used_tx.add(tx.id)
        used_it.update(it.id for it in combo)
        if apply and len(found) == 1 and not _manual_only(tx, combo):
            settle(session, tx, combo)
            res.matched += len(combo)
        else:
            res.proposals.append(Proposal([tx], combo, f"Un bonifico per {len(combo)} fatture della stessa controparte"))

    # importo identico al centesimo: abbinamento automatico se il candidato è unico da entrambe le parti,
    # altrimenti (più fatture o più movimenti con lo stesso importo) proposta da confermare
    tx_by_id = {tx.id: tx for tx in txs}
    # controparti di tutte le fatture: se il movimento ne nomina un'altra, non è il pagamento di questa
    all_parties = [(w, b, k) for w, b, k in (
        (_name_words(n), merchant.brand_of(n), _key_word(n)) for n in set(session.scalars(select(Invoice.counterparty)))
    ) if w or b]
    exact: dict[int, list[PlannedItem]] = {}
    for tx_id, cands in amount_only.items():
        if tx_id in used_tx or tx_by_id[tx_id].category in NOT_INVOICE:
            continue
        if any(_name_match(w, b, tx_words[tx_id], tx_brand[tx_id], k) for w, b, k in all_parties):
            continue
        a = abs(tx_by_id[tx_id].amount_cents)
        same = [it for it in cands if it.id not in used_it and a in (abs(it.amount_cents), abs(it.invoice.total_cents))]
        if same:
            exact[tx_id] = same
    claims: dict[int, list[int]] = defaultdict(list)
    for tx_id, cands in exact.items():
        for it in cands:
            claims[it.id].append(tx_id)
    proposed_it: set[int] = set()
    for tx_id, cands in sorted(exact.items(), key=lambda kv: tx_by_id[kv[0]].date):
        tx = tx_by_id[tx_id]
        free = [it for it in cands if it.id not in used_it]
        if not free:
            continue
        if len(free) == 1 and len(claims[free[0].id]) == 1:
            used_tx.add(tx_id)
            used_it.add(free[0].id)
            if apply and not _manual_only(tx, free):
                settle(session, tx, free)
                res.matched += 1
            else:
                res.proposals.append(Proposal([tx], free, "Stesso importo"))
            continue
        # più candidati: propongo il più plausibile non già proposto per un altro movimento (la più vecchia aperta)
        pick = [it for it in free if it.id not in proposed_it] or free
        best = min(pick, key=lambda it: (it.status != "previsto", it.due_date > tx.date, it.due_date))
        proposed_it.add(best.id)
        res.proposals.append(Proposal([tx], [best], f"Stesso importo, ma ci sono {max(len(free), len(claims[best.id]))} "
                                                    "fatture o movimenti con questo importo: scegli tu"))

    # acconti: più pagamenti alla stessa controparte che sommati danno esattamente l'importo della fattura
    by_tx_word: dict[str, list[Transaction]] = defaultdict(list)
    by_tx_brand: dict[str, list[Transaction]] = defaultdict(list)
    for tx in txs:
        if tx.id in used_tx:
            continue
        for w in tx_words[tx.id]:
            by_tx_word[w].append(tx)
        if tx_brand[tx.id]:
            by_tx_brand[tx_brand[tx.id]].append(tx)
    proposed_tx = {t.id for p in res.proposals for t in p.transactions}
    for it in sorted(items, key=lambda i: i.invoice.date):
        if it.id in used_it:
            continue
        words, brand, _ = party[it.invoice.counterparty]
        pool = {t.id: t for w in words for t in by_tx_word.get(w, ())}
        if brand:
            pool.update((t.id, t) for t in by_tx_brand.get(brand, ()))
        inv = it.invoice
        target = abs(it.amount_cents)
        targets = {target, abs(inv.total_cents)} if len(inv.planned_items) == 1 else {target}
        pays = sorted(
            (t for t in pool.values() if t.id not in used_tx and t.id not in proposed_tx
             and (t.amount_cents > 0) == (it.amount_cents > 0) and abs(t.amount_cents) < max(targets)
             and inv.date - timedelta(days=ACCONTI_DAYS_BEFORE) <= t.date <= inv.date + timedelta(days=DAYS_AFTER)
             and same_party(t, it)),
            key=lambda t: t.date,
        )[:14]
        if len(pays) < 2:
            continue
        combo = None
        for size in range(2, min(MAX_ACCONTI, len(pays)) + 1):
            combo = next((c for c in itertools.combinations(pays, size) if abs(sum(t.amount_cents for t in c)) in targets), None)
            if combo:
                break
        if not combo:
            continue
        used_it.add(it.id)
        proposed_tx.update(t.id for t in combo)
        res.proposals.append(Proposal(list(combo), [it], f"{len(combo)} acconti alla stessa controparte che sommati danno "
                                                         "esattamente l'importo della fattura"))
    res.proposals.sort(key=lambda p: p.transaction.date, reverse=True)
    return res


# ---------- situazione fatture ----------

@dataclass
class InvoiceStatus:
    invoice: Invoice
    state: str  # aperta | pagata | compensata | presunta | contenzioso | perdita | nessun_pagamento
    due: date | None  # prima scadenza aperta
    open_cents: int  # residuo (positivo)
    paid_cents: int
    paid_date: date | None
    transactions: list[Transaction]
    items: list[PlannedItem]
    disputed_cents: int = 0  # in contenzioso (legali): non è un incasso atteso
    lost_cents: int = 0  # stralciato: credito perso

    @property
    def overdue_days(self) -> int:
        return (date.today() - self.due).days if self.due and self.state == "aperta" else 0


def invoice_statuses(session: Session, direction: str | None = None) -> list[InvoiceStatus]:
    q = select(Invoice).options(selectinload(Invoice.planned_items).selectinload(PlannedItem.transactions))
    if direction:
        q = q.where(Invoice.direction == direction)
    invoices = list(session.scalars(q))
    group_tx_ids = {it.payment_tx_id for inv in invoices for it in inv.planned_items if it.payment_tx_id}
    group_tx = {t.id: t for t in session.scalars(select(Transaction).where(Transaction.id.in_(group_tx_ids)))} if group_tx_ids else {}
    out = []
    for inv in invoices:
        items = [it for it in inv.planned_items if it.status != "annullato" or it.settlement == "perdita"]
        if not items:
            out.append(InvoiceStatus(inv, "nessun_pagamento", None, 0, 0, None, [], []))
            continue
        open_items = [it for it in items if it.status == "previsto"]
        sign = 1 if inv.doc_type not in CREDIT_NOTE_TYPES else -1
        disputed = sum(abs(it.amount_cents) for it in items if it.status == "contenzioso") * sign
        lost = sum(abs(it.amount_cents) for it in items if it.status == "annullato") * sign
        txs = []
        for it in items:
            txs += it.transactions
            if it.payment_tx_id and it.payment_tx_id in group_tx and group_tx[it.payment_tx_id] not in txs:
                txs.append(group_tx[it.payment_tx_id])
        paid = [it for it in items if it.status == "pagato"]
        if open_items:
            state = "aperta"
        elif disputed:
            state = "contenzioso"
        elif lost:
            state = "perdita"
        elif all(it.settlement == "compensata" for it in paid):
            state = "compensata"
        elif all(it.transactions or it.payment_tx_id or it.settlement in ("compensata", "manuale") for it in paid):
            state = "pagata"
        else:
            state = "presunta"
        out.append(InvoiceStatus(
            invoice=inv,
            state=state,
            due=min((it.due_date for it in open_items), default=None),
            open_cents=sum(abs(it.amount_cents) * (1 if inv.doc_type not in CREDIT_NOTE_TYPES else -1) for it in open_items),
            paid_cents=sum(abs(it.amount_cents) for it in paid),
            paid_date=max((it.paid_date for it in paid if it.paid_date), default=None),
            transactions=txs,
            items=items,
            disputed_cents=disputed,
            lost_cents=lost,
        ))
    return out


# ---------- contenzioso e stralcio ----------

def set_dispute(items: list[PlannedItem], note: str = "") -> int:
    """Credito affidato ai legali: esce dagli incassi previsti (forecast, investimenti, crediti), la fattura resta."""
    n = 0
    for it in items:
        if it.status == "previsto":
            it.status, it.no_auto_match = "contenzioso", True
            it.notes = (f"In contenzioso dal {date.today():%d/%m/%Y}" + (f": {note}" if note else "")
                        + (f" — {it.notes}" if it.notes else ""))
            n += 1
    return n


def write_off(items: list[PlannedItem]) -> int:
    """Stralcio: il credito è considerato perso."""
    n = 0
    for it in items:
        if it.status in ("contenzioso", "previsto"):
            it.status, it.settlement, it.paid_date = "annullato", "perdita", date.today()
            it.notes = f"Stralciato il {date.today():%d/%m/%Y} (credito perso)" + (f" — {it.notes}" if it.notes else "")
            n += 1
    return n


def reactivate(items: list[PlannedItem]) -> int:
    """Torna un credito normale da incassare (es. accordo con il cliente)."""
    n = 0
    for it in items:
        if it.status == "contenzioso" or (it.status == "annullato" and it.settlement == "perdita"):
            it.status, it.settlement, it.paid_date = "previsto", "", None
            n += 1
    return n


# ---------- conferma in blocco (e annullamento) ----------

def confirm_all(session: Session) -> tuple[str, int]:
    """Conferma tutte le proposte "Da confermare" con un unico lotto annullabile. Restituisce (lotto, fatture saldate)."""
    import uuid

    res = run(session)
    batch = uuid.uuid4().hex[:10]
    used_tx: set[int] = set()
    used_it: set[int] = set()
    n = 0
    for p in res.proposals:
        tx_ids = {t.id for t in p.transactions}
        it_ids = {it.id for it in p.items}
        if tx_ids & used_tx or it_ids & used_it:  # proposte in conflitto: vale la prima
            continue
        used_tx |= tx_ids
        used_it |= it_ids
        if len(p.transactions) > 1:
            settle_installments(session, p.transactions, p.items[0])
        else:
            settle(session, p.transactions[0], p.items)
        for it in p.items:
            it.match_batch = batch
        n += len(p.items)
    session.flush()
    return batch, n


def undo_batch(session: Session, batch: str) -> int:
    """Annulla tutti gli abbinamenti confermati in blocco con quel lotto: le fatture tornano da saldare."""
    from app.services import reconcile

    if not batch:
        return 0
    items = list(session.scalars(select(PlannedItem).where(PlannedItem.match_batch == batch)))
    txs: dict[int, Transaction] = {}
    for it in items:
        for t in it.transactions:
            txs[t.id] = t
        if it.payment_tx_id:
            t = session.get(Transaction, it.payment_tx_id)
            if t:
                txs[t.id] = t
    for t in txs.values():
        reconcile.unmatch(session, t)
    for it in items:
        if it.match_batch and not it.transactions and not it.payment_tx_id:
            reconcile._reopen(it)  # segnate pagate in blocco senza movimento
        it.match_batch = ""
    session.flush()
    return len(items)


def batch_size(session: Session, batch: str) -> int:
    if not batch:
        return 0
    return len(list(session.scalars(select(PlannedItem.id).where(PlannedItem.match_batch == batch))))


# ---------- periodo coperto dai movimenti ----------

@dataclass
class Coverage:
    start: date | None  # da quando TUTTI i conti hanno movimenti (prima, un incasso/pagamento può mancare)
    accounts: list[tuple[str, date, date, int]]  # (conto, primo movimento, ultimo, n°)


def coverage(session: Session) -> Coverage:
    from sqlalchemy import func

    from app.models import Account

    rows = session.execute(
        select(Account.name, func.min(Transaction.date), func.max(Transaction.date), func.count(Transaction.id))
        .join(Transaction, Transaction.account_id == Account.id)
        .where(Account.active, Transaction.date >= date(2000, 1, 1))  # escluse le righe con date assurde (es. 1900)
        .group_by(Account.id)
        .order_by(func.min(Transaction.date))
    ).all()
    accounts = [(n, a, b, c) for n, a, b, c in rows]
    return Coverage(max((a for _, a, _, _ in accounts), default=None), accounts)


def mark_presumed(session: Session, statuses: list["InvoiceStatus"]) -> tuple[str, int]:
    """Segna come pagate (senza movimento) le scadenze aperte di queste fatture, con un lotto annullabile."""
    import uuid

    batch = uuid.uuid4().hex[:10]
    n = 0
    for st in statuses:
        for it in st.items:
            if it.status == "previsto":
                it.status, it.paid_date, it.settlement, it.match_batch = "pagato", it.due_date, "", batch
                n += 1
    session.flush()
    return batch, n


# ---------- azioni manuali ----------

def mark_items_paid(session: Session, items: list[PlannedItem], paid_date: date | None) -> tuple[str, int]:
    """Segna pagate a mano più scadenze insieme (lotto annullabile). Senza data si usa la scadenza di ciascuna."""
    import uuid

    batch = uuid.uuid4().hex[:10]
    n = 0
    for it in items:
        if it.status != "previsto":
            continue
        it.status, it.settlement, it.match_batch = "pagato", "manuale", batch
        it.paid_date = paid_date or min(it.due_date, date.today())
        n += 1
    session.flush()
    return batch, n


def partial_payment(session: Session, item: PlannedItem, amount_cents: int, paid_date: date) -> PlannedItem:
    """Registra un pagamento parziale: la parte pagata diventa una voce saldata, la scadenza resta aperta per il resto."""
    amount_cents = abs(amount_cents)
    if not 0 < amount_cents < abs(item.amount_cents):
        raise ValueError("L'importo pagato deve essere maggiore di zero e minore del residuo")
    sign = 1 if item.amount_cents > 0 else -1
    paid = PlannedItem(
        title=f"{item.title} (pagamento parziale)", amount_cents=sign * amount_cents, due_date=paid_date,
        account_id=item.account_id, counterparty=item.counterparty, category=item.category, source=item.source,
        status="pagato", paid_date=paid_date, settlement="manuale",
        notes="Pagamento parziale registrato a mano",
    )
    if item.invoice is not None:
        item.invoice.planned_items.append(paid)  # tramite la relazione: la fattura vede subito la nuova voce
    else:
        paid.invoice_id = item.invoice_id
    session.add(paid)
    item.amount_cents -= sign * amount_cents
    session.flush()
    return paid


@dataclass
class Diagnosis:
    by_name: list[tuple[Transaction, bool]]  # movimenti con il nome della controparte (True = già abbinato ad altro)
    by_amount: list[tuple[Transaction, bool]]  # movimenti con lo stesso importo

    @property
    def summary(self) -> str:
        if not self.by_name and not self.by_amount:
            return ("Nessun movimento con questo nome né con questo importo: probabilmente pagata da un conto o una carta "
                    "non caricati, in contanti, o con una descrizione diversa")
        parts = []
        if self.by_name:
            parts.append(f"{len(self.by_name)} movimenti con il nome ma importo diverso")
        if self.by_amount:
            parts.append(f"{len(self.by_amount)} movimenti con lo stesso importo ma senza il nome")
        return " · ".join(parts)


def diagnose(session: Session, statuses: list["InvoiceStatus"]) -> dict[int, Diagnosis]:
    """Per le fatture aperte: quali movimenti le somigliano e perché non sono stati abbinati."""
    open_st = [s for s in statuses if s.state == "aperta"]
    if not open_st:
        return {}
    txs = list(session.scalars(select(Transaction).where(Transaction.category != categorize.INTERNAL)))
    words = {t.id: set(_tokens(f"{t.counterparty} {t.description}")) for t in txs}
    brands = {t.id: merchant.brand_of(f"{t.counterparty} {t.description}") for t in txs}
    group_paid = set(session.scalars(select(PlannedItem.payment_tx_id).where(PlannedItem.payment_tx_id.is_not(None))))
    by_word: dict[str, list[Transaction]] = defaultdict(list)
    by_amount: dict[int, list[Transaction]] = defaultdict(list)
    for t in txs:
        for w in words[t.id]:
            by_word[w].append(t)
        by_amount[abs(t.amount_cents)].append(t)
    out = {}
    for st in open_st:
        inv = st.invoice
        sign_out = inv.direction == "passiva"
        lo, hi = inv.date - timedelta(days=ACCONTI_DAYS_BEFORE), inv.date + timedelta(days=DAYS_AFTER)
        name_w, brand = _name_words(inv.counterparty), merchant.brand_of(inv.counterparty)
        pool = {t.id: t for w in name_w for t in by_word.get(w, ())}
        named = [t for t in pool.values() if lo <= t.date <= hi and (t.amount_cents < 0) == sign_out
                 and _name_match(name_w, brand, words[t.id], brands[t.id], _key_word(inv.counterparty))]
        if brand:
            named += [t for t in txs if brands[t.id] == brand and t.id not in pool and lo <= t.date <= hi
                      and (t.amount_cents < 0) == sign_out]
        amounts = {abs(it.amount_cents) for it in st.items if it.status == "previsto"} | {abs(inv.total_cents)}
        same_amount = [t for a in amounts for t in by_amount.get(a, ()) if lo <= t.date <= hi
                       and (t.amount_cents < 0) == sign_out and t.id not in {x.id for x in named}]

        def linked(t: Transaction) -> bool:
            return bool(t.planned_item_id) or t.id in group_paid

        out[inv.id] = Diagnosis(
            by_name=[(t, linked(t)) for t in sorted(named, key=lambda t: t.date)][:8],
            by_amount=[(t, linked(t)) for t in sorted(same_amount, key=lambda t: t.date)][:5],
        )
    return out
