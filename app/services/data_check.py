"""Verifica dei dati: controlli automatici su fatture, movimenti e collegamenti, con i totali da confrontare con Aruba."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Invoice, Transaction
from app.services import categorize, invoice_match
from app.services.fatturapa import CREDIT_NOTE_TYPES, SELF_INVOICE_TYPES
from app.services.util import fmt_eur, get_setting


@dataclass
class Check:
    title: str
    status: str  # ok | warn | err | info
    detail: str
    items: list[str] = field(default_factory=list)
    link: str = ""


@dataclass
class Totals:
    year: int
    issued_gross: int  # emesse IVA compresa, autofatture comprese (come la lista di Aruba)
    issued_taxable: int  # emesse imponibile, autofatture escluse (come il grafico di Aruba)
    received_gross: int  # ricevute IVA compresa, autofatture comprese
    received_taxable: int  # ricevute imponibile, autofatture escluse
    self_gross: int
    issued_count: int
    received_count: int


def _signed(inv: Invoice, amount: int) -> int:
    return -abs(amount) if inv.doc_type in CREDIT_NOTE_TYPES else abs(amount)


def totals(session: Session, year: int) -> Totals:
    invs = list(session.scalars(select(Invoice).where(Invoice.date >= date(year, 1, 1), Invoice.date <= date(year, 12, 31))))
    self_inv = [i for i in invs if i.doc_type in SELF_INVOICE_TYPES]
    issued = [i for i in invs if i.direction == "attiva" and i.doc_type not in SELF_INVOICE_TYPES]
    received = [i for i in invs if i.direction == "passiva" and i.doc_type not in SELF_INVOICE_TYPES]
    self_gross = sum(_signed(i, i.total_cents) for i in self_inv)

    def taxable(lst):
        return sum(_signed(i, i.taxable_cents if i.taxable_cents is not None else i.total_cents) for i in lst)

    return Totals(
        year=year,
        issued_gross=sum(_signed(i, i.total_cents) for i in issued) + self_gross,
        issued_taxable=taxable(issued),
        received_gross=sum(_signed(i, i.total_cents) for i in received) + self_gross,
        received_taxable=taxable(received),
        self_gross=self_gross,
        issued_count=len(issued) + len(self_inv),
        received_count=len(received) + len(self_inv),
    )


def run_checks(session: Session, today: date | None = None) -> list[Check]:
    today = today or date.today()
    checks: list[Check] = []
    invoices = list(session.scalars(select(Invoice)))
    own = get_setting(session, "company_name").strip().lower()

    # 1. IVA
    novat = [i for i in invoices if i.vat_cents is None and i.total_cents]
    by_dir = defaultdict(int)
    for i in novat:
        by_dir[i.direction] += 1
    if novat:
        checks.append(Check("IVA delle fatture", "err",
                            f"{by_dir['attiva']} emesse e {by_dir['passiva']} ricevute senza IVA indicata: ricavi, costi e IVA "
                            "risultano sbagliati. Ricarica i report Excel di Aruba (emesse e ricevute) da Fatture (Aruba).",
                            [f"{i.number} del {i.date:%d/%m/%Y} – {i.counterparty}" for i in novat[:8]], "/fatture"))
    else:
        checks.append(Check("IVA delle fatture", "ok", f"Tutte le {len(invoices)} fatture hanno l'IVA indicata."))

    # 2. importi a zero
    zero = [i for i in invoices if not i.total_cents]
    if zero:
        checks.append(Check("Fatture con importo zero", "info",
                            f"{len(zero)} fatture valgono 0 anche nel report di Aruba (documenti a valore zero o importi non letti "
                            "da Aruba). Se dovrebbero avere un importo, caricane gli XML.",
                            [f"{i.number} del {i.date:%d/%m/%Y} – {i.counterparty} ({'emessa' if i.direction == 'attiva' else 'ricevuta'})"
                             for i in zero[:8]], "/fatture"))
    else:
        checks.append(Check("Fatture con importo zero", "ok", "Nessuna."))

    # 3. autofatture
    self_wrong = [i for i in invoices if i.doc_type in SELF_INVOICE_TYPES and
                  (i.direction != "passiva" or (own and own.split()[0] in i.counterparty.lower()))]
    if self_wrong:
        checks.append(Check("Autofatture estere (TD17/TD18)", "warn",
                            f"{len(self_wrong)} autofatture sono ancora tra le emesse o intestate alla vostra società invece che al "
                            "fornitore estero: ricarica il report delle fatture RICEVUTE per completarle.",
                            [f"{i.number} – {i.counterparty}" for i in self_wrong[:8]], "/fatture"))
    else:
        n = sum(1 for i in invoices if i.doc_type in SELF_INVOICE_TYPES)
        checks.append(Check("Autofatture estere (TD17/TD18)", "ok", f"{n} autofatture, tutte tra i costi con il fornitore estero."))

    # 4. possibili doppioni
    seen = defaultdict(list)
    for i in invoices:
        seen[(i.direction, i.doc_type, i.number.strip().lower(), i.date, abs(i.total_cents))].append(i)
    dups = [v for v in seen.values() if len(v) > 1]
    if dups:
        checks.append(Check("Possibili fatture doppie", "warn",
                            f"{len(dups)} fatture compaiono più volte con stesso numero, data e importo.",
                            [f"{v[0].number} del {v[0].date:%d/%m/%Y} – {v[0].counterparty} (×{len(v)})" for v in dups[:8]], "/fatture"))
    else:
        checks.append(Check("Possibili fatture doppie", "ok", "Nessuna."))

    # 4b. acconti TD02 forse contati due volte: la fattura di saldo ha lo stesso imponibile dell'acconto
    from datetime import timedelta
    from itertools import combinations

    suspects = []
    passive = [i for i in invoices if i.direction == "passiva" and i.doc_type not in SELF_INVOICE_TYPES]
    used: set[int] = set()
    # dal più recente: ogni saldo si abbina all'acconto più vicino e una sola volta
    for adv in sorted([i for i in passive if i.doc_type == "TD02" and i.taxable_cents], key=lambda i: i.date, reverse=True):
        later = [i for i in passive if i.id != adv.id and i.id not in used and i.doc_type in ("TD01", "TD24")
                 and i.counterparty == adv.counterparty and adv.date <= i.date <= adv.date + timedelta(days=120) and i.taxable_cents]
        found = next((c for n in (1, 2, 3) for c in combinations(later, n)
                      if abs(sum(i.taxable_cents for i in c) - adv.taxable_cents) <= 100), None)
        if found:
            used.update(i.id for i in found)
            suspects.append((adv, found))
    if suspects:
        tot = sum(a.taxable_cents for a, _ in suspects)
        checks.append(Check("Acconti fornitori forse contati due volte", "warn",
                            f"{len(suspects)} fatture di acconto (TD02) hanno una fattura di saldo successiva con lo stesso imponibile: "
                            f"se il saldo non scala l'acconto, il costo è contato due volte e l'EBITDA risulta più basso fino a "
                            f"{fmt_eur(tot)}. Controlla le fatture (il saldo dovrebbe sottrarre l'acconto) e chiedi al commercialista.",
                            [f"{a.counterparty}: acconto n. {a.number} del {a.date:%d/%m/%Y} ({fmt_eur(a.taxable_cents)}) → saldo "
                             + ", ".join(f"n. {i.number} del {i.date:%d/%m/%Y}" for i in c) for a, c in suspects[:8]]))

    # 5. movimenti: copertura dei conti
    cov = invoice_match.coverage(session)
    lines, status = [], "ok"
    for name, first, last, n in cov.accounts:
        gap_months = []
        months = {(d.year, d.month) for (d,) in session.execute(
            select(Transaction.date).join(Account).where(Account.name == name, Transaction.date >= date(2000, 1, 1)))}
        m = date(first.year, first.month, 1)
        while m <= last:
            if (m.year, m.month) not in months:
                gap_months.append(f"{m.month:02d}/{m.year}")
            m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        stale = (today - last).days
        note = f"{name}: dal {first:%d/%m/%Y} al {last:%d/%m/%Y}, {n} movimenti"
        if gap_months:
            note += f" — mesi senza movimenti: {', '.join(gap_months[:6])}"
            status = "warn"
        if stale > 10:
            note += f" — ultimo movimento {stale} giorni fa"
            status = "warn"
        lines.append(note)
    active_no_tx = [a.name for a in session.scalars(select(Account).where(Account.active))
                    if a.name not in {n for n, *_ in cov.accounts}]
    if active_no_tx:
        status = "warn"
        lines.append("Senza movimenti: " + ", ".join(active_no_tx))
    checks.append(Check("Movimenti bancari caricati", status if cov.accounts else "err",
                        ("Tutti i conti hanno movimenti dal " + cov.start.strftime("%d/%m/%Y") + "." if cov.start else
                         "Nessun movimento caricato.") + (" Controlla i buchi segnalati." if status == "warn" else ""),
                        lines, "/carica"))

    # 5b. righe di movimenti non valide
    bad = list(session.scalars(select(Transaction).where(
        (Transaction.date < date(2000, 1, 1)) | ((Transaction.amount_cents == 0) & (Transaction.description == "")))))
    if bad:
        checks.append(Check("Movimenti non validi", "err",
                            f"{len(bad)} righe con data assurda o importo zero senza descrizione: sono errori di lettura del file, "
                            "cancellale (oppure chiedi all'Assistente di proporne l'eliminazione).",
                            [f"id {t.id} · {t.account.name} · {t.date:%d/%m/%Y} · {fmt_eur(t.amount_cents)}" for t in bad[:8]], "/movimenti"))

    # 6. fatture emesse vecchie ancora aperte
    statuses = invoice_match.invoice_statuses(session)
    old = [s for s in statuses if s.state == "aperta" and s.invoice.direction == "attiva" and s.due and (today - s.due).days > 60]
    if old:
        checks.append(Check("Crediti scaduti da oltre 60 giorni", "warn",
                            f"{len(old)} fatture emesse per {fmt_eur(sum(s.open_cents for s in old))}: incassate ma non abbinate? "
                            "Controlla la scheda cliente; se sono ai legali mettile in contenzioso.",
                            [f"{s.invoice.counterparty} – n. {s.invoice.number}: {fmt_eur(s.open_cents)}" for s in old[:8]],
                            "/fatture/situazione?vista=incassare"))
    else:
        checks.append(Check("Crediti scaduti da oltre 60 giorni", "ok", "Nessuno."))

    # 7. entrate non abbinate a fatture
    year_start = date(today.year, 1, 1)
    unlinked_in = session.scalar(select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
        Transaction.amount_cents > 0, Transaction.date >= year_start, Transaction.category != categorize.INTERNAL,
        Transaction.planned_item_id.is_(None)))
    total_in = session.scalar(select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
        Transaction.amount_cents > 0, Transaction.date >= year_start, Transaction.category != categorize.INTERNAL))
    share = unlinked_in / total_in if total_in else 0
    checks.append(Check("Entrate non collegate a fatture (anno)", "warn" if share > 0.2 else "ok",
                        f"{fmt_eur(unlinked_in)} su {fmt_eur(total_in)} ({share * 100:.0f}%). Possono essere finanziamenti, "
                        "rimborsi o incassi da abbinare." if total_in else "Nessuna entrata nell'anno.",
                        link="/fatture/situazione"))

    # 8. categorie
    out_total = session.scalar(select(func.coalesce(func.sum(-Transaction.amount_cents), 0)).where(
        Transaction.amount_cents < 0, Transaction.date >= year_start, Transaction.category != categorize.INTERNAL))
    other = session.scalar(select(func.coalesce(func.sum(-Transaction.amount_cents), 0)).where(
        Transaction.amount_cents < 0, Transaction.date >= year_start, Transaction.category == categorize.OTHER))
    s_other = other / out_total if out_total else 0
    checks.append(Check("Spese senza categoria (\"Altro\")", "warn" if s_other > 0.15 else "ok",
                        f"{s_other * 100:.0f}% delle uscite dell'anno ({fmt_eur(other)})." if out_total else "Nessuna uscita.",
                        link="/analisi"))

    # 9. invii al commercialista
    from app.services.refresh import stale_sent

    st = stale_sent(session)
    if st:
        checks.append(Check("Excel già inviati al commercialista", "warn",
                            "I movimenti di questi periodi sono cambiati dopo l'invio.",
                            [f"{i.category.name} – {i.period_key}" for i in st], "/documenti"))
    # 10. contenzioso
    disp = [s for s in statuses if s.state in ("contenzioso", "perdita")]
    if disp:
        checks.append(Check("Crediti in contenzioso o stralciati", "info",
                            f"{len(disp)} fatture, {fmt_eur(sum(s.disputed_cents + s.lost_cents for s in disp))}: escluse dagli "
                            "incassi previsti.", [f"{s.invoice.counterparty} – n. {s.invoice.number}" for s in disp[:8]],
                            "/fatture/situazione?vista=contenzioso"))
    if not get_setting(session, "company_vat"):
        checks.append(Check("P.IVA aziendale", "warn", "Non impostata: serve a riconoscere fatture emesse e ricevute negli XML.",
                            link="/impostazioni"))
    return checks
