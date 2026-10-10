"""Saldi e previsione della liquidità giorno per giorno."""
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, BalanceSnapshot, PlannedItem, Transaction
from app.services.util import get_setting


class BalanceCalculator:
    """Calcola il saldo di un conto in qualsiasi data partendo dai saldi certi (snapshot) e dai movimenti."""

    def __init__(self, snapshots: list[BalanceSnapshot], txs: list[tuple[date, int]]):
        self.snapshots = sorted(((s.date, s.balance_cents) for s in snapshots), key=lambda x: x[0])
        txs = sorted(txs)
        self.dates = [d for d, _ in txs]
        self.prefix: list[int] = []
        total = 0
        for _, amount in txs:
            total += amount
            self.prefix.append(total)

    def _cum(self, d: date) -> int:
        """Somma dei movimenti con data <= d."""
        i = bisect_right(self.dates, d)
        return self.prefix[i - 1] if i else 0

    def at(self, d: date) -> int:
        if not self.snapshots:
            return self._cum(d)
        anchor = self.snapshots[0]
        for snap in self.snapshots:
            if snap[0] <= d:
                anchor = snap
            else:
                break
        return anchor[1] + self._cum(d) - self._cum(anchor[0])


def forecast_accounts(session: Session, account_ids: list[int] | None = None) -> list[Account]:
    q = select(Account).where(Account.active, Account.include_in_forecast, Account.currency == "EUR")
    if account_ids:
        q = q.where(Account.id.in_(account_ids))
    return list(session.scalars(q.order_by(Account.id)))


def calculators(session: Session, accounts: list[Account]) -> dict[int, BalanceCalculator]:
    ids = [a.id for a in accounts]
    txs: dict[int, list[tuple[date, int]]] = {i: [] for i in ids}
    if ids:
        for acc_id, d, amount in session.execute(
            select(Transaction.account_id, Transaction.date, Transaction.amount_cents).where(Transaction.account_id.in_(ids))
        ):
            txs[acc_id].append((d, amount))
    return {a.id: BalanceCalculator(a.snapshots, txs[a.id]) for a in accounts}


@dataclass
class AccountStatus:
    account: Account
    balance: int
    last_tx_date: date | None
    last_snapshot: date | None


def account_statuses(session: Session, today: date) -> list[AccountStatus]:
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.kind, Account.id)))
    calcs = calculators(session, accounts)
    last_tx = dict(
        session.execute(select(Transaction.account_id, func.max(Transaction.date)).group_by(Transaction.account_id)).all()
    )
    return [
        AccountStatus(
            account=a,
            balance=calcs[a.id].at(today),
            last_tx_date=last_tx.get(a.id),
            last_snapshot=a.snapshots[-1].date if a.snapshots else None,
        )
        for a in accounts
    ]


@dataclass
class ForecastDay:
    date: date
    actual: int | None = None  # saldo reale (giorni passati e oggi)
    projected: int | None = None  # saldo previsto (oggi e futuro)
    inflow: int = 0
    outflow: int = 0
    items: list[PlannedItem] = field(default_factory=list)
    below_threshold: bool = False


@dataclass
class Forecast:
    days: list[ForecastDay]
    today: date
    threshold: int
    balance_today: int
    overdue: list[PlannedItem]
    overdue_total: int
    overdue_included: bool
    end_balance: int | None
    min_balance: int | None
    min_date: date | None
    planned_in: int
    planned_out: int
    accounts: list[Account]


def build_forecast(
    session: Session,
    start: date,
    end: date,
    today: date | None = None,
    account_ids: list[int] | None = None,
) -> Forecast:
    today = today or date.today()
    threshold = int(get_setting(session, "low_balance_threshold") or 0)
    include_overdue = get_setting(session, "include_overdue_in_forecast") == "1"
    accounts = forecast_accounts(session, account_ids)
    calcs = calculators(session, accounts)
    ids = [a.id for a in accounts]

    def actual_total(d: date) -> int:
        return sum(c.at(d) for c in calcs.values())

    q = select(PlannedItem).where(PlannedItem.status == "previsto", PlannedItem.due_date <= end)
    if account_ids:
        q = q.where(PlannedItem.account_id.in_(ids))
    else:
        # voci senza conto contano sul totale; escludo quelle legate a conti fuori forecast
        q = q.where((PlannedItem.account_id.is_(None)) | (PlannedItem.account_id.in_(ids)))
    open_items = list(session.scalars(q.order_by(PlannedItem.due_date, PlannedItem.id)))
    overdue = [p for p in open_items if p.due_date < today]
    overdue_total = sum(p.amount_cents for p in overdue)
    by_day: dict[date, list[PlannedItem]] = {}
    for p in open_items:
        if p.due_date >= today:
            by_day.setdefault(p.due_date, []).append(p)

    # Movimenti reali per i giorni passati (per mostrare entrate/uscite effettive)
    past_flows: dict[date, list[int]] = {}
    if ids and start <= today:
        for d, amount in session.execute(
            select(Transaction.date, Transaction.amount_cents).where(
                Transaction.account_id.in_(ids), Transaction.date >= start, Transaction.date < min(end + timedelta(days=1), today)
            )
        ):
            past_flows.setdefault(d, []).append(amount)

    balance_today = actual_total(today)
    running = balance_today + (overdue_total if include_overdue else 0)
    # Voci previste tra oggi e l'inizio del periodo (periodi futuri)
    d = today
    while d < start:
        running += sum(p.amount_cents for p in by_day.get(d, []))
        d += timedelta(days=1)

    days: list[ForecastDay] = []
    planned_in = planned_out = 0
    d = start
    while d <= end:
        day = ForecastDay(date=d)
        if d < today:
            day.actual = actual_total(d)
            flows = past_flows.get(d, [])
            day.inflow = sum(a for a in flows if a > 0)
            day.outflow = sum(a for a in flows if a < 0)
        else:
            items = by_day.get(d, [])
            day.items = items
            day.inflow = sum(p.amount_cents for p in items if p.amount_cents > 0)
            day.outflow = sum(p.amount_cents for p in items if p.amount_cents < 0)
            planned_in += day.inflow
            planned_out += day.outflow
            running += day.inflow + day.outflow
            day.projected = running
            if d == today:
                day.actual = balance_today
            day.below_threshold = running < threshold
        days.append(day)
        d += timedelta(days=1)

    projected = [x for x in days if x.projected is not None]
    min_day = min(projected, key=lambda x: x.projected) if projected else None
    return Forecast(
        days=days,
        today=today,
        threshold=threshold,
        balance_today=balance_today,
        overdue=overdue,
        overdue_total=overdue_total,
        overdue_included=include_overdue,
        end_balance=days[-1].projected if days and days[-1].projected is not None else (days[-1].actual if days else None),
        min_balance=min_day.projected if min_day else None,
        min_date=min_day.date if min_day else None,
        planned_in=planned_in,
        planned_out=planned_out,
        accounts=accounts,
    )
