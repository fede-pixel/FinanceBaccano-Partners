"""Modifiche che l'assistente può PROPORRE. Non le esegue mai da solo: ogni proposta compare nella chat con il
dettaglio e un pulsante "Esegui"; parte solo quando l'utente lo preme. Ogni azione viene validata quando è proposta
(così l'assistente può correggersi) e di nuovo al momento dell'esecuzione."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CategoryRule, Invoice, PlannedItem, Recurrence, Transaction
from app.services import categorize, invoice_match, reconcile
from app.services.recurrence import FREQUENCIES, sync_recurrence
from app.services.util import fmt_eur


class ActionError(Exception):
    pass


def _tx(session: Session, tx_id: int) -> Transaction:
    tx = session.get(Transaction, tx_id)
    if tx is None:
        raise ActionError(f"movimento {tx_id} inesistente")
    return tx


def _tx_label(tx: Transaction) -> str:
    return f"{tx.date:%d/%m/%Y} · {tx.account.name} · {fmt_eur(tx.amount_cents)} · {tx.description[:60]}"


def _inv_label(inv: Invoice) -> str:
    return (f"{'emessa' if inv.direction == 'attiva' else 'ricevuta'} n. {inv.number} del {inv.date:%d/%m/%Y} – "
            f"{inv.counterparty} – {fmt_eur(inv.total_cents)} (file {inv.file_name})")


def _linked(inv: Invoice) -> bool:
    return any(it.transactions or it.payment_tx_id for it in inv.planned_items)


# ---------- definizione delle azioni: (descrizione per Claude, schema, controllo+descrizione, esecuzione) ----------

def check_delete_invoice(session, fattura_id: int) -> str:
    inv = session.get(Invoice, fattura_id)
    if inv is None:
        raise ActionError(f"fattura {fattura_id} inesistente")
    twins = [i for i in session.scalars(select(Invoice).where(
        Invoice.id != inv.id, Invoice.direction == inv.direction, Invoice.doc_type == inv.doc_type, Invoice.date == inv.date))
        if i.number.strip().lower() == inv.number.strip().lower() and abs(i.total_cents) == abs(inv.total_cents)]
    if not twins:
        raise ActionError("questa fattura non ha un doppione (stesso numero, data, tipo e importo): non la propongo")
    if _linked(inv):
        raise ActionError("questa copia è collegata a un movimento bancario: proponi di eliminare l'altra copia")
    return f"Elimina la fattura doppia {_inv_label(inv)}. Resta l'altra copia (id {twins[0].id})."


def do_delete_invoice(session, fattura_id: int) -> str:
    check_delete_invoice(session, fattura_id)
    inv = session.get(Invoice, fattura_id)
    label = _inv_label(inv)
    session.delete(inv)
    return f"Eliminata: {label}"


def check_delete_tx(session, movimento_id: int) -> str:
    tx = _tx(session, movimento_id)
    warn = "" if (tx.amount_cents == 0 or tx.date.year < 2000) else " ATTENZIONE: è un movimento con importo e data validi."
    return f"Elimina il movimento {_tx_label(tx)}.{warn}"


def do_delete_tx(session, movimento_id: int) -> str:
    tx = _tx(session, movimento_id)
    label = _tx_label(tx)
    if tx.planned_item_id:
        reconcile.unmatch(session, tx)
    session.delete(tx)
    return f"Eliminato il movimento {label}"


def _all_categories(session) -> list[str]:
    custom = set(session.scalars(select(CategoryRule.category).distinct()))
    return sorted(set(categorize.CATEGORIES) | custom | {categorize.INTERNAL, categorize.INCOME})


def _rule_matches(session, testo: str) -> list[Transaction]:
    needed = set(categorize.norm(testo).split())
    out = []
    for tx in session.scalars(select(Transaction)):
        words = set(categorize.norm(f"{tx.counterparty} {tx.description} {categorize.merchant_name(tx)}").split())
        if needed and needed <= words:
            out.append(tx)
    return out


def check_rule(session, testo: str, categoria: str) -> str:
    testo = " ".join(testo.lower().split())
    if len(testo) < 3:
        raise ActionError("testo troppo corto")
    if categoria not in _all_categories(session):
        raise ActionError(f"categoria inesistente; scegli tra: {', '.join(_all_categories(session))}")
    m = _rule_matches(session, testo)
    if not m:
        raise ActionError("nessun movimento contiene queste parole intere (per un IBAN usa l'IBAN completo come scritto nella "
                          "descrizione, leggendolo con esegui_sql)")
    manual = sum(1 for t in m if t.category_source == "manuale")
    return (f"Crea la regola «{testo}» → {categoria}: vale per {len(m)} movimenti attuali"
            f" ({fmt_eur(sum(t.amount_cents for t in m))}) e per i futuri"
            + (f"; {manual} hanno una categoria scelta a mano e restano come sono" if manual else "") + ".")


def do_rule(session, testo: str, categoria: str) -> str:
    desc = check_rule(session, testo, categoria)
    testo = " ".join(testo.lower().split())
    rule = session.scalar(select(CategoryRule).where(CategoryRule.pattern == testo))
    if rule:
        rule.category, rule.priority, rule.source = categoria, 10, "tua"
    else:
        session.add(CategoryRule(pattern=testo, category=categoria, priority=10, source="tua"))
    session.flush()
    categorize.apply(session)
    return desc.replace("Crea la regola", "Creata la regola")


def check_transfer(session, movimenti_ids: list[int]) -> str:
    if not movimenti_ids:
        raise ActionError("nessun movimento indicato")
    txs = [_tx(session, i) for i in movimenti_ids]
    return "Segna come giroconto (trasferimento tra conti propri, escluso da spese e cassa): " + "; ".join(_tx_label(t) for t in txs)


def do_transfer(session, movimenti_ids: list[int]) -> str:
    check_transfer(session, movimenti_ids)
    for i in movimenti_ids:
        tx = _tx(session, i)
        tx.category, tx.category_source = categorize.INTERNAL, "manuale"
    return f"{len(movimenti_ids)} movimenti segnati come giroconto"


def _open_item(session, fattura_id: int) -> tuple[Invoice, PlannedItem]:
    inv = session.get(Invoice, fattura_id)
    if inv is None:
        raise ActionError(f"fattura {fattura_id} inesistente")
    items = [it for it in inv.planned_items if it.status in ("previsto", "contenzioso")]
    if not items:
        raise ActionError("la fattura non ha importi aperti")
    return inv, items[0]


def check_advance(session, movimento_id: int, fattura_id: int) -> str:
    tx = _tx(session, movimento_id)
    inv, item = _open_item(session, fattura_id)
    if tx.planned_item_id:
        raise ActionError("il movimento è già collegato a un'altra voce")
    if (tx.amount_cents > 0) != (inv.direction == "attiva"):
        raise ActionError("segno del movimento incoerente con la fattura (incasso per emesse, pagamento per ricevute)")
    if abs(tx.amount_cents) > abs(item.amount_cents):
        raise ActionError(f"il movimento ({fmt_eur(tx.amount_cents)}) supera il residuo della fattura ({fmt_eur(item.amount_cents)})")
    rest = abs(item.amount_cents) - abs(tx.amount_cents)
    return (f"Collega {_tx_label(tx)} alla {_inv_label(inv)} come "
            + ("pagamento completo." if rest == 0 else f"acconto: la fattura resta aperta per {fmt_eur(rest)}."))


def do_advance(session, movimento_id: int, fattura_id: int) -> str:
    desc = check_advance(session, movimento_id, fattura_id)
    tx = _tx(session, movimento_id)
    _inv, item = _open_item(session, fattura_id)
    tx.no_auto_match = False
    if abs(tx.amount_cents) == abs(item.amount_cents):
        invoice_match.settle(session, tx, [item])
    else:
        paid = invoice_match.partial_payment(session, item, abs(tx.amount_cents), tx.date)
        tx.planned_item = paid
        paid.settlement, paid.notes = "movimento", "Acconto collegato al movimento"
        session.flush()
    return desc.replace("Collega", "Collegato")


def check_recurrence(session, titolo: str, importo: float, tipo: str, frequenza: str, giorno: int, inizio: str,
                     conto_id: int, categoria: str) -> str:
    if not titolo.strip() or importo <= 0:
        raise ActionError("titolo e importo positivo obbligatori")
    if frequenza not in FREQUENCIES:
        raise ActionError(f"frequenza non valida: {', '.join(FREQUENCIES)}")
    try:
        start = date.fromisoformat(inizio)
    except ValueError as exc:
        raise ActionError("data di inizio non valida (YYYY-MM-DD)") from exc
    if not 1 <= giorno <= 31:
        raise ActionError("giorno del mese tra 1 e 31")
    return (f"Crea la ricorrenza «{titolo.strip()}»: {'entrata' if tipo == 'entrata' else 'uscita'} di "
            f"{fmt_eur(int(round(importo * 100)))}, {FREQUENCIES[frequenza].lower()}, il giorno {giorno}, dal {start:%d/%m/%Y}"
            + (f", categoria {categoria}" if categoria else "") + ".")


def do_recurrence(session, titolo, importo, tipo, frequenza, giorno, inizio, conto_id, categoria) -> str:
    desc = check_recurrence(session, titolo, importo, tipo, frequenza, giorno, inizio, conto_id, categoria)
    cents = int(round(importo * 100))
    rec = Recurrence(title=titolo.strip(), amount_cents=cents if tipo == "entrata" else -cents, frequency=frequenza,
                     day_of_month=giorno, start_date=date.fromisoformat(inizio), account_id=conto_id or None,
                     category=categoria.strip(), notes="Creata dall'assistente")
    session.add(rec)
    session.flush()
    sync_recurrence(session, rec)
    return desc.replace("Crea la ricorrenza", "Creata la ricorrenza")


def check_dispute(session, fattura_id: int, nota: str) -> str:
    inv = session.get(Invoice, fattura_id)
    if inv is None:
        raise ActionError(f"fattura {fattura_id} inesistente")
    if inv.direction != "attiva":
        raise ActionError("il contenzioso riguarda le fatture emesse (crediti verso clienti)")
    items = [it for it in inv.planned_items if it.status == "previsto"]
    if not items:
        raise ActionError("la fattura non ha importi ancora da incassare (se c'è un acconto da collegare, proponilo prima)")
    amount = sum(abs(it.amount_cents) for it in items)
    return (f"Metti in contenzioso la {_inv_label(inv)}: {fmt_eur(amount)} non incassati escono da crediti, forecast e "
            f"capacità di investimento (la fattura resta emessa)" + (f". Nota: {nota}" if nota else "") + ".")


def do_dispute(session, fattura_id: int, nota: str) -> str:
    desc = check_dispute(session, fattura_id, nota)
    inv = session.get(Invoice, fattura_id)
    invoice_match.set_dispute([it for it in inv.planned_items if it.status == "previsto"], nota.strip())
    return desc.replace("Metti in contenzioso", "Messa in contenzioso")


def check_note(session, titolo: str, testo: str) -> str:
    if not titolo.strip():
        raise ActionError("titolo obbligatorio")
    return f"Aggiungi alle note per il commercialista: «{titolo.strip()}» — {testo.strip()[:300]}"


def do_note(session, titolo: str, testo: str) -> str:
    from app.services import notes

    notes.create(session, titolo, testo, source="assistente")
    return f"Nota salvata per il commercialista: «{titolo.strip()}» (la trovi in Note per il commercialista)"


ACTIONS = {
    "proponi_elimina_fattura_doppia": (
        "Propone di eliminare UNA copia di una fattura caricata due volte (stesso numero, data, tipo e importo). "
        "Indica l'id della copia NON collegata a movimenti.",
        {"fattura_id": {"type": "integer"}}, check_delete_invoice, do_delete_invoice),
    "proponi_elimina_movimento": (
        "Propone di eliminare un movimento bancario errato (es. righe vuote, importo 0, data 1900).",
        {"movimento_id": {"type": "integer"}}, check_delete_tx, do_delete_tx),
    "proponi_regola_categoria": (
        "Propone una regola di categoria: i movimenti la cui descrizione/controparte contiene TUTTE queste parole intere "
        "(es. nome del fornitore o IBAN completo) prendono la categoria indicata, anche in futuro.",
        {"testo": {"type": "string"}, "categoria": {"type": "string"}}, check_rule, do_rule),
    "proponi_giroconto": (
        "Propone di segnare dei movimenti come giroconto (trasferimenti tra conti propri).",
        {"movimenti_ids": {"type": "array", "items": {"type": "integer"}}}, check_transfer, do_transfer),
    "proponi_acconto": (
        "Propone di collegare un movimento a una fattura: pagamento completo se l'importo coincide, altrimenti acconto "
        "(la fattura resta aperta per il residuo).",
        {"movimento_id": {"type": "integer"}, "fattura_id": {"type": "integer"}}, check_advance, do_advance),
    "proponi_contenzioso": (
        "Propone di mettere in contenzioso una fattura emessa che il cliente non pagherà (pratica ai legali): il residuo "
        "non incassato esce da crediti, forecast e investimenti. Se c'è un acconto, proponi PRIMA il collegamento "
        "dell'acconto (proponi_acconto) e poi il contenzioso.",
        {"fattura_id": {"type": "integer"}, "nota": {"type": "string"}}, check_dispute, do_dispute),
    "proponi_nota_commercialista": (
        "Prepara una nota per il commercialista ogni volta che consigli di chiedergli qualcosa (fiscale, bilancio, IVA, "
        "svalutazioni, contratti, adempimenti). Il testo deve essere autonomo e pronto da inviare: domanda chiara, con "
        "importi, numeri di fattura, date e controparti, senza riferimenti all'app o alla chat.",
        {"titolo": {"type": "string", "description": "Breve, es. 'Svalutazione crediti Barone'"},
         "testo": {"type": "string", "description": "La domanda per lo studio, con i dati necessari"}},
        check_note, do_note),
    "proponi_ricorrenza": (
        "Propone una spesa o entrata ricorrente (stipendi, F24, affitto, leasing, abbonamenti) per il forecast. "
        "Usa importi e giorni ricavati dallo storico dei movimenti e dillo all'utente.",
        {"titolo": {"type": "string"}, "importo": {"type": "number", "description": "In euro, positivo"},
         "tipo": {"type": "string", "enum": ["uscita", "entrata"]},
         "frequenza": {"type": "string", "enum": list(FREQUENCIES)},
         "giorno": {"type": "integer", "description": "Giorno del mese 1-31"},
         "inizio": {"type": "string", "description": "YYYY-MM-DD"},
         "conto_id": {"type": "integer", "description": "0 = nessun conto specifico"},
         "categoria": {"type": "string"}},
        check_recurrence, do_recurrence),
}


def tool_definitions() -> list[dict]:
    return [{
        "name": name,
        "description": desc + " NON esegue nulla: crea una proposta che l'utente conferma con un clic.",
        "input_schema": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
        "strict": True,
    } for name, (desc, props, _c, _d) in ACTIONS.items()]


def propose(session: Session, name: str, args: dict) -> dict:
    """Valida la proposta e la restituisce pronta da salvare nella conversazione (non modifica nulla)."""
    _desc, _props, check, _do = ACTIONS[name]
    summary = check(session, **args)
    return {"id": uuid.uuid4().hex[:10], "tool": name, "args": args, "summary": summary, "status": "proposta"}


def execute(session: Session, action: dict) -> str:
    _desc, _props, _check, do = ACTIONS[action["tool"]]
    return do(session, **action["args"])
