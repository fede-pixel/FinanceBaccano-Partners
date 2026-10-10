"""Smoke test: ogni pagina si apre e i flussi principali funzionano dall'interfaccia."""
import io
import re

from tests.test_fatturapa import make_xml


def test_all_pages_render(client):
    for url in ["/", "/forecast", "/forecast?giorni=90", "/previsti", "/movimenti", "/riconciliazione", "/fatture",
                "/ricorrenze", "/conti", "/conti/nuovo", "/documenti", "/pacchetto", "/impostazioni", "/movimenti/import"]:
        r = client.get(url)
        assert r.status_code == 200, url


def test_end_to_end_flow(client):
    r = client.post("/impostazioni", data={"company_name": "Baccano Partners", "company_vat": "IT09876543210",
                                           "low_balance_threshold": "1.000,00", "include_overdue_in_forecast": "1"})
    assert r.status_code == 200

    r = client.post("/conti/salva", data={"name": "Revolut Business", "bank": "Revolut", "kind": "conto",
                                          "include_in_forecast": "1", "opening_balance": "5.000,00", "opening_date": "2026-08-31"})
    assert "salvato" in r.text

    csv = b"Data;Descrizione;Importo\n01/09/2026;Fornitore Srl fatt 12/A;-610,00\n02/09/2026;Canone;-10,00\n"
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("mov.csv", io.BytesIO(csv), "text/csv")},
                    follow_redirects=False)
    assert r.status_code == 303
    preview_url = r.headers["location"]
    r = client.get(preview_url)
    assert "2 movimenti riconosciuti" in r.text
    token = preview_url.split("/movimenti/import/")[1].split("?")[0]
    r = client.post(f"/movimenti/import/{token}/conferma?conto=1",
                    data={"header_row": "0", "date_col": "Data", "amount_mode": "single", "amount_col": "Importo",
                          "desc_cols": "Descrizione", "date_format": "auto", "decimal_sep": "auto"})
    assert "Importati 2 movimenti" in r.text

    r = client.post("/fatture/import", files=[("files", ("a.xml", io.BytesIO(make_xml()), "text/xml"))], data={"direction": "auto"})
    assert "Importate 1 fatture" in r.text

    r = client.post("/ricorrenze/salva", data={"title": "Stipendi", "amount": "2000", "direction": "uscita",
                                              "frequency": "mensile", "day_of_month": "27", "start_date": "2026-09-01"})
    assert "Stipendi" in r.text

    r = client.get("/forecast?giorni=90")
    assert r.status_code == 200 and "Stipendi" in r.text

    r = client.get("/documenti")
    item_id = re.search(r'/documenti/voce/(\d+)', r.text).group(1)
    r = client.post(f"/documenti/voce/{item_id}/carica", files=[("files", ("scontrino.jpg", io.BytesIO(b"jpg"), "image/jpeg"))])
    assert "Caricati 1 documenti" in r.text
    doc_id = re.search(r'/documenti/file/(\d+)"', r.text).group(1)
    assert client.get(f"/documenti/file/{doc_id}").content == b"jpg"


def test_cross_origin_post_blocked(client):
    r = client.post("/impostazioni", data={"company_name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_undo_import_only_removes_its_own_batch(client):
    from app.db import SessionLocal
    from app.models import Transaction

    client.post("/conti/salva", data={"name": "A", "opening_balance": "0"})
    client.post("/conti/salva", data={"name": "B", "opening_balance": "0"})
    for acc in ("1", "2"):
        r = client.post("/movimenti/import", data={"account_id": acc},
                        files={"file": ("m.csv", b"Data;Descrizione;Importo\n01/09/2026;X;-1\n")}, follow_redirects=False)
        token = r.headers["location"].split("/movimenti/import/")[1].split("?")[0]
        client.post(f"/movimenti/import/{token}/conferma?conto={acc}",
                    data={"header_row": "0", "date_col": "Data", "amount_col": "Importo", "desc_cols": "Descrizione"})
    with SessionLocal() as s:
        batches = [t.import_batch for t in s.query(Transaction).order_by(Transaction.account_id)]
    assert len(set(batches)) == 2  # anche se fatti nello stesso secondo
    client.post(f"/movimenti/import/{batches[0]}/annulla")
    with SessionLocal() as s:
        assert [t.account_id for t in s.query(Transaction)] == [2]


def test_delete_invoice_removes_its_due_dates(client):
    from app.db import SessionLocal
    from app.models import PlannedItem

    client.post("/impostazioni", data={"company_vat": "IT09876543210", "low_balance_threshold": "0"})
    client.post("/fatture/import", files=[("files", ("a.xml", make_xml()))], data={"direction": "auto"})
    with SessionLocal() as s:
        assert s.query(PlannedItem).count() == 2
    client.post("/fatture/1/elimina")
    with SessionLocal() as s:
        assert s.query(PlannedItem).count() == 0
