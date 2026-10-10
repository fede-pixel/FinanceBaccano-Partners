"""KPI economici/finanziari e capacità di investimento."""
import json
from datetime import date, timedelta

from app.models import Account, BalanceSnapshot, Invoice, PlannedItem, Transaction
from app.services import assistant, investment, kpi
from app.services.util import set_setting

TODAY = date(2026, 10, 10)


def _setup(session):
    acc = Account(name="BCC", bank="BCC", include_in_forecast=True)
    session.add(acc)
    session.flush()
    session.add(BalanceSnapshot(account_id=acc.id, date=date(2026, 3, 31), balance_cents=5_000_000))
    n = 0
    for m in range(4, 10):  # aprile-settembre: 30k fatturati, 10k costi, 8k stipendi; incassi 30k, uscite 20k
        d = date(2026, m, 10)
        for direction, tot, vat, tipo in (("attiva", 3_660_000, 660_000, "TD01"), ("passiva", 1_220_000, 220_000, "TD01")):
            n += 1
            session.add(Invoice(direction=direction, doc_type=tipo, number=str(n), date=d, counterparty="Cliente A" if direction == "attiva" else "Forn B",
                                total_cents=tot, vat_cents=vat, file_name="x", file_hash=f"h{n}"))
        session.add(Transaction(account_id=acc.id, date=d + timedelta(days=5), amount_cents=3_000_000, description="incasso", dedupe_key=f"i{m}"))
        session.add(Transaction(account_id=acc.id, date=d + timedelta(days=6), amount_cents=-1_200_000, description="fornitore", dedupe_key=f"o{m}"))
        session.add(Transaction(account_id=acc.id, date=d + timedelta(days=7), amount_cents=-800_000, description="stipendi",
                                category="Stipendi e collaboratori", category_source="auto", dedupe_key=f"s{m}"))
        session.add(Transaction(account_id=acc.id, date=d + timedelta(days=8), amount_cents=-500_000, description="giroconto",
                                category="Giroconti", category_source="auto", dedupe_key=f"g{m}"))
    # nota di credito di settembre: riduce i ricavi
    session.add(Invoice(direction="attiva", doc_type="TD04", number="NC1", date=date(2026, 9, 20), counterparty="Cliente A",
                        total_cents=-122_000, vat_cents=-22_000, file_name="x", file_hash="nc"))
    session.commit()
    return acc


def test_kpi_monthly(session):
    _setup(session)
    rep = kpi.build(session, 12, TODAY)
    sep = next(m for m in rep.months if m.key == "2026-09")
    assert sep.revenue == 3_000_000 - 100_000  # imponibile, nota di credito in negativo
    assert sep.external_costs == 1_000_000 and sep.personnel == 800_000
    assert sep.ebitda == 1_100_000 and round(sep.margin, 3) == round(1_100_000 / 2_900_000, 3)
    assert sep.cash_in == 3_000_000 and sep.cash_out == 2_000_000  # giroconti esclusi
    assert rep.ytd_revenue == 6 * 3_000_000 - 100_000
    assert rep.balance_today == 5_000_000 + 6 * (3_000_000 - 2_000_000 - 500_000)
    assert rep.avg_monthly_out == 2_000_000 and round(rep.runway_months, 1) == 4.0
    assert rep.top_clients[0][0] == "Cliente A" and rep.invoices_without_vat == 0


def test_investment_capacity(session):
    acc = _setup(session)
    base = investment.build(session, 6, "base", TODAY)
    assert base.hist_in == 3_000_000 and base.avg_monthly_out == 2_000_000 and base.recurring_surplus == 1_000_000
    assert base.reserve == 4_000_000  # 2 mesi di uscite medie
    # cassa oggi 80.000 €: subito si possono spendere 40.000 € (fino alla riserva); in modo graduale di più, perché la cassa cresce
    assert base.investable_now == base.balance_today - base.reserve == 4_000_000
    assert base.investable > base.investable_now
    pru = investment.build(session, 6, "prudente", TODAY)
    assert pru.investable < base.investable
    assert abs(sum(a.amount for a in base.allocations) - base.investable) <= 4
    hr = next(a for a in base.allocations if a.key == "hr")
    assert base.hire_monthly_budget == min(hr.amount // 12, 1_000_000 // 2)
    # una grossa uscita prevista tra 2 mesi riduce la capacità
    session.add(PlannedItem(title="Rata leasing", amount_cents=-6_000_000, due_date=TODAY + timedelta(days=60), account_id=acc.id))
    session.commit()
    after = investment.build(session, 6, "base", TODAY)
    assert after.investable < base.investable and after.min_date >= TODAY


def test_pages_and_assistant_tools(client):
    from app.db import SessionLocal

    with SessionLocal() as s:
        _setup(s)
    assert client.get("/kpi").status_code == 200
    page = client.get("/investimenti?mesi=12&scenario=prudente")
    assert page.status_code == 200 and "Ripartizione per area" in page.text and "Scenario prudente" in page.text
    r = client.post("/investimenti/impostazioni", data={"inv_marketing": "50", "inv_hr": "50", "inv_mezzi": "0", "inv_altro": "0",
                                                         "inv_reserve_months": "1", "inv_hire_cost": "2500", "mesi": "6", "scenario": "base"},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        cfg = investment.settings(s)
        assert cfg["inv_marketing"] == 50 and cfg["inv_reserve_months"] == 1
        out = json.loads(assistant.run_tool(s, "capacita_investimento", {"mesi": 6, "scenario": "base"}))
        assert out["ripartizione"][2]["budget"].startswith("0,00")
        k = json.loads(assistant.run_tool(s, "kpi_economici", {"mesi": 12}))
        assert "ebitda" in k["anno_in_corso"]


def test_cash_split_and_self_invoices(session):
    acc = _setup(session)
    inv = Invoice(direction="passiva", doc_type="TD17", number="AF1", date=date(2026, 9, 5), counterparty="ANTHROPIC",
                  total_cents=12_200, vat_cents=2_200, file_name="x", file_hash="af")
    session.add(inv)
    tx = session.query(Transaction).filter(Transaction.dedupe_key == "i9").one()
    item = PlannedItem(title="incasso", amount_cents=3_000_000, due_date=tx.date, status="pagato", source="fattura",
                       invoice_id=session.query(Invoice).filter(Invoice.direction == "attiva").first().id)
    session.add(item)
    session.flush()
    tx.planned_item_id = item.id
    session.commit()
    sep = next(m for m in kpi.build(session, 12, TODAY).months if m.key == "2026-09")
    assert sep.self_invoice_costs == 10_000 and sep.external_costs == 1_010_000
    assert sep.cash_in_invoices == 3_000_000 and sep.cash_out_invoices == 0


def test_year_totals_do_not_depend_on_window(session):
    _setup(session)
    six, twelve = kpi.build(session, 6, TODAY), kpi.build(session, 12, TODAY)
    assert len(six.months) == 6 and len(twelve.months) == 12
    assert six.ytd_revenue == twelve.ytd_revenue == 6 * 3_000_000 - 100_000
    r = six.recon
    assert r["aruba_like"] == r["revenue"] - (r["costs"] - r["self_costs"])
    assert six.ytd_ebitda == r["revenue"] - r["costs"] - r["personnel"]


def test_recon_gross_matches_aruba_list(session):
    """Colonna IVA compresa: emesse − ricevute come i totali della lista di Aruba (autofatture in entrambe)."""
    _setup(session)
    session.add(Invoice(direction="passiva", doc_type="TD17", number="AF", date=date(2026, 6, 30), counterparty="ANTHROPIC",
                        total_cents=9_080, vat_cents=1_637, file_name="x", file_hash="af"))
    session.commit()
    r = kpi.build(session, 12, TODAY).recon
    emesse_lista = r["revenue_gross"] + r["self_gross"]
    ricevute_lista = r["costs_gross"] + r["self_gross"]
    assert emesse_lista - ricevute_lista == r["diff_gross"] == 6 * 3_660_000 - 122_000 - 6 * 1_220_000
    assert r["net_vat"] == r["diff_gross"] - r["aruba_like"] == 6 * 660_000 - 22_000 - 6 * 220_000


def test_vat_position_with_carry_forward(session):
    """Vendi al 10%, compri al 22%: nel T1 si va a credito, il credito si scala dal T2."""
    def inv(n, direction, d, taxable, rate, tipo="TD01"):
        vat = taxable * rate // 100
        session.add(Invoice(direction=direction, doc_type=tipo, number=n, date=d, counterparty="X", total_cents=taxable + vat,
                            vat_cents=vat, file_name="x", file_hash=f"v{n}"))

    inv("E1", "attiva", date(2026, 2, 10), 10_000_000, 10)    # debito 10.000
    inv("R1", "passiva", date(2026, 3, 5), 6_000_000, 22)     # credito 13.200
    inv("E2", "attiva", date(2026, 5, 10), 10_000_000, 10)    # debito 10.000
    inv("R2", "passiva", date(2026, 5, 12), 1_000_000, 22)    # credito 2.200
    inv("NC", "attiva", date(2026, 6, 1), 1_000_000, 10, "TD04")  # nota di credito: -1.000 di debito
    inv("AF", "passiva", date(2026, 6, 30), 500_000, 22, "TD17")  # reverse charge: si annulla
    session.commit()
    rep = kpi.vat_report(session, 2026, "trimestrale", TODAY)
    t1, t2, t3 = rep.periods[:3]
    assert (t1.debit, t1.credit, t1.to_pay, t1.carried_out) == (1_000_000, 1_320_000, 0, 320_000)
    assert (t2.debit, t2.credit, t2.carried_in, t2.to_pay) == (900_000, 220_000, 320_000, 360_000)
    assert t2.due == date(2026, 8, 20) and t3.to_pay == 0
    assert len(rep.periods) == 4  # T4 iniziato (ottobre)
    assert rep.self_invoices_vat == 110_000 and rep.total_to_pay == 360_000
    monthly = kpi.vat_report(session, 2026, "mensile", TODAY)
    feb = monthly.periods[1]
    assert feb.to_pay == 1_000_000 and feb.due == date(2026, 3, 16)
    assert len(monthly.periods) == 10


def test_profit_plan_from_expected_inflows(session):
    from app.models import Contract
    from app.services import contracts

    c = Contract(client="Luongo", total_taxable_cents=14_000_000, vat_included=True, as_of=date(2026, 10, 10),
                 end_date=date(2027, 2, 28))
    session.add(c)
    session.flush()
    contracts.sync_contract(session, c, today=date(2026, 10, 10))
    session.add(PlannedItem(title="Scaduta", amount_cents=1_000_000, due_date=date(2026, 9, 1)))
    session.commit()
    pp = investment.profit_plan(session, "base", date(2026, 10, 10))
    assert pp.overdue == 800_000  # scaduti all'80% nello scenario base
    assert pp.inflow == 14_000_000 + 800_000
    assert pp.costs == round(pp.inflow * 0.7) and pp.profit == pp.inflow - pp.costs
    assert pp.investable == pp.profit and abs(sum(a.amount for a in pp.allocations) - pp.investable) <= 4
    assert pp.months[-1].cumulative == pp.profit and pp.last_month == "2027-03"
    set_setting(session, "inv_tax_reserve", "25")
    session.commit()
    pp2 = investment.profit_plan(session, "base", date(2026, 10, 10))
    assert pp2.taxes == round(pp2.profit * 0.25) and pp2.investable == pp2.profit - pp2.taxes
