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
