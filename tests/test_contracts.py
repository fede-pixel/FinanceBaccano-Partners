"""Contratti e piani di incasso."""
from datetime import date

from sqlalchemy import select

from app.models import Contract, Invoice, PlannedItem
from app.services import contracts


def _dmg(session, as_of=date(2026, 10, 5)):
    c = Contract(client="DMG Analytics & Consulting S.r.l.s.", total_taxable_cents=9_649_000, paid_taxable_cents=4_028_887,
                 vat_rate=10, as_of=as_of, end_date=date(2027, 2, 28))
    session.add(c)
    session.flush()
    return c


def test_residual_split_evenly_until_end(session):
    c = _dmg(session)
    contracts.sync_contract(session, c, today=date(2026, 10, 10))
    session.commit()
    items = list(session.scalars(select(PlannedItem).where(PlannedItem.contract_id == c.id).order_by(PlannedItem.due_date)))
    assert c.residual_gross_cents == 6_182_124
    assert [i.due_date for i in items] == [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 28)]
    assert sum(i.amount_cents for i in items) == 6_182_124 and items[0].amount_cents == 1_236_425
    assert all(i.amount_cents > 0 and i.estimated_date for i in items)


def test_new_invoices_reduce_the_plan_and_months_shift(session):
    c = _dmg(session)
    session.add(Invoice(direction="attiva", doc_type="TD01", number="FPR 70/26", date=date(2026, 11, 3),
                        counterparty="DMG ANALYTICS & CONSULTING SRLS", total_cents=1_100_000, vat_cents=100_000,
                        file_name="x", file_hash="x"))
    session.add(Invoice(direction="attiva", doc_type="TD01", number="FPR 54/26", date=date(2026, 9, 15),
                        counterparty="DMG ANALYTICS & CONSULTING SRLS", total_cents=1_923_295, vat_cents=174_845,
                        file_name="y", file_hash="y"))
    session.flush()
    contracts.sync_contract(session, c, today=date(2026, 12, 2))
    session.commit()
    p = contracts.plan(session, c)
    assert p.invoiced_after == 1_100_000  # solo quella dopo la data di aggiornamento
    assert p.to_plan == 6_182_124 - 1_100_000
    items = [i for i in p.items if i.status == "previsto"]
    assert len(items) == 3 and sum(i.amount_cents for i in items) == p.to_plan  # dicembre, gennaio, febbraio


def test_page_and_crud(client):
    page = client.post("/contratti/salva", data={"client": "Rossi Srl", "total": "10.000,00", "paid": "4.000",
                                                  "vat_rate": "10", "as_of": "2026-10-05", "end_date": "2027-02-28"})
    assert page.status_code == 200 and "Rossi Srl" in page.text and "6.600,00" in page.text
    from app.db import SessionLocal
    with SessionLocal() as s:
        cid = s.scalars(select(Contract)).one().id
        item = s.scalars(select(PlannedItem).where(PlannedItem.contract_id == cid)).first()
    assert "dal contratto" in client.post("/previsti/salva", data={"item_id": item.id, "title": "x", "amount": "1",
                                                                   "due_date": "2026-11-30"}).text
    client.post(f"/contratti/{cid}/elimina")
    with SessionLocal() as s:
        assert not s.scalars(select(PlannedItem).where(PlannedItem.contract_id == cid)).all()


def test_seed_only_with_dmg_invoices(session):
    assert not contracts.seed_dmg(session)
    session.add(Invoice(direction="attiva", doc_type="TD01", number="FPR 48/26", date=date(2026, 8, 18),
                        counterparty="DMG ANALYTICS & CONSULTING S.R.L.S.", total_cents=719_950, vat_cents=65_450,
                        file_name="z", file_hash="z"))
    session.flush()
    assert contracts.seed_dmg(session)
    assert not contracts.seed_dmg(session)  # una sola volta
    c = session.scalars(select(Contract)).one()
    assert c.residual_gross_cents == 6_182_124


def test_invoice_outside_contract(client):
    from app.db import SessionLocal
    with SessionLocal() as s:
        c = _dmg(s)
        s.add(Invoice(direction="attiva", doc_type="TD01", number="FPR 72/26", date=date(2026, 10, 7),
                      counterparty="DMG ANALYTICS & CONSULTING SOCIETA' A RESPONSABILITA' LIMITATA SEMPLIFICATA",
                      total_cents=1_148_400, vat_cents=104_400, file_name="x", file_hash="x"))
        s.commit()
        cid, iid = c.id, s.scalars(select(Invoice)).one().id
        assert contracts.plan(s, c).to_plan == 6_182_124 - 1_148_400
    page = client.post(f"/contratti/{cid}/fattura", data={"invoice_id": iid}).text
    assert "Fuori contratto" in page and "rimetti nel contratto" in page
    with SessionLocal() as s:
        assert contracts.plan(s, s.get(Contract, cid)).to_plan == 6_182_124
    client.post(f"/contratti/{cid}/fattura", data={"invoice_id": iid, "inside": "1"})
    with SessionLocal() as s:
        assert contracts.plan(s, s.get(Contract, cid)).to_plan == 6_182_124 - 1_148_400


def test_vat_included_contracts_and_october_seed(session):
    from app.models import Recurrence

    session.add(Invoice(direction="attiva", doc_type="TD01", number="FPR 48/26", date=date(2026, 8, 18),
                        counterparty="DMG ANALYTICS & CONSULTING S.R.L.S.", total_cents=719_950, vat_cents=65_450,
                        file_name="z", file_hash="z"))
    session.flush()
    assert contracts.seed_october_2026(session) and not contracts.seed_october_2026(session)
    by = {c.client: c for c in session.scalars(select(Contract))}
    assert by["Luongo"].residual_gross_cents == 14_000_000  # niente IVA in più: è già la cifra da incassare
    contracts.sync_contract(session, by["Luongo"], today=date(2026, 10, 10))
    items = [i for i in contracts.plan(session, by["Luongo"]).items]
    assert len(items) == 5 and {i.amount_cents for i in items} == {2_800_000}
    rec = session.scalars(select(Recurrence)).one()
    occ = session.scalars(select(PlannedItem).where(PlannedItem.recurrence_id == rec.id)).all()
    assert len(occ) == 8 and all(o.amount_cents == 100_000 for o in occ) and min(o.due_date for o in occ) == date(2026, 10, 15)


def test_monthly_inflows_by_source(session, ):
    from app.services import inflows

    c = _dmg(session)
    contracts.sync_contract(session, c, today=date(2026, 10, 10))
    session.add(PlannedItem(title="Fattura", amount_cents=500_000, due_date=date(2026, 11, 15), source="fattura"))
    session.add(PlannedItem(title="Scaduta", amount_cents=200_000, due_date=date(2026, 9, 15)))
    session.add(PlannedItem(title="Uscita", amount_cents=-900_000, due_date=date(2026, 11, 20)))
    session.commit()
    r = inflows.monthly(session, date(2026, 10, 10))
    assert [m.key for m in r.months][:5] == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02"] and len(r.months) == 6
    assert r.months[1].contracts == 1_236_425 and r.months[1].other == 500_000
    assert r.overdue == 200_000 and r.total == 6_182_124 + 500_000
    assert r.months[-1].cumulative == r.total


def test_inflows_on_pages(client):
    for url in ("/", "/forecast", "/contratti"):
        assert "Incassi previsti mese per mese" in client.get(url).text


def test_revenue_outlook(session):
    from app.models import Recurrence
    from app.services import inflows

    session.add(Invoice(direction="attiva", doc_type="TD01", number="E1", date=date(2026, 3, 1), counterparty="Cliente",
                        total_cents=1_100_000, vat_cents=100_000, file_name="e", file_hash="e"))
    c = Contract(client="Salerno", total_taxable_cents=11_000_000, vat_included=True, vat_rate=10,
                 as_of=date(2026, 10, 10), end_date=date(2027, 3, 31))
    session.add(c)
    session.flush()
    contracts.sync_contract(session, c, today=date(2026, 10, 10))  # 6 rate da 1.833.333 IVA compresa
    session.add(PlannedItem(title="Fattura aperta", amount_cents=500_000, due_date=date(2026, 11, 1), invoice_id=None,
                            source="manuale"))
    session.commit()
    o = inflows.revenue_outlook(session, date(2026, 10, 10))
    y = o.year_end
    assert y.issued == 1_000_000
    assert y.contracts == round(1_833_334 * 100 / 110) * 3 or abs(y.contracts - 5_000_000) <= 3  # ott-dic, IVA scorporata
    assert y.other == round(500_000 * 100 / 110)
    assert o.last_due == date(2027, 3, 31) and o.last is not None
    assert abs(o.last.contracts - 10_000_000) <= 3 and abs(o.next_year - 5_000_000) <= 3


def test_forecast_page_shows_revenue_outlook(client):
    page = client.get("/forecast").text
    assert "Previsione di fatturato" in page and "Ultimo incasso previsto" in page


def test_cash_contract_includes_open_invoices(session):
    from app.models import Account, Transaction

    c = Contract(client="Luongo", total_taxable_cents=14_000_000, vat_included=True, as_of=date(2026, 10, 10),
                 end_date=date(2027, 2, 28))
    session.add(c)
    for n, cents in (("FPR 51/26", 525_889), ("FPR 63/26", 525_889), ("FPR 66/26", 946_601)):
        inv = Invoice(direction="attiva", doc_type="TD01", number=n, date=date(2026, 9, 2), counterparty="Luciano Luongo",
                      total_cents=cents, vat_cents=cents // 11, file_name=n, file_hash=n)
        inv.planned_items.append(PlannedItem(title=n, amount_cents=cents, due_date=date(2026, 10, 2), source="fattura"))
        session.add(inv)
    session.flush()
    p = contracts.plan(session, c)
    assert p.to_plan == 14_000_000 - 1_998_379 and len(p.open_invoices) == 3  # niente doppio conteggio
    # la 51/26 viene incassata dopo l'aggiornamento: il totale resta lo stesso
    acc = Account(name="Banca")
    session.add(acc)
    session.flush()
    item = session.scalars(select(PlannedItem).where(PlannedItem.title == "FPR 51/26")).one()
    tx = Transaction(account_id=acc.id, date=date(2026, 10, 20), amount_cents=525_889, description="Bonifico Luongo", dedupe_key="t")
    tx.planned_item = item
    item.status, item.paid_date, item.settlement = "pagato", date(2026, 10, 20), "movimento"
    session.add(tx)
    session.flush()
    p = contracts.plan(session, c)
    assert p.collected_after == 525_889 and p.to_plan == 14_000_000 - 1_998_379
