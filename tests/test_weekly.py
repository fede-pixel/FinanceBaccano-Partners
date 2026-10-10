"""Caricamento settimanale: riconoscimento e smistamento dei file."""
import io
import re
from datetime import date

from app.models import Account, ChecklistItem, DocCategory, Invoice, Transaction
from app.services import weekly
from app.services.bank_import import ImportProfile
from tests.test_fatturapa import make_xml

REVOLUT = b"""Date completed (UTC),ID,State,Description,Amount,Balance
2026-09-03 10:01:00,id-3,COMPLETED,Google Workspace,-11.10,4888.90
2026-09-02 09:00:10,id-2,COMPLETED,Cliente Rossi,1000.00,4900.00
"""
BCC = "Data contabile;Dare;Avere;Descrizione operazione\n01/09/2026;610,00;;BONIFICO FORNITORE SRL\n".encode()


def test_guessers():
    assert weekly.guess_category("Telepass_settembre.pdf") == "spese_varie"
    assert weekly.guess_category("Invoice-AWS-123.pdf") == "fatture_estere"
    assert weekly.guess_category("assegno emesso 12.jpg") == "assegni_emessi"
    assert weekly.guess_category("EC trimestrale BCC 30.09.2026.pdf") == "ec_trimestrali"
    assert weekly.guess_category("estratto carta revolut.pdf") == "ec_carte"
    assert weekly.guess_period("scontrino 2026-08-14.jpg", "2026-10") == "2026-08"
    assert weekly.guess_period("ricevuta_14-09-2026.pdf", "2026-10") == "2026-09"
    assert weekly.guess_period("foto.jpg", "2026-10") == "2026-10"
    assert weekly.default_period(date(2026, 10, 3)) == "2026-09"
    assert weekly.default_period(date(2026, 10, 20)) == "2026-10"


def test_match_account_by_profile_iban_and_name(session):
    rev = Account(name="Revolut Business", bank="Revolut", csv_profile=ImportProfile(
        header_row=0, date_col="Date completed (UTC)", amount_col="Amount", desc_cols=["Description"], balance_col="Balance").to_json())
    bcc = Account(name="BCC", bank="BCC", iban="IT60X0542811101000000123456")
    vivid = Account(name="Vivid", bank="Vivid")
    session.add_all([rev, bcc, vivid])
    session.commit()
    from app.services.bank_import import read_table

    accs = [rev, bcc, vivid]
    assert weekly.match_account(read_table(REVOLUT, "x.csv"), "x.csv", accs) == (rev, False)
    rows = [["Conto IT60 X054 2811 1010 0000 0123 456"], *read_table(BCC, "b.csv")]
    assert weekly.match_account(rows, "export.csv", accs) == (bcc, True)
    assert weekly.match_account(read_table(BCC, "b.csv"), "movimenti_vivid_settembre.csv", accs) == (vivid, True)
    assert weekly.match_account(read_table(BCC, "b.csv"), "export.csv", accs) == (None, True)


def test_end_to_end_weekly_upload(client, session):
    client.post("/impostazioni", data={"company_name": "Baccano", "company_vat": "IT09876543210", "low_balance_threshold": "0"})
    client.post("/conti/salva", data={"name": "Revolut Business", "bank": "Revolut", "include_in_forecast": "1",
                                      "opening_balance": "0", "opening_date": "2026-08-31"})
    client.post("/conti/salva", data={"name": "BCC", "bank": "BCC", "include_in_forecast": "1",
                                      "opening_balance": "0", "opening_date": "2026-08-31"})
    # Revolut ha già una mappatura salvata (da un import precedente)
    from app.db import SessionLocal
    with SessionLocal() as s:
        rev = s.get(Account, 1)
        rev.csv_profile = ImportProfile(header_row=0, date_col="Date completed (UTC)", amount_col="Amount", desc_cols=["Description"],
                                        id_col="ID", balance_col="Balance", status_col="State", status_ok="COMPLETED").to_json()
        s.commit()

    files = [
        ("files", ("revolut_settembre.csv", io.BytesIO(REVOLUT), "text/csv")),
        ("files", ("bcc_settembre.csv", io.BytesIO(BCC), "text/csv")),
        ("files", ("fattura.xml", io.BytesIO(make_xml()), "text/xml")),
        ("files", ("Telepass 2026-09.pdf", io.BytesIO(b"%PDF-telepass"), "application/pdf")),
        ("files", ("invoice_google.pdf", io.BytesIO(b"%PDF-google"), "application/pdf")),
    ]
    r = client.post("/carica", files=files, follow_redirects=False)
    assert r.status_code == 303
    review = client.get(r.headers["location"])
    assert review.status_code == 200
    assert "riconosciuto" in review.text and "prima volta" in review.text
    batch_id = r.headers["location"].rsplit("/", 1)[1]
    batch = weekly.load_batch(batch_id)
    kinds = {i.name: (i.kind, i.account_id, i.category) for i in batch.items}
    assert kinds["revolut_settembre.csv"] == ("banca", 1, "")
    assert kinds["bcc_settembre.csv"][:2] == ("banca", 2)
    assert kinds["fattura.xml"][0] == "fattura"
    assert kinds["Telepass 2026-09.pdf"] == ("documento", 0, "spese_varie")

    # cambio a mano il mese della ricevuta Google
    form = {f"period_{i}": "2026-09" for i, it in enumerate(batch.items) if it.name == "invoice_google.pdf"}
    done = client.post(f"/carica/{batch_id}", data=form)
    assert done.status_code == 200
    assert re.search(r"Movimenti nuovi</div><div class=\"value\">2<", done.text)
    assert re.search(r"Fatture importate</div><div class=\"value\">1<", done.text)
    assert re.search(r"Documenti archiviati</div><div class=\"value\">2<", done.text)
    assert "Indica le colonne" in done.text  # BCC: prima volta

    session.expire_all()
    assert session.query(Transaction).count() == 2
    assert session.query(Invoice).count() == 1
    spese = session.query(ChecklistItem).join(DocCategory).filter(DocCategory.code == "spese_varie", ChecklistItem.period_key == "2026-09").one()
    assert [d.file_name for d in spese.documents] == ["Telepass 2026-09.pdf"]
    estere = session.query(ChecklistItem).join(DocCategory).filter(DocCategory.code == "fatture_estere", ChecklistItem.period_key == "2026-09").one()
    assert len(estere.documents) == 1

    # completo il BCC dalla schermata di mappatura: la prossima volta sarà automatico
    pending = weekly.load_batch(batch_id).items
    assert [p.name for p in pending] == ["bcc_settembre.csv"]
    token = pending[0].token
    r = client.post(f"/movimenti/import/{token}/conferma?conto=2",
                    data={"header_row": "0", "date_col": "Data contabile", "amount_mode": "split", "in_col": "Avere",
                          "out_col": "Dare", "desc_cols": "Descrizione operazione", "date_format": "auto", "decimal_sep": "auto"})
    assert "Importati 1 movimenti" in r.text

    r = client.post("/carica", files=[("files", ("bcc_ottobre.csv", io.BytesIO(BCC), "text/csv"))], follow_redirects=False)
    batch = weekly.load_batch(r.headers["location"].rsplit("/", 1)[1])
    assert batch.items[0].account_id == 2 and not batch.items[0].needs_mapping
    done = client.post(r.headers["location"], data={})
    assert "1 già presenti" in done.text

    assert "Caricamento settimanale" in client.get("/").text
