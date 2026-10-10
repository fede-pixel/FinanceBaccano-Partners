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


def test_textual_dates_and_typographic_minus():
    from datetime import date

    from app.services.bank_import import parse_date_value
    from app.services.util import parse_amount

    assert parse_date_value("1 ott 2026") == date(2026, 10, 1)
    assert parse_date_value("01 Oct 2026") == date(2026, 10, 1)
    assert parse_date_value("1. Okt. 2026") == date(2026, 10, 1)
    assert parse_date_value("Oct 1, 2026") == date(2026, 10, 1)
    assert parse_date_value("15 dicembre 2026") == date(2026, 12, 15)
    assert parse_date_value("01.10.26") == date(2026, 10, 1)
    assert parse_date_value("01/10/2026 14:32 CET") == date(2026, 10, 1)
    assert parse_date_value("Saldo iniziale") is None
    assert parse_amount("−12,50 €") == -1250
    assert parse_amount("– 1.000,00") == -100000


def test_unruled_pdf_with_textual_dates():
    header = ["Data transazione", "Tipo", "Descrizione", "Importo", "Saldo"]
    xs = [40, 130, 200, 400, 480]
    rows_ = [["1 ott 2026", "Carta", "Amazon EU", "−45,20 €", "954,80 €"],
             ["3 ott 2026", "Bonifico", "Cliente Rossi", "1.000,00 €", "1.954,80 €"]]
    rows = read_table(bank_pdf([rows_], header, xs), "vivid.pdf")
    p = guess_profile(rows)
    assert p.date_col == "Data transazione" and p.amount_col == "Importo"
    txs, errors = parse_rows(rows, p)
    assert [(t.date.day, t.amount_cents) for t in txs] == [(1, -4520), (3, 100000)] and not errors


def test_boxed_header_and_footer_with_free_text_rows():
    """Come l'estratto Vivid: intestazione e piè di pagina in riquadri, movimenti come testo libero."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    xs = [40, 150, 260, 420, 500, 570]
    header = ["Data transazione", "Tipo", "Descrizione", "Importo", "Saldo"]
    pages = [[("01.10.2026", "Carta", "Amazon EU", "-45,20 €", "954,80 €"), ("02.10.2026", "Bonifico", "Cliente Rossi", "1.000,00 €", "1.954,80 €")],
             [("03.10.2026", "Addebito", "Canone", "-9,00 €", "1.945,80 €")]]
    for n, rows in enumerate(pages, start=1):
        for i, h in enumerate(header):  # intestazione in celle con bordo
            c.rect(xs[i], 740, xs[i + 1] - xs[i], 20)
            c.drawString(xs[i] + 3, 746, h)
        y = 720
        for row in rows:
            for i, v in enumerate(row):
                c.drawString(xs[i] + 3, y, v)
            y -= 16
        for i in range(len(header)):  # piè di pagina nella stessa griglia
            c.rect(xs[i], 40, xs[i + 1] - xs[i], 20)
        c.drawString(xs[1] + 3, 46, f"Pagina {n} di {len(pages)}")
        c.showPage()
    c.save()
    rows = read_table(buf.getvalue(), "vivid.pdf")
    p = guess_profile(rows)
    txs, errors = parse_rows(rows, p)
    assert [(t.date.day, t.amount_cents) for t in txs] == [(1, -4520), (2, 100000), (3, -900)], rows[:6]
    assert not errors


def test_offset_amounts_and_wide_numbers():
    """Importo stampato più in basso della data (descrizione su due righe) e più largo dell'intestazione."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    xs = [40, 140, 220, 440, 515]
    for i, h in enumerate(["Data transazione", "Tipo", "Descrizione", "Importo", "Saldo"]):
        c.drawString(xs[i], 760, h)
    y = 735
    for d, t, d1, d2, amt, bal in [
        ("01.10.2026", "Carta", "Pagamento carta", "AMAZON EU SARL LUX", "\u221212.345,20 €", "954,80 €"),
        ("02.10.2026", "Bonifico", "Bonifico in entrata", "ROSSI MARIO", "+1.000,00 €", "1.954,80 €"),
    ]:
        c.drawString(xs[0], y, d)
        c.drawString(xs[1], y, t)
        c.drawString(xs[2], y, d1)
        c.drawString(xs[2], y - 11, d2)
        c.drawRightString(xs[3] + 55, y - 6, amt)   # centrato in verticale e più largo dell'intestazione
        c.drawRightString(xs[4] + 60, y - 6, bal)
        y -= 30
    c.save()
    rows = read_table(buf.getvalue(), "vivid.pdf")
    txs, errors = parse_rows(rows, guess_profile(rows))
    assert [t.amount_cents for t in txs] == [-1234520, 100000], (rows, errors)
    assert txs[0].description == "Pagamento carta AMAZON EU SARL LUX"
    assert not errors


def test_error_message_shows_raw_amount():
    rows = [["Data", "Descrizione", "Importo"], ["01/10/2026", "x", "n.d."], ["02/10/2026", "y", ""]]
    _, errors = parse_rows(rows, guess_profile(rows))
    assert errors == ["Riga 2: importo non riconosciuto ('n.d.')", "Riga 3: importo non riconosciuto (cella vuota)"]


def test_revolut_italian_pdf_layout():
    """Come l'estratto Revolut in italiano: intestazione in grassetto su altezze leggermente diverse,
    data seguita dalla sigla (CAR/FEE), importi '€90.00', colonne Soldi in uscita / in entrata, più recenti in alto."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 9)
    c.drawString(40, 800, "00015   Valuta   EUR")
    c.drawString(40, 785, "Italy   Tipo IBAN BIC   Locale LT40 3250 0643 3460 6142 REVOLT21")
    heads = [(40, "Data", 0), (130, "Descrizione", 2.5), (330, "Soldi in uscita", 1), (420, "Soldi in entrata", 1), (510, "Saldo", 0)]
    for x, h, dy in heads:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, 750 + dy, h)
    c.setFont("Helvetica", 9)
    rows = [("28 set 2026", "CAR", "Anthropic* Claude Sub", "€90.00", "", "€59.33"),
            ("23 set 2026", "FEE", "Commissione Revolut Business", "€10.00", "", "€149.33"),
            ("20 set 2026", "TRA", "Bonifico da Cliente Rossi", "", "€1,000.00", "€159.33")]
    y = 725
    for d, kind, desc, out_, in_, bal in rows:
        c.drawString(40, y, f"{d} {kind}")
        c.drawString(130, y, desc)
        if out_:
            c.drawRightString(395, y, out_)
        if in_:
            c.drawRightString(490, y, in_)
        c.drawRightString(560, y, bal)
        y -= 22
    c.save()
    rows_ = read_table(buf.getvalue(), "revolut.pdf")
    p = guess_profile(rows_)
    assert p.date_col == "Data" and p.amount_mode == "split", (rows_[:5], p)
    assert (p.in_col, p.out_col) == ("Soldi in entrata", "Soldi in uscita")
    txs, errors = parse_rows(rows_, p)
    assert [(t.date.day, t.amount_cents) for t in txs] == [(28, -9000), (23, -1000), (20, 100000)] and not errors


def test_sign_inferred_from_balance():
    rows = [["Data", "Descrizione", "Importo", "Saldo"],
            ["28/09/2026", "Claude", "90,00", "59,33"],
            ["23/09/2026", "Commissione", "10,00", "149,33"],
            ["20/09/2026", "Cliente", "1.000,00", "159,33"],
            ["19/09/2026", "Apertura", "100,00", "-840,67"]]
    txs, _ = parse_rows(rows, guess_profile(rows))
    assert [t.amount_cents for t in txs] == [-9000, -1000, 100000, 10000]


def test_scalare_document_is_recognized(client):
    from app.services.bank_import import looks_like_scalare

    rows = [["COORDINATE BANCARIE"], ["- Valuta - Saldo per valuta - gg - Numeri S.B.F.- Numeri di Conto - Numeri Sconf.- Numeri Creditori"],
            ["17/04/26 15.983,80 A 10 159.838,00"]]
    assert looks_like_scalare(rows)
    assert not looks_like_scalare([["Data", "Descrizione", "Importo"], ["01/10/2026", "Valuta estera USD", "-10"]])
    client.post("/conti/salva", data={"name": "BCC", "opening_balance": "0"})
    csv = "Valuta;Saldo per valuta;gg;Numeri Creditori\n17/04/26;15.983,80 A;10;159.838,00\n".encode()
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("scalare.csv", csv)})
    assert "riassunto scalare" in r.text


def test_bcc_movimenti_globali_layout():
    """Lista "Movimenti Globali" BCC: ogni riga ha intestatario e rapporto prima della data,
    la parola "Data" dell'intestazione è più in alto delle altre e le intestazioni sono vicinissime."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 8)
    c.drawString(40, 800, "Banca di Credito Cooperativo di Roma")
    c.drawString(40, 790, "AG. 139 VIA SALARIA 110 MONTEROTONDO 00015 - RM")
    c.drawString(40, 780, "Movimenti Globali")
    c.setFont("Helvetica-Bold", 8)
    c.drawString(215, 756, "Data")                 # più in alto: finisce su una riga a sé
    for x, h in [(40, "Intestatario"), (150, "Rapporto"), (262, "Data valuta"), (366, "Causale"), (400, "Descrizione")]:
        c.drawString(x, 748, h)
    c.drawRightString(360, 748, "Importo")
    c.setFont("Helvetica", 8)
    rows = [("BACCANO PARTNERS SRL", "000000003415", "03/06/2026", "03/06/2026", "-1.250,00", "26", "BONIFICO A FAVORE DI FORNITORE SRL"),
            ("BACCANO PARTNERS SRL", "000000003415", "05/06/2026", "04/06/2026", "3.000,50", "48", "BONIFICO DA CLIENTE ROSSI")]
    y = 735
    for intest, rapp, d, dv, imp, caus, desc in rows:
        c.drawString(40, y, intest)
        c.drawString(150, y, rapp)
        c.drawString(215, y, d)
        c.drawString(262, y, dv)
        c.drawRightString(360, y, imp)
        c.drawString(366, y, caus)
        c.drawString(400, y, desc)
        y -= 12
    c.save()
    out = read_table(buf.getvalue(), "movimenti_globali.pdf")
    assert out[0] == ["Intestatario", "Rapporto", "Data", "Data valuta", "Importo", "Causale", "Descrizione"], out[:3]
    p = guess_profile(out)
    assert p.date_col == "Data" and p.amount_col == "Importo", p
    txs, errors = parse_rows(out, p)
    assert [(t.date.day, t.amount_cents) for t in txs] == [(3, -125000), (5, 300050)] and not errors, (out, errors)


def test_tight_header_words_split_into_columns():
    from app.services.bank_import import _header_columns

    def w(text, x0, x1):
        return {"text": text, "x0": x0, "x1": x1, "top": 100, "bottom": 108}
    cols = _header_columns([w("Data", 40, 58), w("valuta", 60.5, 85), w("Importo", 87.5, 115), w("Causale", 118, 146), w("Descrizione", 148, 190)])
    assert [c[2] for c in cols] == ["Data valuta", "Importo", "Causale", "Descrizione"]
