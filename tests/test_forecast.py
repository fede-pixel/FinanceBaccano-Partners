from datetime import date

from app.models import Account, BalanceSnapshot, PlannedItem, Recurrence, Transaction
from app.services.forecast import BalanceCalculator, build_forecast
from app.services.recurrence import occurrences, sync_recurrence
from app.services.util import set_setting


def _tx(session, acc, d, cents, key):
    session.add(Transaction(account_id=acc.id, date=d, amount_cents=cents, dedupe_key=key))


def test_balance_calculator_forward_and_backward():
    snap = BalanceSnapshot(date=date(2026, 9, 10), balance_cents=10000)
    calc = BalanceCalculator([snap], [(date(2026, 9, 5), 2000), (date(2026, 9, 12), -500)])
    assert calc.at(date(2026, 9, 10)) == 10000
    assert calc.at(date(2026, 9, 12)) == 9500
    assert calc.at(date(2026, 9, 4)) == 8000  # prima dello snapshot: tolgo i movimenti successivi


def test_forecast_daily(session):
    today = date(2026, 10, 10)
    set_setting(session, "low_balance_threshold", "0")
    acc = Account(name="BCC", kind="conto")
    session.add(acc)
    session.flush()
    session.add(BalanceSnapshot(account_id=acc.id, date=date(2026, 9, 30), balance_cents=100000))
    _tx(session, acc, date(2026, 10, 5), -20000, "a")
    session.add(PlannedItem(title="Incasso", amount_cents=50000, due_date=date(2026, 10, 15)))
    session.add(PlannedItem(title="Fornitore", amount_cents=-150000, due_date=date(2026, 10, 20), account_id=acc.id))
    session.add(PlannedItem(title="Scaduto", amount_cents=-1000, due_date=date(2026, 10, 1)))
    session.add(PlannedItem(title="Pagato", amount_cents=-999, due_date=date(2026, 10, 12), status="pagato"))
    session.commit()

    fc = build_forecast(session, date(2026, 10, 1), date(2026, 10, 31), today)
    by_day = {d.date: d for d in fc.days}
    assert fc.balance_today == 80000
    assert by_day[date(2026, 10, 4)].actual == 100000
    assert by_day[date(2026, 10, 5)].outflow == -20000
    assert by_day[date(2026, 10, 10)].projected == 79000  # include lo scaduto
    assert by_day[date(2026, 10, 15)].projected == 129000
    assert by_day[date(2026, 10, 20)].projected == -21000
    assert by_day[date(2026, 10, 20)].below_threshold
    assert fc.min_balance == -21000 and fc.min_date == date(2026, 10, 20)
    assert fc.end_balance == -21000
    assert len(fc.overdue) == 1

    # periodo futuro: parte dal saldo previsto accumulato
    nov = build_forecast(session, date(2026, 11, 1), date(2026, 11, 30), today)
    assert nov.days[0].projected == -21000


def test_recurrence_generation(session):
    today = date(2026, 10, 10)
    rec = Recurrence(title="Stipendi", amount_cents=-300000, frequency="mensile", day_of_month=31,
                     start_date=date(2026, 9, 1), business_day_rule="precedente")
    session.add(rec)
    session.flush()
    nominal = occurrences(rec, date(2027, 2, 28))
    assert nominal[:3] == [date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 30)]
    assert date(2027, 2, 28) in nominal
    sync_recurrence(session, rec, today)
    session.commit()
    items = session.query(PlannedItem).filter_by(recurrence_id=rec.id).order_by(PlannedItem.due_date).all()
    assert items[0].occurrence_key == date(2026, 10, 31)  # settembre è già passato: non creato
    oct_item = next(i for i in items if i.occurrence_key == date(2026, 10, 31))
    assert oct_item.due_date == date(2026, 10, 30)  # 31/10/2026 è sabato

    # una modifica alla regola aggiorna le occorrenze future ma non quelle modificate a mano
    oct_item.source = "modificata"
    oct_item.amount_cents = -1
    rec.amount_cents = -320000
    sync_recurrence(session, rec, today)
    session.commit()
    assert oct_item.amount_cents == -1
    nov_item = next(i for i in items if i.occurrence_key == date(2026, 11, 30))
    assert nov_item.amount_cents == -320000

    # disattivata: spariscono le future ancora previste
    rec.active = False
    sync_recurrence(session, rec, today)
    session.commit()
    left = session.query(PlannedItem).filter_by(recurrence_id=rec.id).all()
    assert not [i for i in left if i.source == "ricorrente" and i.due_date >= today]
    assert oct_item in left  # quella modificata a mano resta


def test_quarterly_recurrence():
    rec = Recurrence(title="IVA", amount_cents=-1, frequency="trimestrale", day_of_month=16, start_date=date(2026, 2, 1))
    assert occurrences(rec, date(2026, 12, 31)) == [date(2026, 2, 16), date(2026, 5, 16), date(2026, 8, 16), date(2026, 11, 16)]
