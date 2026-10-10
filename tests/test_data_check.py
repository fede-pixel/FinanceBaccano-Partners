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


def test_supplier_advances_possibly_double_counted(session):
    def inv(n, tipo, d, taxable):
        session.add(Invoice(direction="passiva", doc_type=tipo, number=n, date=d, counterparty="MONDATI E SELVA SRL",
                            total_cents=taxable * 122 // 100, vat_cents=taxable * 22 // 100, file_name="x", file_hash=n))
    inv("206", "TD02", date(2026, 5, 21), 540_000)
    inv("327", "TD02", date(2026, 7, 16), 540_000)
    inv("333", "TD01", date(2026, 7, 18), 540_000)
    session.commit()
    c = {c.title: c for c in data_check.run_checks(session)}["Acconti fornitori forse contati due volte"]
    assert len(c.actions) == 1 and "n. 327" in c.actions[0]["text"] and "5.400,00" in c.detail
    # "è corretto": non viene più segnalato
    session.query(Invoice).filter(Invoice.number == "327").one().cost_treatment = "verificato"
    session.commit()
    assert {c.title: c for c in data_check.run_checks(session)}["Acconti fornitori forse contati due volte"].status == "ok"


def test_cost_treatment_moves_invoice_out_of_ebitda(client):
    from app.db import SessionLocal
    from app.services import kpi

    with SessionLocal() as s:
        s.add(Invoice(direction="attiva", doc_type="TD01", number="E1", date=date(2026, 3, 1), counterparty="Cliente",
                      total_cents=1_100_000, vat_cents=100_000, file_name="e", file_hash="e"))
        s.add(Invoice(direction="passiva", doc_type="TD01", number="FPR 10/26", date=date(2026, 6, 3), counterparty="A&V MOTORS",
                      total_cents=638_999, vat_cents=115_229, file_name="a", file_hash="a"))
        s.add(Invoice(direction="passiva", doc_type="TD02", number="ACC", date=date(2026, 7, 29), counterparty="MONDIAL",
                      total_cents=418_503, vat_cents=75_468, file_name="m", file_hash="m"))
        s.commit()
        ids = {i.number: i.id for i in s.query(Invoice)}
        before = kpi.build(s, today=date(2026, 10, 10))
    assert before.ytd_ebitda == 1_000_000 - 523_770 - 343_035
    assert client.post("/fatture/trattamento-costo", data={"invoice_id": ids["FPR 10/26"], "value": "cespite"}).status_code == 200
    client.post("/fatture/trattamento-costo", data={"invoice_id": ids["ACC"], "value": "escluso"})
    assert client.post("/fatture/trattamento-costo", data={"invoice_id": ids["E1"], "value": "cespite"}).status_code == 200
    with SessionLocal() as s:
        rep = kpi.build(s, today=date(2026, 10, 10))
        assert s.get(Invoice, ids["E1"]).cost_treatment == ""  # solo fatture ricevute
        prov = kpi.provisional_view(s, date(2026, 7, 31))
    assert rep.ytd_ebitda == 1_000_000
    r = rep.recon
    assert (r["capex"], r["excluded_costs"]) == (523_770, 343_035)
    # il confronto con Aruba conta ancora tutte le ricevute e torna all'EBITDA
    assert r["aruba_like"] == 1_000_000 - 523_770 - 343_035
    assert r["aruba_like"] - r["self_costs"] - r["personnel"] + r["capex"] + r["excluded_costs"] == rep.ytd_ebitda
    assert prov.costs_all == 0
    page = client.get("/verifica").text
    assert "Fatture fuori dai costi" in page and "Rimetti tra i costi" in page
    assert "Beni da ammortizzare" in client.get("/kpi").text
