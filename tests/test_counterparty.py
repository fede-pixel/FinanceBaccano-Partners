"""Scheda cliente/fornitore: tornano i conti?"""
import json
from datetime import date

from app.models import Account, Invoice, PlannedItem, Transaction
from app.services import assistant, counterparty, invoice_match


def _inv(s, n, total, d, party="BARONE COSTRUZIONI SRL", tipo="TD01", direction="attiva"):
    inv = Invoice(direction=direction, doc_type=tipo, number=n, date=d, counterparty=party, total_cents=total,
                  vat_cents=0, file_name="x", file_hash=f"h{n}")
    s.add(inv)
    s.flush()
    sign = 1 if direction == "attiva" else -1
    if tipo == "TD04":
        sign = -sign
    s.add(PlannedItem(title=f"fatt {n}", amount_cents=sign * abs(total), due_date=d, invoice_id=inv.id, source="fattura"))
    return inv


def _setup(s):
    acc = Account(name="BCC", bank="BCC")
    s.add(acc)
    s.flush()
    _inv(s, "1", 3_000_000, date(2026, 5, 1))
    _inv(s, "2", 1_800_000, date(2026, 6, 1))
    _inv(s, "3", 1_200_000, date(2026, 7, 1))
    _inv(s, "NC", 200_000, date(2026, 7, 5), tipo="TD04")
    _inv(s, "9", 500_000, date(2026, 7, 1), party="ALTRO CLIENTE SRL")
    s.add(Transaction(account_id=acc.id, date=date(2026, 5, 20), amount_cents=3_000_000,
                      description="BONIFICO DA BARONE COSTRUZIONI FATT 1", dedupe_key="a"))
    # acconto di importo diverso: non abbinabile da solo, ma col nome
    s.add(Transaction(account_id=acc.id, date=date(2026, 6, 15), amount_cents=900_000,
                      description="BON. DA BARONE COSTR. ACCONTO", dedupe_key="b"))
    s.add(Transaction(account_id=acc.id, date=date(2026, 6, 16), amount_cents=500_000, description="BONIFICO ALTRO CLIENTE",
                      dedupe_key="c"))
    s.commit()
    invoice_match.run(s)
    s.commit()


def test_ledger(session):
    _setup(session)
    led = counterparty.build(session, "barone")
    assert led.names == ["BARONE COSTRUZIONI SRL"] and led.direction == "attiva"
    assert led.invoiced == 3_000_000 + 1_800_000 + 1_200_000 - 200_000
    assert led.settled_by_movements == 3_000_000
    assert [t.dedupe_key for t in led.unlinked] == ["b"] and led.unlinked_total == 900_000
    # aperto: fatt. 2 e 3 (la nota di credito resta aperta e si sottrae)
    assert led.open_amount == 1_800_000 + 1_200_000 - 200_000
    # l'acconto non abbinato è contato due volte (incassato e ancora aperto): la differenza lo segnala
    assert led.difference == -900_000
    tool = json.loads(assistant.run_tool(session, "scheda_controparte", {"nome": "Barone"}))
    assert tool["fatturato_iva_compresa"].startswith("58.000,00") and tool["movimenti_non_abbinati"]


def test_page(client):
    from app.db import SessionLocal

    with SessionLocal() as s:
        _setup(s)
    page = client.get("/controparte?q=Barone")
    assert page.status_code == 200 and "BARONE COSTRUZIONI SRL" in page.text and "Residuo da incassare" in page.text
    assert "BON. DA BARONE COSTR. ACCONTO" in page.text and "ALTRO CLIENTE" not in page.text.split("Movimenti con questo nome")[1]
    assert client.get("/controparte?q=zzzz").status_code == 200


def test_link_advance_from_ledger_page(client):
    from app.db import SessionLocal
    from app.services import invoice_match as im

    with SessionLocal() as s:
        _setup(s)
        inv_id = s.query(Invoice).filter(Invoice.number == "2").one().id
        tx_id = s.query(Transaction).filter(Transaction.dedupe_key == "b").one().id
        item = s.query(PlannedItem).filter(PlannedItem.invoice_id == inv_id).one()
        im.set_dispute([item], "legali")
        s.commit()
    page = client.get("/controparte?q=Barone")
    assert "Collega come acconto" in page.text and "(contenzioso)" in page.text
    client.post("/controparte/acconto", data={"tx_id": tx_id, "invoice_id": inv_id, "q": "Barone"})
    with SessionLocal() as s:
        st = next(x for x in im.invoice_statuses(s) if x.invoice.id == inv_id)
        assert st.state == "contenzioso" and st.disputed_cents == 1_800_000 - 900_000
        assert s.get(Transaction, tx_id).planned_item_id
