"""Dopo ogni modifica si aggiornano i dati collegati, rispettando le scelte manuali."""
from datetime import date

from app.db import SessionLocal
from app.models import Account, ChecklistItem, DocCategory, Invoice, PlannedItem, Setting, Transaction
from app.services import invoice_match


def _invoice(s, n="7", party="Alfa Beta Spa", amount=122_000, d=date(2026, 9, 1)):
    inv = Invoice(direction="attiva", doc_type="TD01", number=n, date=d, counterparty=party, total_cents=amount,
                  vat_cents=22_000, file_name="x", file_hash=f"h{n}")
    s.add(inv)
    s.flush()
    s.add(PlannedItem(title=f"Incasso fatt. {n}", amount_cents=amount, due_date=d, invoice_id=inv.id, source="fattura"))
    return inv


def test_adding_and_deleting_a_movement_updates_invoice_status(client):
    with SessionLocal() as s:
        acc = Account(name="BCC", bank="BCC")
        s.add(acc)
        s.flush()
        acc_id, inv_id = acc.id, _invoice(s).id
        s.commit()
    # movimento inserito a mano: dopo il salvataggio la fattura risulta incassata, senza aprire altre pagine
    r = client.post("/movimenti/nuovo", data={"account_id": acc_id, "tx_date": "2026-09-20", "amount": "1220",
                                              "direction": "entrata", "description": "Bonifico da Alfa Beta Spa fatt 7"})
    assert r.status_code in (200, 303)
    with SessionLocal() as s:
        st = next(x for x in invoice_match.invoice_statuses(s) if x.invoice.id == inv_id)
        assert st.state == "pagata"
        tx_id = s.query(Transaction).one().id
    # cancellato il movimento, la fattura torna da incassare
    client.post(f"/movimenti/{tx_id}/elimina")
    with SessionLocal() as s:
        st = next(x for x in invoice_match.invoice_statuses(s) if x.invoice.id == inv_id)
        assert st.state == "aperta"


def test_manual_unlink_is_respected(client):
    with SessionLocal() as s:
        acc = Account(name="BCC", bank="BCC")
        s.add(acc)
        s.flush()
        inv_id = _invoice(s).id
        s.add(Transaction(account_id=acc.id, date=date(2026, 9, 20), amount_cents=122_000,
                          description="Bonifico da Alfa Beta Spa", dedupe_key="t"))
        s.commit()
    client.get("/fatture/situazione")  # abbinamento automatico
    with SessionLocal() as s:
        tx = s.query(Transaction).one()
        assert tx.planned_item_id
        tx_id = tx.id
    client.post("/fatture/situazione/scollega", data={"tx_id": tx_id})
    page = client.get("/fatture/situazione")  # nuovo controllo: non deve riabbinarlo, solo proporlo
    with SessionLocal() as s:
        assert s.get(Transaction, tx_id).planned_item_id is None
        assert next(x for x in invoice_match.invoice_statuses(s) if x.invoice.id == inv_id).state == "aperta"
    assert "Da confermare (1)" in page.text
    # confermato a mano: torna abbinato
    with SessionLocal() as s:
        item_id = s.query(PlannedItem).one().id
    client.post("/fatture/situazione/abbina", data={"tx_ids": str(tx_id), "item_ids": str(item_id)})
    with SessionLocal() as s:
        tx = s.get(Transaction, tx_id)
        assert tx.planned_item_id == item_id and not tx.no_auto_match


def test_reopened_invoice_is_not_auto_paid_again(client):
    with SessionLocal() as s:
        acc = Account(name="BCC", bank="BCC")
        s.add(acc)
        s.flush()
        _invoice(s)
        s.add(Transaction(account_id=acc.id, date=date(2026, 9, 20), amount_cents=122_000, description="incasso", dedupe_key="t"))
        s.commit()
        item_id = s.query(PlannedItem).one().id
    client.post("/fatture/situazione/segna", data={"item_ids": str(item_id), "action": "riapri"})
    client.get("/fatture/situazione")
    with SessionLocal() as s:
        assert s.get(PlannedItem, item_id).status == "previsto"


def test_company_name_change_recategorizes_transfers(client):
    with SessionLocal() as s:
        acc = Account(name="BCC", bank="BCC")
        s.add(acc)
        s.flush()
        s.add(Transaction(account_id=acc.id, date=date(2026, 9, 1), amount_cents=-500_000,
                          description="Bonifico a favore di Rossi Costruzioni Srl", dedupe_key="g"))
        s.commit()
    client.post("/analisi/ricalcola")
    with SessionLocal() as s:
        assert s.query(Transaction).one().category == "Subappalti e conto terzi"
    # se "Rossi Costruzioni" è la nostra società, quel bonifico è un giroconto
    client.post("/impostazioni", data={"company_name": "Rossi Costruzioni", "company_vat": "", "low_balance_threshold": "0"})
    with SessionLocal() as s:
        assert s.query(Transaction).one().category == "Giroconti"


def test_generated_exports_follow_movements(client):
    with SessionLocal() as s:
        acc = Account(name="BCC", bank="BCC", kind="conto")
        s.add(acc)
        s.flush()
        s.add(Transaction(account_id=acc.id, date=date(2026, 9, 3), amount_cents=-1000, description="a", dedupe_key="a"))
        cat = s.query(DocCategory).filter(DocCategory.auto_export == "conto").first()
        item = s.query(ChecklistItem).filter(ChecklistItem.category_id == cat.id, ChecklistItem.period_key == "2026-09").first()
        if item is None:
            item = ChecklistItem(category=cat, period_key="2026-09", period_end=date(2026, 9, 30), due_date=date(2026, 10, 6))
            s.add(item)
        s.commit()
        item_id, acc_id = item.id, acc.id
    client.post(f"/documenti/voce/{item_id}/genera")
    from app.services.refresh import exports_fingerprint
    from app.services.util import get_setting

    with SessionLocal() as s:
        assert [d for d in s.get(ChecklistItem, item_id).documents if d.generated]
        first = get_setting(s, f"export_fp_{item_id}")
    # nuovo movimento di settembre: l'Excel viene rigenerato da solo
    client.post("/movimenti/nuovo", data={"account_id": acc_id, "tx_date": "2026-09-10", "amount": "50",
                                          "direction": "uscita", "description": "b"})
    with SessionLocal() as s:
        item = s.get(ChecklistItem, item_id)
        now = get_setting(s, f"export_fp_{item_id}")
        assert now != first and now == exports_fingerprint(s, item)  # Excel rifatto con il nuovo movimento
        assert [d for d in item.documents if d.generated]
        s.get(ChecklistItem, item_id).status = "inviato"
        s.commit()
    # dopo l'invio non si rigenera, ma lo scadenzario avvisa
    client.post("/movimenti/nuovo", data={"account_id": acc_id, "tx_date": "2026-09-11", "amount": "70",
                                          "direction": "uscita", "description": "c"})
    page = client.get("/documenti?anno=2026")
    assert "Movimenti modificati dopo l" in page.text
