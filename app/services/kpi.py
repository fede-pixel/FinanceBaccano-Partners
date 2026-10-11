"""KPI economici e finanziari, mese per mese.

Economici (per competenza, dalla data delle fatture, importi al netto dell'IVA):
- Ricavi = imponibile delle fatture emesse (note di credito in negativo).
- Costi esterni = imponibile delle fatture ricevute, comprese le autofatture per servizi esteri (TD17/TD18/TD19),
  che rappresentano il costo di Google, AWS, Anthropic…; note di credito in negativo.
- Personale = uscite bancarie della categoria "Stipendi e collaboratori" (per cassa: il costo del personale
  non passa dalle fatture). I contributi pagati con F24 sono mescolati alle imposte e restano esclusi.
- EBITDA (stimato) = Ricavi − Costi esterni − Personale.  È un'approssimazione gestionale, non il bilancio:
  mancano ratei, risconti, rimanenze e i costi senza fattura elettronica.

Finanziari (per cassa, dai movimenti bancari, giroconti esclusi): entrate, uscite, flusso netto, liquidità a fine mese,
autonomia di cassa (runway), crediti e debiti aperti, tempi medi di incasso (DSO) e di pagamento (DPO)."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice, PlannedItem, Transaction
from app.services import categorize, invoice_match
from app.services.fatturapa import CREDIT_NOTE_TYPES, SELF_INVOICE_TYPES
from app.services.forecast import calculators, forecast_accounts
from app.services.util import add_months

PERSONNEL = "Stipendi e collaboratori"


@dataclass
class Month:
    key: str  # YYYY-MM
    start: date
    end: date
    revenue: int = 0
    external_costs: int = 0
    personnel: int = 0
    self_invoice_costs: int = 0  # di cui autofatture per servizi esteri (escluse dal "fatturato passivo" di Aruba)
    capex: int = 0  # fatture di beni da ammortizzare: investimenti, fuori dai costi dell'EBITDA
    excluded_costs: int = 0  # acconti già compresi nella fattura di saldo (altrimenti contati due volte)
    cash_in: int = 0
    cash_out: int = 0
    cash_in_invoices: int = 0  # entrate collegate a fatture emesse (incassi)
    cash_out_invoices: int = 0  # uscite collegate a fatture ricevute (pagamenti)
    end_balance: int | None = None
    invoices_without_vat: int = 0

    @property
    def ebitda(self) -> int:
        return self.revenue - self.external_costs - self.personnel

    @property
    def margin(self) -> float | None:
        return self.ebitda / self.revenue if self.revenue > 0 else None

    @property
    def net_cash(self) -> int:
        return self.cash_in - self.cash_out


@dataclass
class KPIReport:
    months: list[Month]
    ytd_revenue: int
    ytd_ebitda: int
    ytd_margin: float | None
    ytd_cash: int
    revenue_growth: float | None  # ultimi 3 mesi vs 3 precedenti
    balance_today: int
    avg_monthly_out: int  # uscite medie degli ultimi 3 mesi completi
    runway_months: float | None
    receivables: int
    receivables_overdue: int
    payables: int
    payables_overdue: int
    dso: float | None
    dpo: float | None
    top_clients: list[tuple[str, int, float]] = field(default_factory=list)  # (cliente, ricavi 12 mesi, quota)
    invoices_without_vat: int = 0
    coverage_start: date | None = None
    disputed: int = 0  # crediti in contenzioso (IVA compresa)
    lost: int = 0  # crediti stralciati
    recon: dict = field(default_factory=dict)  # confronto con il "fatturato attivo − passivo" di Aruba (anno in corso)
    ytd_revenue_without_vat: int = 0  # parte dei ricavi dell'anno presa da fatture senza IVA nota (quindi IVA compresa)
    ytd_revenue_invoices_without_vat: int = 0


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _signed_taxable(inv: Invoice) -> tuple[int, bool]:
    """Imponibile con segno (note di credito negative). Il secondo valore è True se l'IVA non è nota
    (si usa il totale: sovrastima di circa il 22%)."""
    base = inv.taxable_cents
    missing = base is None
    if missing:
        base = abs(inv.total_cents)
    return (-base if inv.doc_type in CREDIT_NOTE_TYPES else base), missing


COST_EXCLUDED = ("cespite", "escluso")


def uncollectible(session: Session) -> dict[int, int]:
    """Fatture emesse che non verranno pagate (in contenzioso o stralciate): {id fattura: imponibile non incassabile}.
    È la quota del totale non coperta dagli acconti già incassati, al netto dell'IVA. Escono da ricavi ed EBITDA in
    tutte le dashboard e previsioni."""
    out = {}
    for st in invoice_match.invoice_statuses(session, "attiva"):
        bad = st.disputed_cents + st.lost_cents
        if not bad:
            continue
        inv = st.invoice
        share = (inv.taxable_cents / abs(inv.total_cents)) if inv.taxable_cents is not None and inv.total_cents else 1.0
        out[inv.id] = int(bad * share)
    return out


def _paid_days(statuses, direction: str, since: date) -> float | None:
    """Giorni medi (ponderati per importo) tra data fattura e pagamento verificato da movimento."""
    num = den = 0
    for s in statuses:
        inv = s.invoice
        if inv.direction != direction or s.state != "pagata" or not s.paid_date or inv.doc_type in CREDIT_NOTE_TYPES:
            continue
        if s.paid_date < since or not s.transactions:
            continue
        w = abs(inv.total_cents)
        num += max(0, (s.paid_date - inv.date).days) * w
        den += w
    return num / den if den else None


def build(session: Session, months: int = 12, today: date | None = None) -> KPIReport:
    today = today or date.today()
    shown_from = add_months(_month_start(today), -(months - 1))
    first = min(shown_from, date(today.year, 1, 1))  # i totali "dell'anno" partono sempre da gennaio
    rows: list[Month] = []
    m = first
    while m <= today:
        nxt = add_months(m, 1)
        rows.append(Month(key=f"{m.year}-{m.month:02d}", start=m, end=nxt - timedelta(days=1)))
        m = nxt
    by_key = {r.key: r for r in rows}

    def bucket(d: date) -> Month | None:
        return by_key.get(f"{d.year}-{d.month:02d}")

    clients: dict[str, int] = defaultdict(int)
    unc = uncollectible(session)
    unc_year = 0
    without_vat = 0
    ytd_rev_novat = ytd_rev_novat_n = 0
    for inv in session.scalars(select(Invoice).where(Invoice.date >= first, Invoice.date <= today)):
        r = bucket(inv.date)
        if r is None:
            continue
        amount, missing = _signed_taxable(inv)
        if missing and inv.total_cents:
            r.invoices_without_vat += 1
            without_vat += 1
        if inv.direction == "attiva" and inv.doc_type not in SELF_INVOICE_TYPES:
            lost = unc.get(inv.id, 0)  # crediti che non verranno pagati: fuori dai ricavi
            amount -= lost
            if inv.date.year == today.year:
                unc_year += lost
            r.revenue += amount
            if missing and inv.date.year == today.year:
                ytd_rev_novat += amount
                ytd_rev_novat_n += 1
            clients[inv.counterparty] += amount
        elif inv.cost_treatment == "cespite":
            r.capex += amount
        elif inv.cost_treatment == "escluso":
            r.excluded_costs += amount
        else:  # fatture ricevute e autofatture (anche se caricate tra le emesse): costi
            r.external_costs += amount
            if inv.doc_type in SELF_INVOICE_TYPES:
                r.self_invoice_costs += amount

    # movimenti che saldano una fattura (abbinamento diretto o bonifico unico per più fatture)
    invoice_tx = {tx_id for (tx_id,) in session.execute(
        select(Transaction.id).join(PlannedItem, Transaction.planned_item_id == PlannedItem.id).where(PlannedItem.invoice_id.is_not(None)))}
    invoice_tx |= set(session.scalars(select(PlannedItem.payment_tx_id).where(
        PlannedItem.payment_tx_id.is_not(None), PlannedItem.invoice_id.is_not(None))))
    txs = session.execute(
        select(Transaction.id, Transaction.date, Transaction.amount_cents, Transaction.category)
        .where(Transaction.date >= first, Transaction.date <= today)
    )
    for tx_id, d, amount, category in txs:
        r = bucket(d)
        if r is None or category == categorize.INTERNAL:
            continue
        if amount > 0:
            r.cash_in += amount
            if tx_id in invoice_tx:
                r.cash_in_invoices += amount
        else:
            r.cash_out += -amount
            if tx_id in invoice_tx:
                r.cash_out_invoices += -amount
            if category == PERSONNEL:
                r.personnel += -amount

    accounts = forecast_accounts(session)
    calcs = calculators(session, accounts)
    for r in rows:
        r.end_balance = sum(c.at(min(r.end, today)) for c in calcs.values()) if calcs else None
    balance_today = sum(c.at(today) for c in calcs.values()) if calcs else 0

    year_rows = [r for r in rows if r.start.year == today.year]
    recon = {
        "revenue": sum(r.revenue for r in year_rows),
        # come Aruba: tutte le fatture ricevute; cespiti e acconti esclusi si tolgono dopo, verso l'EBITDA
        "costs": sum(r.external_costs + r.capex + r.excluded_costs for r in year_rows),
        "capex": sum(r.capex for r in year_rows),
        "excluded_costs": sum(r.excluded_costs for r in year_rows),
        "self_costs": sum(r.self_invoice_costs for r in year_rows),
        "personnel": sum(r.personnel for r in year_rows),
    }
    year_inv = list(session.scalars(select(Invoice).where(Invoice.date >= date(today.year, 1, 1), Invoice.date <= today)))
    for direction, key in (("attiva", "rev"), ("passiva", "cost")):
        recon[f"{key}_novat"] = sum(1 for i in year_inv if i.direction == direction and i.vat_cents is None and i.total_cents)
        recon[f"{key}_zero"] = sum(1 for i in year_inv if i.direction == direction and not i.total_cents)
    recon["revenue_issued"] = recon["revenue"] + unc_year  # come su Aruba: anche le fatture che non verranno pagate
    recon["aruba_like"] = recon["revenue_issued"] - (recon["costs"] - recon["self_costs"])
    # stessi conti IVA compresa (come i totali della lista di Aruba e come esce la fattura)
    def gross(i: Invoice) -> int:
        return (-1 if i.doc_type in CREDIT_NOTE_TYPES else 1) * abs(i.total_cents)
    recon["revenue_gross"] = sum(gross(i) for i in year_inv if i.direction == "attiva" and i.doc_type not in SELF_INVOICE_TYPES)
    recon["costs_gross"] = sum(gross(i) for i in year_inv if i.direction == "passiva" and i.doc_type not in SELF_INVOICE_TYPES)
    recon["self_gross"] = sum(gross(i) for i in year_inv if i.doc_type in SELF_INVOICE_TYPES)
    recon["diff_gross"] = recon["revenue_gross"] - recon["costs_gross"]
    recon["net_vat"] = recon["diff_gross"] - recon["aruba_like"]  # IVA sulle vendite − IVA sugli acquisti
    ytd_revenue = sum(r.revenue for r in year_rows)
    ytd_ebitda = sum(r.ebitda for r in year_rows)
    complete = [r for r in rows if r.end < _month_start(today)]
    last3, prev3 = complete[-3:], complete[-6:-3]
    rev_last, rev_prev = sum(r.revenue for r in last3), sum(r.revenue for r in prev3)
    avg_out = sum(r.cash_out for r in last3) // len(last3) if last3 else 0

    statuses = invoice_match.invoice_statuses(session)
    open_rows = [s for s in statuses if s.state == "aperta"]

    def open_total(direction: str, overdue: bool = False) -> int:
        return sum(s.open_cents for s in open_rows if s.invoice.direction == direction and (not overdue or (s.due and s.due < today)))

    # crediti in contenzioso/stralciati sulle fatture dell'anno (imponibile): già tolti da ricavi ed EBITDA
    recon["disputed_taxable"] = unc_year
    recon["ebitda_prudent"] = ytd_ebitda
    total_clients = sum(v for v in clients.values() if v > 0)
    top = sorted(((n, v, v / total_clients if total_clients else 0) for n, v in clients.items() if v > 0), key=lambda x: -x[1])[:5]
    return KPIReport(
        months=[r for r in rows if r.start >= shown_from],
        recon=recon,
        ytd_revenue=ytd_revenue,
        ytd_ebitda=ytd_ebitda,
        ytd_margin=ytd_ebitda / ytd_revenue if ytd_revenue > 0 else None,
        ytd_cash=sum(r.net_cash for r in year_rows),
        revenue_growth=(rev_last - rev_prev) / rev_prev if rev_prev > 0 and len(prev3) == 3 else None,
        balance_today=balance_today,
        avg_monthly_out=avg_out,
        runway_months=balance_today / avg_out if avg_out > 0 else None,
        receivables=open_total("attiva"),
        disputed=sum(x.disputed_cents for x in statuses if x.invoice.direction == "attiva"),
        lost=sum(x.lost_cents for x in statuses if x.invoice.direction == "attiva"),
        receivables_overdue=open_total("attiva", True),
        payables=open_total("passiva"),
        payables_overdue=open_total("passiva", True),
        dso=_paid_days(statuses, "attiva", today - timedelta(days=365)),
        dpo=_paid_days(statuses, "passiva", today - timedelta(days=365)),
        top_clients=top,
        invoices_without_vat=without_vat,
        ytd_revenue_without_vat=ytd_rev_novat,
        ytd_revenue_invoices_without_vat=ytd_rev_novat_n,
        coverage_start=invoice_match.coverage(session).start,
    )


# ---------- IVA ----------

@dataclass
class VatPeriod:
    key: str  # 2026-03 oppure 2026-T1
    label: str
    debit: int = 0  # IVA sulle fatture emesse
    credit: int = 0  # IVA sulle fatture ricevute
    carried_in: int = 0  # credito del periodo precedente
    to_pay: int = 0  # da versare con F24
    carried_out: int = 0  # credito che passa al periodo dopo
    due: date | None = None  # scadenza del versamento
    missing_vat: int = 0  # fatture del periodo senza IVA nota

    @property
    def balance(self) -> int:
        """Saldo del solo periodo: positivo = debito, negativo = credito."""
        return self.debit - self.credit


@dataclass
class VatReport:
    periods: list[VatPeriod]
    frequency: str  # mensile | trimestrale
    total_debit: int
    total_credit: int
    total_to_pay: int
    credit_now: int  # credito IVA ancora da usare
    missing_vat: int
    self_invoices_vat: int  # IVA delle autofatture (reverse charge: a debito e a credito, si annulla)


MONTHS_SHORT = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def vat_report(session: Session, year: int, frequency: str = "trimestrale", today: date | None = None) -> VatReport:
    """Liquidazione IVA stimata dalle fatture (per data documento). Ogni periodo: IVA a debito − IVA a credito − credito
    riportato; se positivo si versa (entro il 16 del mese dopo, o del secondo mese dopo il trimestre), se negativo il
    credito passa al periodo successivo. Non considera acconto di dicembre, interessi dell'1% trimestrale, IVA indetraibile
    (es. auto, ristoranti), split payment ed esigibilità differita: è una stima per la pianificazione, non la liquidazione."""
    today = today or date.today()
    quarterly = frequency == "trimestrale"
    keys = [f"{year}-T{q}" for q in range(1, 5)] if quarterly else [f"{year}-{m:02d}" for m in range(1, 13)]
    periods = {k: VatPeriod(k, (f"{k[-2:]} {year}" if quarterly else f"{MONTHS_SHORT[int(k[-2:]) - 1]} {year}")) for k in keys}
    self_vat = missing = 0
    q = select(Invoice).where(Invoice.date >= date(year, 1, 1), Invoice.date <= date(year, 12, 31))
    for inv in session.scalars(q):
        key = f"{year}-T{(inv.date.month - 1) // 3 + 1}" if quarterly else f"{year}-{inv.date.month:02d}"
        p = periods[key]
        if inv.vat_cents is None:
            if inv.total_cents:
                p.missing_vat += 1
                missing += 1
            continue
        vat = abs(inv.vat_cents) * (-1 if inv.doc_type in CREDIT_NOTE_TYPES else 1)
        if inv.doc_type in SELF_INVOICE_TYPES:
            self_vat += vat  # reverse charge: stessa IVA a debito e a credito
        elif inv.direction == "attiva":
            p.debit += vat
        else:
            p.credit += vat
    carry = 0
    out = []
    for k in keys:
        p = periods[k]
        p.carried_in = carry
        net = p.debit - p.credit - carry
        if net > 0:
            p.to_pay, carry = net, 0
        else:
            p.to_pay, carry = 0, -net
        p.carried_out = carry
        if quarterly:
            qn = int(k[-1])
            # T1 16/5, T2 20/8, T3 16/11, T4 con la dichiarazione annuale (16/3 dell'anno dopo)
            p.due = {1: date(year, 5, 16), 2: date(year, 8, 20), 3: date(year, 11, 16), 4: date(year + 1, 3, 16)}[qn]
            first_month = qn * 3 - 2
            last_month = qn * 3
        else:
            m = int(k[-2:])
            p.due = date(year + (m == 12), m % 12 + 1, 16)
            first_month = last_month = m
        if date(year, first_month, 1) > today:
            break  # periodi futuri: non ancora iniziati
        if date(year, first_month, 1) <= today < add_months(date(year, last_month, 1), 1):
            p.label += " (in corso)"
        out.append(p)
    return VatReport(
        periods=out, frequency="trimestrale" if quarterly else "mensile",
        total_debit=sum(p.debit for p in out), total_credit=sum(p.credit for p in out),
        total_to_pay=sum(p.to_pay for p in out), credit_now=out[-1].carried_out if out else 0,
        missing_vat=missing, self_invoices_vat=self_vat,
    )


# ---------- confronto con una chiusura provvisoria del commercialista ----------

@dataclass
class Provisional:
    cutoff: date
    revenue: int
    costs_known: int  # fatture ricevute già arrivate alla data di chiusura
    costs_all: int  # tutte le fatture con data documento entro la chiusura (anche arrivate dopo)
    late: list[tuple[str, str, date, date | None, int]]  # (numero, fornitore, data doc, ricevuta il, imponibile)
    personnel: int

    @property
    def margin_known(self) -> int:
        return self.revenue - self.costs_known - self.personnel

    @property
    def margin_all(self) -> int:
        return self.revenue - self.costs_all - self.personnel


def provisional_view(session: Session, cutoff: date) -> Provisional:
    """Risultato da inizio anno alla data di chiusura: come lo vedeva il commercialista (solo le fatture già arrivate)
    e come risulta oggi (anche i costi con data entro la chiusura ma arrivati dopo)."""
    start = date(cutoff.year, 1, 1)
    invs = list(session.scalars(select(Invoice).where(Invoice.date >= start, Invoice.date <= cutoff)))
    unc = uncollectible(session)
    revenue = costs_all = costs_known = 0
    late = []
    for inv in invs:
        amount, _ = _signed_taxable(inv)
        if inv.direction == "attiva" and inv.doc_type not in SELF_INVOICE_TYPES:
            revenue += amount - unc.get(inv.id, 0)
            continue
        if inv.cost_treatment in COST_EXCLUDED:
            continue
        costs_all += amount
        if inv.received_date is None or inv.received_date <= cutoff:
            costs_known += amount
        else:
            late.append((inv.number, inv.counterparty, inv.date, inv.received_date, amount))
    personnel = sum(-a for (a,) in session.execute(select(Transaction.amount_cents).where(
        Transaction.date >= start, Transaction.date <= cutoff, Transaction.category == PERSONNEL, Transaction.amount_cents < 0)))
    late.sort(key=lambda x: -x[4])
    return Provisional(cutoff, revenue, costs_known, costs_all, late, personnel)


# ---------- previsione al 31 dicembre ----------

@dataclass
class YearEndForecast:
    end: date
    revenue_done: int  # ricavi già fatturati (imponibile)
    revenue_to_come: int  # rate dei contratti e altre entrate previste da fatturare entro il 31/12 (imponibile)
    costs_done: int  # costi fornitori + autofatture già fatturati
    personnel_done: int
    cost_ratio: float
    costs_to_come: int  # stima: cost_ratio dei ricavi ancora da fatturare
    disputed: int  # crediti in contenzioso/stralciati (imponibile)

    @property
    def revenue(self) -> int:
        return self.revenue_done + self.revenue_to_come

    @property
    def ebitda_done(self) -> int:
        return self.revenue_done - self.costs_done - self.personnel_done

    @property
    def ebitda(self) -> int:
        return self.ebitda_done + self.revenue_to_come - self.costs_to_come

    @property
    def ebitda_prudent(self) -> int:
        return self.ebitda - self.disputed

    @property
    def margin(self) -> float | None:
        return self.ebitda / self.revenue if self.revenue > 0 else None


def year_end_forecast(session: Session, rep: KPIReport, today: date | None = None) -> YearEndForecast:
    """EBITDA dell'anno a oggi + ricavi ancora da fatturare entro il 31/12 (contratti, ricorrenze, altre entrate,
    IVA scorporata) − costi stimati sugli stessi ricavi con la percentuale della capacità di investimento."""
    from app.services import inflows
    from app.services.investment import settings as inv_settings

    today = today or date.today()
    out = inflows.revenue_outlook(session, today)
    ratio = min(max(inv_settings(session)["inv_cost_ratio"], 0), 100) / 100
    r = rep.recon
    to_come = out.year_end.to_come
    return YearEndForecast(
        end=out.year_end.end, revenue_done=rep.ytd_revenue, revenue_to_come=to_come,
        costs_done=r.get("costs", 0) - r.get("capex", 0) - r.get("excluded_costs", 0), personnel_done=r.get("personnel", 0),
        cost_ratio=ratio, costs_to_come=round(to_come * ratio), disputed=0,  # i crediti persi sono già fuori dai ricavi
    )
