"""Report Excel delle fatture esportato da Aruba (ReportFattureRicevute.xls / .xlsx)."""
import io
from datetime import date

import openpyxl
from sqlalchemy import select

from app.models import Invoice, PlannedItem
from app.services import weekly
from app.services.fatturapa import import_invoices, is_aruba_report
from tests.test_fatturapa import make_xml

HEADER = ["Numero", "Nome file", "ID SdI", "Data ricezione", "Data documento", "Tipo documento", "Fornitore", "P.IVA",
          "Codice Fiscale", "Metodo di pagamento", "Totale imponibile", "Totale IVA", "Totale documento", "Netto a pagare",
          "Pagamenti", "Data pagamento", "Stato"]
ROWS = [
    ["059", "a.xml", "1001", "2026-10-10", date(2026, 10, 9), "Fattura - TD01", "GIORGIO CICERO", "18093441006", "", "",
     650, 0, 650, None, "Non pagata", None, "Non letta"],
    ["106/ZN", "b.xml.p7m", "1002", "2026-10-10", date(2026, 9, 28), "Fattura - TD01", "ZENZERO S.R.L.", "04424390237", "",
     "MP08 - Carta di pagamento", 172.07, 37.86, 209.93, 209.93, "Non pagata", None, "Letta"],
    ["AF01 119/26", "c.xml.p7m", "1003", "2026-10-02", date(2026, 6, 30), "Integrazione/autofattura per acquisto servizi dall'estero - TD17",
     "ANTHROPIC IRELAND LIMITED", "4276970QH", "", "MP05 - Bonifico", 74.43, 16.37, 90.8, 90.8, "Non pagata", None, "Letta"],
    ["027", "d.xml", "1004", "2026-04-01", date(2026, 4, 1), "Fattura - TD01", "ROSSI MARIO", "11111111111", "", "MP05 - Bonifico",
     3460, 0, 3460, 3000, "Pagata", date(2026, 4, 20), "Letta"],
    ["027", "e.xml", "1005", "2026-04-01", date(2026, 4, 1), "Nota di credito - TD04", "ROSSI MARIO", "11111111111", "", "",
     -500, 0, -500, None, "Non pagata", None, "Letta"],
    ["410/A", "f.xml.p7m", "1006", "2026-03-01", date(2026, 3, 1), "Fattura - TD01", "MAVERIK", "01234567890", "", "",
     0, 0, None, None, "Non pagata", None, "Letta"],
]


def report_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_report_import(session):
    content = report_xlsx()
    assert is_aruba_report("20261010_ReportFattureRicevute.xlsx", content)
    res = import_invoices(session, [("20261010_ReportFattureRicevute.xlsx", content)], company_vat="", default_payment_days=30,
                          paid_before=date(2026, 5, 15))
    assert len(res.imported) == 6 and not res.duplicates
    assert len(res.errors) == 1 and "410/A" in res.errors[0]  # senza totale: archiviata senza scadenza
    invs = {(i.number, i.doc_type): i for i in session.scalars(select(Invoice))}
    assert all(i.direction == "passiva" for i in invs.values())

    def items(number, tipo="TD01"):
        return invs[(number, tipo)].planned_items

    open_item = items("059")[0]  # bonifico non pagato: scadenza stimata a 30 giorni
    assert (open_item.amount_cents, open_item.due_date, open_item.status, open_item.estimated_date) == (-65000, date(2026, 11, 8), "previsto", True)
    card = items("106/ZN")[0]  # carta: già pagata alla data della fattura
    assert (card.amount_cents, card.status, card.due_date) == (-20993, "pagato", date(2026, 9, 28))
    assert items("AF01 119/26", "TD17") == []  # autofattura reverse charge: nessuna uscita prevista
    paid = items("027")[0]  # pagata: si usa il netto a pagare e la data di pagamento
    assert (paid.amount_cents, paid.status, paid.due_date) == (-300000, "pagato", date(2026, 4, 20))
    note = items("027", "TD04")[0]  # nota di credito con stesso numero: è un documento diverso, segno invertito
    assert note.amount_cents == 50000 and note.status == "pagato"  # scadenza prima di paid_before
    assert items("410/A") == []

    # reimportare il report non crea doppioni
    res = import_invoices(session, [("ReportFattureRicevute.xlsx", content)], company_vat="")
    assert not res.imported and len(res.duplicates) == 6


def test_xml_and_report_do_not_duplicate(session):
    res = import_invoices(session, [("f.xml", make_xml(numero="12/A"))], company_vat="09876543210")
    assert len(res.imported) == 1
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    ws.append(["12/A", "f.xml", "2001", "", date(2026, 9, 30), "Fattura - TD01", "Fornitore Srl", "01234567890", "", "MP05 - Bonifico",
               1000, 220, 1220, 1220, "Non pagata", None, "Letta"])
    buf = io.BytesIO()
    wb.save(buf)
    res = import_invoices(session, [("ReportFattureRicevute.xlsx", buf.getvalue())], company_vat="09876543210")
    assert not res.imported and res.duplicates == ["12/A"]
    assert session.query(Invoice).count() == 1


def test_report_from_web_and_weekly_upload(client, session):
    r = client.post("/fatture/import", files=[("files", ("ReportFattureRicevute.xlsx", io.BytesIO(report_xlsx()), "application/octet-stream"))],
                    data={"direction": "auto"}, follow_redirects=False)
    assert r.status_code == 303 and "Importate+6" in r.headers["location"].replace("%20", "+")
    page = client.get("/fatture")
    assert "ZENZERO" in page.text and "report Excel" in page.text
    assert weekly.is_invoice("x.xml", make_xml())
    batch = weekly.create_batch(session, [("20261010_ReportFattureRicevute.xlsx", report_xlsx())])
    assert batch.items[0].kind == "fattura"
    session.query(PlannedItem).delete()
    session.query(Invoice).delete()
    session.commit()
    res = weekly.process_batch(session, batch)
    assert res.invoices == 6


def test_reimport_completes_missing_vat(session):
    from app.services import kpi

    content = make_xml(supplier_vat="09876543210", customer_vat="01234567890", numero="7/A").replace(  # fattura emessa
        b"</DatiGenerali>", b"</DatiGenerali><DatiBeniServizi><DatiRiepilogo><ImponibileImporto>1000.00</ImponibileImporto>"
                            b"<Imposta>220.00</Imposta></DatiRiepilogo></DatiBeniServizi>")
    import_invoices(session, [("e.xml", content)], company_vat="09876543210")
    inv = session.query(Invoice).one()
    inv.vat_cents = None  # come le fatture importate prima dell'aggiornamento
    session.commit()
    rep = kpi.build(session, 12, date(2026, 10, 10))
    assert rep.ytd_revenue_without_vat == 122000 and rep.invoices_without_vat == 1
    res = import_invoices(session, [("e.xml", content)], company_vat="09876543210")
    assert not res.imported and res.vat_completed == 1
    assert inv.vat_cents == 22000 and inv.taxable_cents == 100000
    assert kpi.build(session, 12, date(2026, 10, 10)).ytd_revenue == 100000
