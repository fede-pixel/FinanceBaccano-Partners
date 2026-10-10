"""Pagina Verifica dati."""
from datetime import date

from app.models import Invoice
from app.services import data_check
from app.services.fatturapa import import_invoices
from tests.test_aruba_report import _report


def test_totals_like_aruba_and_checks(session):
    emesse = _report("Cliente", [
        ["1", "a.xml", "1", "", date(2026, 3, 1), "Fattura - TD01", "Cliente Uno", "111", "", "", 1000, 100, 1100, 1100, "", None, ""],
        ["AF1", "b.xml", "2", "", date(2026, 3, 2), "Integrazione/autofattura per acquisto servizi dall'estero - TD17",
         "Baccano & Partners", "999", "", "", 100, 22, 122, 122, "", None, ""],
    ])
    ricevute = _report("Fornitore", [
        ["F1", "c.xml", "3", "", date(2026, 3, 5), "Fattura - TD01", "Fornitore Srl", "222", "", "", 500, 110, 610, 610, "Non pagata", None, ""],
        ["AF1", "b.xml", "2", "", date(2026, 3, 2), "Integrazione/autofattura per acquisto servizi dall'estero - TD17",
         "GOOGLE CLOUD", "IE1", "", "", 100, 22, 122, 122, "Non pagata", None, ""],
        ["Z1", "d.xml", "4", "", date(2026, 3, 6), "Fattura - TD01", "Zero Srl", "333", "", "", 0, 0, 0, 0, "Non pagata", None, ""],
    ])
    import_invoices(session, [("ReportFattureEmesse.xlsx", emesse)], company_vat="")
    import_invoices(session, [("ReportFattureRicevute.xlsx", ricevute)], company_vat="")
    session.commit()
    t = data_check.totals(session, 2026)
    # come la lista di Aruba: l'autofattura compare in entrambe
    assert (t.issued_count, t.issued_gross) == (2, 110_000 + 12_200)
    assert (t.received_count, t.received_gross) == (3, 61_000 + 12_200)
    # come il grafico di Aruba: imponibile, autofatture escluse
    assert (t.issued_taxable, t.received_taxable) == (100_000, 50_000)
    checks = {c.title: c for c in data_check.run_checks(session, date(2026, 10, 10))}
    assert checks["IVA delle fatture"].status == "ok"
    assert checks["Autofatture estere (TD17/TD18)"].status == "ok"
    assert checks["Fatture con importo zero"].status == "info"
    assert checks["Movimenti bancari caricati"].status == "err"
    session.query(Invoice).filter(Invoice.number == "1").one().vat_cents = None
    session.commit()
    assert {c.title: c for c in data_check.run_checks(session)}["IVA delle fatture"].status == "err"


def test_page(client):
    page = client.get("/verifica")
    assert page.status_code == 200 and "Totali fatture" in page.text and "Verifica dati" in page.text


def test_invalid_rows_are_skipped_and_reported(session):
    from app.models import Account, Transaction
    from app.services.bank_import import ParsedTx, import_transactions

    acc = Account(name="Revolut", bank="Revolut")
    session.add(acc)
    session.flush()
    res = import_transactions(session, acc, [ParsedTx(date(1900, 1, 14), 0, "", "", "", None, 1),
                                             ParsedTx(date(2026, 9, 1), -1000, "Bar", "", "", None, 2)], "b")
    assert res.inserted == 1
    session.add(Transaction(account_id=acc.id, date=date(1900, 1, 14), amount_cents=0, description="", dedupe_key="old"))
    session.commit()
    checks = {c.title: c for c in data_check.run_checks(session, date(2026, 10, 10))}
    assert checks["Movimenti non validi"].status == "err"
    assert "1900" not in checks["Movimenti bancari caricati"].detail + " ".join(checks["Movimenti bancari caricati"].items)
