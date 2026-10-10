"""Estratti conto in PDF: tabelle con e senza bordi, descrizioni su più righe, più pagine."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from app.services.bank_import import UnreadableFile, guess_profile, parse_rows, read_table


def bank_pdf(rows_per_page, header, xs, multiline=None):
    """PDF 'disegnato' come quelli delle banche: testo posizionato, nessun bordo."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for page_rows in rows_per_page:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, 800, "BANCA DI CREDITO COOPERATIVO - Estratto movimenti")
        c.setFont("Helvetica", 9)
        c.drawString(40, 785, "IBAN IT60X0542811101000000123456   Periodo 01/10/2026 - 31/10/2026")
        y = 750
        for x, h in zip(xs, header):
            c.drawString(x, y, h)
        y -= 18
        for row in page_rows:
            for x, v in zip(xs, row):
                if v:
                    c.drawRightString(x + 50, y, v) if v[0].isdigit() and "/" not in v else c.drawString(x, y, v)
            y -= 14
            if multiline and row[0] in multiline:
                c.drawString(xs[2], y, multiline[row[0]])
                y -= 14
        c.drawString(40, 40, "Pagina")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_unruled_pdf_multipage_multiline():
    header = ["Data contabile", "Data valuta", "Descrizione operazione", "Dare", "Avere", "Saldo"]
    xs = [40, 110, 180, 360, 430, 500]
    p1 = [["01/10/2026", "01/10/2026", "BONIFICO A FAVORE DI", "1.250,00", "", "8.750,00"],
          ["03/10/2026", "03/10/2026", "PAGAMENTO POS", "45,20", "", "8.704,80"]]
    p2 = [["05/10/2026", "05/10/2026", "BONIFICO DA CLIENTE ROSSI", "", "3.000,50", "11.705,30"]]
    pdf = bank_pdf([p1, p2], header, xs, multiline={"01/10/2026": "FORNITORE SRL FATT. 12/A"})
    rows = read_table(pdf, "estratto.pdf")
    p = guess_profile(rows)
    assert p.date_col == "Data contabile" and p.amount_mode == "split"
    txs, errors = parse_rows(rows, p)
    assert [t.amount_cents for t in txs] == [-125000, -4520, 300050] and not errors
    assert txs[0].description == "BONIFICO A FAVORE DI FORNITORE SRL FATT. 12/A"
    assert [t.balance_cents for t in txs] == [875000, 870480, 1170530]


def test_unruled_english_statement():
    header = ["Date", "Description", "Money out", "Money in", "Balance"]
    xs = [40, 120, 330, 410, 490]
    rows_ = [["2026-10-01", "Google Workspace", "11.10", "", "4888.90"], ["2026-10-02", "Transfer from Rossi", "", "1000.00", "5888.90"]]
    rows = read_table(bank_pdf([rows_], header, xs), "revolut.pdf")
    p = guess_profile(rows)
    assert (p.in_col, p.out_col) == ("Money in", "Money out")
    txs, _ = parse_rows(rows, p)
    assert [t.amount_cents for t in txs] == [-1110, 100000]


def test_ruled_table_pdf():
    buf = io.BytesIO()
    data = [["Data", "Descrizione", "Importo"], ["01/10/2026", "Canone", "-10,00"], ["02/10/2026", "Incasso", "250,00"]]
    t = Table(data)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "black")]))
    SimpleDocTemplate(buf, pagesize=A4).build([t])
    rows = read_table(buf.getvalue(), "ec.pdf")
    txs, _ = parse_rows(rows, guess_profile(rows))
    assert [t.amount_cents for t in txs] == [-1000, 25000]


def test_unreadable_files_raise_clean_error():
    import pytest

    with pytest.raises(UnreadableFile):
        read_table(b"%PDF-1.4 rotto", "x.pdf")
    with pytest.raises(UnreadableFile):
        read_table(b"\x00\x01\x02binario", "x.csv")


def test_pdf_upload_from_ui(client):
    client.post("/conti/salva", data={"name": "BCC", "opening_balance": "0"})
    header = ["Data", "Descrizione", "Uscite", "Entrate"]
    pdf = bank_pdf([[["01/10/2026", "POS", "45,20", ""]]], header, [40, 120, 330, 410])
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("ec.pdf", pdf)}, follow_redirects=False)
    preview = client.get(r.headers["location"])
    assert preview.status_code == 200 and "1 movimenti riconosciuti" in preview.text
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("foto.jpg", b"\xff\xd8\xff")})
    assert "non supportato" in r.text
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("x.csv", b"\x00\x00binario")})
    assert "Non riesco a leggere il file" in r.text
