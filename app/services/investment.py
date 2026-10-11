"""Quanto si può investire, e in quali aree, senza mettere a rischio la cassa.

Metodo (tutto in chiaro nella pagina):
1. Si parte dal saldo di oggi dei conti nel forecast.
2. Si proiettano giorno per giorno le entrate e le uscite previste (fatture, ricorrenze, voci manuali e, in futuro,
   i contratti). Gli incassi sono "pesati" secondo lo scenario: nel prudente una parte non arriva o arriva tardi.
3. Le fatture e le ricorrenze coprono solo ciò che è già noto: per i mesi futuri ancora "vuoti" si usa lo storico.
   Ogni mese le entrate sono almeno la media mensile degli incassi degli ultimi 6 mesi (pesata dallo scenario:
   lavoro non ancora fatturato) e le uscite almeno la media delle uscite degli ultimi 6 mesi (costi che arriveranno).
4. Il punto più basso della proiezione, meno una riserva di sicurezza (N mesi di uscite medie, almeno la soglia
   minima), è la liquidità che si può impegnare senza scendere mai sotto la riserva nell'orizzonte scelto.
5. La cifra si divide tra le aree secondo le percentuali scelte. Le assunzioni sono un costo che si ripete: per
   quelle si indica anche il costo mensile sostenibile, confrontato con il margine di cassa ricorrente."""
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PlannedItem, Transaction
from app.services import categorize
from app.services.forecast import calculators, forecast_accounts
from app.services.util import add_months, get_setting, set_setting

SCENARIOS = {
    # incassi previsti, incassi già scaduti, uscite; "history_in" = quota degli incassi medi storici attesa nei mesi
    # non ancora fatturati
    "prudente": {"label": "Prudente", "future_in": 0.85, "overdue_in": 0.5, "out": 1.05, "history_in": 0.5},
    "base": {"label": "Base", "future_in": 1.0, "overdue_in": 0.8, "out": 1.0, "history_in": 0.8},
    "ottimista": {"label": "Ottimista", "future_in": 1.0, "overdue_in": 1.0, "out": 1.0, "history_in": 1.0},
}
AREAS = [
    ("marketing", "Marketing e pubblicità"),
    ("hr", "HR e nuove assunzioni"),
    ("mezzi", "Mezzi, macchinari e attrezzature"),
    ("altro", "Altro / riserva aggiuntiva"),
]
DEFAULTS = {"inv_marketing": "25", "inv_hr": "35", "inv_mezzi": "25", "inv_altro": "15",
            "inv_reserve_months": "2", "inv_hire_cost": "3000",
            "inv_cost_ratio": "70",  # costi stimati in % degli incassi previsti
            "inv_tax_reserve": "0"}  # quota dell'utile accantonata per le imposte


def settings(session: Session) -> dict[str, float]:
    out = {}
    for k, v in DEFAULTS.items():
        raw = get_setting(session, k) or v
        try:
            out[k] = float(str(raw).replace(",", "."))
        except ValueError:
            out[k] = float(v)
    return out


def save_settings(session: Session, values: dict[str, str]) -> None:
    for k in DEFAULTS:
        if k in values and values[k] != "":
            set_setting(session, k, str(values[k]).replace(",", "."))


@dataclass
class Day:
    date: date
    balance: int
    inflow: int
    outflow: int


@dataclass
class Allocation:
    key: str
    label: str
    share: float
    amount: int
    note: str = ""


@dataclass
class Plan:
    today: date
    horizon_months: int
    scenario: str
    balance_today: int
    days: list[Day]
    planned_in: int  # entrate previste pesate nell'orizzonte
    planned_out: int
    hist_in: int  # incassi medi mensili degli ultimi 6 mesi
    avg_monthly_out: int
    recurring_surplus: int  # margine di cassa mensile ricorrente medio (ultimi 6 mesi), può essere negativo
    reserve: int
    min_balance: int
    min_date: date
    investable: int  # spendibile nell'orizzonte, in modo graduale
    investable_now: int = 0  # spendibile subito, tutto in una volta (es. acquisto di un mezzo)
    allocations: list[Allocation] = field(default_factory=list)
    months: list[tuple[str, int, int]] = field(default_factory=list)  # (YYYY-MM, entrate, uscite) pesate
    hire_monthly_budget: int = 0
    hires: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _history(session: Session, today: date) -> tuple[int, int, int]:
    """(entrate al mese, uscite al mese, flusso netto al mese): medie degli ultimi 6 mesi completi, giroconti esclusi.
    Se i movimenti coprono meno di 6 mesi si divide per i mesi effettivamente coperti."""
    end = today.replace(day=1) - timedelta(days=1)
    start = add_months(today.replace(day=1), -6)
    q = select(Transaction.date, Transaction.amount_cents, Transaction.category).where(
        Transaction.date >= start, Transaction.date <= end)
    total_in = total_out = 0
    first = None
    for d, amount, category in session.execute(q):
        if category == categorize.INTERNAL:
            continue
        first = d if first is None or d < first else first
        if amount > 0:
            total_in += amount
        else:
            total_out += -amount
    if first is None:
        return 0, 0, 0
    months = max(1, min(6, round((end - first).days / 30.4)))
    return total_in // months, total_out // months, (total_in - total_out) // months


def build(session: Session, horizon_months: int = 6, scenario: str = "base", today: date | None = None) -> Plan:
    today = today or date.today()
    sc = SCENARIOS.get(scenario, SCENARIOS["base"])
    cfg = settings(session)
    end = add_months(today, horizon_months)
    accounts = forecast_accounts(session)
    ids = [a.id for a in accounts]
    calcs = calculators(session, accounts)
    balance = sum(c.at(today) for c in calcs.values()) if calcs else 0

    q = select(PlannedItem).where(PlannedItem.status == "previsto", PlannedItem.due_date <= end)
    q = q.where((PlannedItem.account_id.is_(None)) | (PlannedItem.account_id.in_(ids)))
    items = list(session.scalars(q))
    by_day: dict[date, list[int]] = {}
    overdue_in = overdue_out = 0
    for p in items:
        if p.due_date < today:  # scaduti: gli incassi pesati per probabilità, i pagamenti da fare subito
            if p.amount_cents > 0:
                overdue_in += round(p.amount_cents * sc["overdue_in"])
            else:
                overdue_out += round(p.amount_cents * sc["out"])
        else:
            w = sc["future_in"] if p.amount_cents > 0 else sc["out"]
            by_day.setdefault(p.due_date, []).append(round(p.amount_cents * w))

    hist_in, avg_out, net_m = _history(session, today)
    # quanto manca, mese per mese, per arrivare ai livelli storici (entrate pesate dallo scenario)
    month_planned: dict[str, list[int]] = {}
    for d0, flows in by_day.items():
        mm = month_planned.setdefault(f"{d0.year}-{d0.month:02d}", [0, 0])
        mm[0] += sum(a for a in flows if a > 0)
        mm[1] += -sum(a for a in flows if a < 0)
    days: list[Day] = []
    running = balance + overdue_in + overdue_out
    planned_in = max(overdue_in, 0)
    planned_out = -min(overdue_out, 0)
    months: dict[str, list[int]] = {}
    carry_in = carry_out = 0.0
    min_balance, min_date = running, today
    d = today
    while d <= end:
        key = f"{d.year}-{d.month:02d}"
        month_days = (add_months(d.replace(day=1), 1) - d.replace(day=1)).days
        p_in, p_out = month_planned.get(key, [0, 0])
        # integrazione giornaliera dallo storico (solo per la parte del mese non già coperta dalle voci previste)
        carry_in += max(0, hist_in * sc["history_in"] - p_in) / month_days
        carry_out += max(0, avg_out * sc["out"] - p_out) / month_days
        extra_in, extra_out = int(carry_in), int(carry_out)
        carry_in -= extra_in
        carry_out -= extra_out
        flows = by_day.get(d, [])
        inflow = sum(a for a in flows if a > 0) + extra_in
        outflow = -sum(a for a in flows if a < 0) + extra_out
        running += inflow - outflow
        planned_in += inflow
        planned_out += outflow
        mm = months.setdefault(key, [0, 0])
        mm[0] += inflow
        mm[1] += outflow
        days.append(Day(d, running, inflow, outflow))
        if running < min_balance:
            min_balance, min_date = running, d
        d += timedelta(days=1)

    threshold = int(get_setting(session, "low_balance_threshold") or 0)
    reserve = max(threshold, int(avg_out * cfg["inv_reserve_months"]))
    investable_now = max(0, min_balance - reserve)
    # spesa distribuita in modo uniforme sull'orizzonte: il budget massimo B tale che, al giorno i,
    # saldo_i − B·i/N resti sopra la riserva per ogni giorno
    n = len(days) - 1
    if days[0].balance < reserve or n <= 0:
        investable = 0
    else:
        investable = max(0, int(min((d_.balance - reserve) * n / i for i, d_ in enumerate(days) if i > 0)))
        investable = max(investable, investable_now)
    total_share = sum(cfg[f"inv_{k}"] for k, _ in AREAS) or 100
    allocations = []
    for k, label in AREAS:
        share = cfg[f"inv_{k}"] / total_share
        allocations.append(Allocation(k, label, share, int(investable * share)))
    plan = Plan(
        today=today, horizon_months=horizon_months, scenario=scenario, balance_today=balance, days=days,
        planned_in=planned_in, planned_out=planned_out, hist_in=hist_in, avg_monthly_out=avg_out,
        recurring_surplus=net_m, reserve=reserve, min_balance=min_balance, min_date=min_date, investable=investable, investable_now=investable_now,
        allocations=allocations, months=[(k, v[0], v[1]) for k, v in months.items()],
    )
    hr = next(a for a in allocations if a.key == "hr")
    # un'assunzione si paga ogni mese: la spesa mensile sostenibile è il minore tra il budget HR spalmato
    # sull'orizzonte e metà del margine di cassa ricorrente (se positivo)
    spread = hr.amount // max(12, horizon_months)  # un'assunzione dura: il budget va spalmato su almeno un anno
    plan.hire_monthly_budget = max(0, min(spread, net_m // 2)) if net_m > 0 else 0
    hire_cost = int(cfg["inv_hire_cost"] * 100)
    plan.hires = plan.hire_monthly_budget / hire_cost if hire_cost else 0
    for a in allocations:
        if a.key == "marketing":
            a.note = f"circa {a.amount // max(1, horizon_months) / 100:,.0f} € al mese per {horizon_months} mesi".replace(",", ".")
        elif a.key == "hr":
            a.note = ("margine di cassa ricorrente non positivo: assunzioni stabili sconsigliate finché non migliora"
                      if net_m <= 0 else
                      f"costo aziendale sostenibile circa {plan.hire_monthly_budget / 100:,.0f} € al mese".replace(",", "."))
        elif a.key == "mezzi":
            now = min(a.amount, investable_now)
            a.note = (f"acquisto immediato fino a circa {now / 100:,.0f} €; il resto con noleggio/leasing o acquisti "
                      f"distribuiti (circa {a.amount // max(1, horizon_months) / 100:,.0f} € al mese)").replace(",", ".")
    if not calcs:
        plan.warnings.append("Nessun conto incluso nel forecast: imposta i conti e il saldo iniziale.")
    if min_balance < reserve:
        plan.warnings.append("Nell'orizzonte la liquidità scende sotto la riserva di sicurezza: oggi non c'è margine per investire.")
    if net_m <= 0 and avg_out:
        plan.warnings.append("Negli ultimi mesi le uscite hanno superato le entrate: gli investimenti si finanzierebbero con la cassa esistente.")
    if not items and not hist_in:
        plan.warnings.append("Non ci sono entrate previste né uno storico di incassi: carica fatture, ricorrenze e movimenti.")
    return plan


def data_freshness(session: Session) -> date | None:
    return session.scalar(select(func.max(Transaction.date)))


# ---------- metodo principale: utile atteso sugli incassi previsti ----------

@dataclass
class ProfitMonth:
    key: str  # YYYY-MM
    inflow: int
    costs: int
    profit: int
    cumulative: int = 0


@dataclass
class ProfitPlan:
    scenario: str
    cost_ratio: float  # 0.70
    tax_ratio: float
    months: list[ProfitMonth]
    overdue: int  # incassi scaduti attesi (pesati dallo scenario), contati nel mese corrente
    overdue_raw: int
    inflow: int
    costs: int
    profit: int
    taxes: int
    investable: int
    allocations: list[Allocation] = field(default_factory=list)
    hire_monthly_budget: int = 0
    hires: float = 0.0
    cash: Plan | None = None  # controllo di liquidità sullo stesso periodo
    actual_cost_ratio: float | None = None  # costi / ricavi dell'anno in corso (dalle fatture), per confronto
    warnings: list[str] = field(default_factory=list)

    @property
    def last_month(self) -> str:
        return self.months[-1].key if self.months else ""


def profit_plan(session: Session, scenario: str = "base", today: date | None = None) -> ProfitPlan:
    """Incassi previsti (fatture emesse, contratti, ricorrenze, altre voci) dal mese corrente all'ultimo incasso
    previsto dei contratti; costi = una percentuale fissa degli incassi; quello che resta è l'utile da investire,
    diviso tra le aree. Il controllo di cassa dice quanto si può spendere subito senza scendere sotto la riserva."""
    from app.services import inflows

    today = today or date.today()
    sc = SCENARIOS.get(scenario, SCENARIOS["base"])
    cfg = settings(session)
    cost_ratio = min(max(cfg["inv_cost_ratio"], 0), 100) / 100
    tax_ratio = min(max(cfg["inv_tax_reserve"], 0), 100) / 100
    fl = inflows.monthly(session, today)
    overdue = round(fl.overdue * sc["overdue_in"])
    months, run = [], 0
    for i, m in enumerate(fl.months):
        inflow = round(m.total * sc["future_in"]) + (overdue if i == 0 else 0)
        costs = round(inflow * cost_ratio)
        run += inflow - costs
        months.append(ProfitMonth(m.key, inflow, costs, inflow - costs, run))
    inflow = sum(m.inflow for m in months)
    costs = sum(m.costs for m in months)
    profit = inflow - costs
    taxes = round(max(profit, 0) * tax_ratio)
    investable = max(0, profit - taxes)
    total_share = sum(cfg[f"inv_{k}"] for k, _ in AREAS) or 100
    allocations = [Allocation(k, label, cfg[f"inv_{k}"] / total_share, int(investable * cfg[f"inv_{k}"] / total_share))
                   for k, label in AREAS]
    n = max(1, len(months))
    cash = build(session, min(12, max(3, n)), scenario, today)
    plan = ProfitPlan(scenario, cost_ratio, tax_ratio, months, overdue, fl.overdue, inflow, costs, profit, taxes, investable,
                      allocations, cash=cash)
    from app.services import kpi

    rep = kpi.build(session, 12, today)
    if rep.ytd_margin is not None:
        plan.actual_cost_ratio = 1 - rep.ytd_margin
    fmt = lambda c: f"{c / 100:,.0f} €".replace(",", ".")  # noqa: E731
    hr = next(a for a in allocations if a.key == "hr")
    plan.hire_monthly_budget = hr.amount // max(12, n)  # un'assunzione dura: il budget va spalmato su almeno un anno
    hire_cost = int(cfg["inv_hire_cost"] * 100)
    plan.hires = plan.hire_monthly_budget / hire_cost if hire_cost else 0
    for a in allocations:
        if a.key == "marketing":
            a.note = f"circa {fmt(a.amount // n)} al mese per {n} mesi, man mano che arrivano gli incassi"
        elif a.key == "hr":
            a.note = (f"circa {fmt(plan.hire_monthly_budget)} al mese di costo aziendale, spalmato su {max(12, n)} mesi: "
                      "un'assunzione resta anche quando questi incassi sono finiti")
        elif a.key == "mezzi":
            now = min(a.amount, cash.investable_now)
            a.note = (f"subito fino a circa {fmt(now)} (liquidità sopra la riserva); il resto quando arrivano gli incassi "
                      "o con leasing/noleggio")
        else:
            a.note = "margine per imprevisti, ritardi negli incassi o altre opportunità"
    if not fl.total and not fl.overdue:
        plan.warnings.append("Non ci sono incassi previsti: carica fatture emesse, contratti e ricorrenze.")
    if cash.min_balance < cash.reserve:
        plan.warnings.append(
            f"Controllo di cassa: il {cash.min_date:%d/%m/%Y} la liquidità prevista scende a {fmt(cash.min_balance)}, sotto la "
            f"riserva di {fmt(cash.reserve)}. L'utile c'è, ma arriva più tardi delle uscite: investi solo dopo gli incassi.")
    if plan.actual_cost_ratio is not None and plan.actual_cost_ratio > cost_ratio + 0.05:
        plan.warnings.append(
            f"Quest'anno i costi (fornitori, autofatture, personale) sono stati il {plan.actual_cost_ratio * 100:.0f}% dei ricavi, "
            f"non il {cost_ratio * 100:.0f}%: con il margine attuale l'utile atteso sarebbe circa "
            f"{fmt(round(inflow * max(0.0, 1 - plan.actual_cost_ratio)))} invece di {fmt(profit)}.")
    if tax_ratio == 0 and profit > 0:
        plan.warnings.append("Nessun accantonamento per le imposte: su questo utile pagherai IRES e IRAP (spesso il 25-30%). "
                             "Imposta la quota nelle impostazioni per non investire soldi che serviranno per le tasse.")
    return plan


# ---------- spese fisse mensili (riferimento: le uscite del forecast restano la percentuale prudente) ----------

FIXED_COSTS_DEFAULT = [("Affitto", 47_000), ("Ads", 150_000), ("Commercialista", 20_800), ("Mengucci", 8_000),
                       ("Assicurazione", 13_000), ("Varie", 14_000)]


def fixed_costs(session: Session) -> list[tuple[str, int]]:
    import json

    raw = get_setting(session, "fixed_costs")
    if not raw:
        return list(FIXED_COSTS_DEFAULT)
    try:
        return [(str(n), int(v)) for n, v in json.loads(raw)]
    except (ValueError, TypeError):
        return list(FIXED_COSTS_DEFAULT)


def parse_fixed_costs(text: str) -> list[tuple[str, int]]:
    """Una voce per riga: "Affitto 470" oppure "Affitto; 470,00"."""
    import re

    from app.services.util import parse_amount

    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(.+?)[\s;:=\t]+(?:€\s*)?([-\d.,]+)\s*€?\s*$", line)
        if m and (cents := parse_amount(m.group(2))) is not None:
            out.append((m.group(1).strip(" ;:"), abs(cents)))
    return out


def save_fixed_costs(session: Session, items: list[tuple[str, int]]) -> None:
    import json

    set_setting(session, "fixed_costs", json.dumps(items))
