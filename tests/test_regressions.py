"""Casi emersi dalla revisione del codice: ognuno riproduce un bug corretto."""
import io
from datetime import date

import openpyxl

from app.models import Account, ChecklistItem, DocCategory, PlannedItem, Recurrence
from app.services import admin_docs as ad
from app.services import weekly
from app.services.bank_import import ImportProfile, guess_profile, parse_rows, read_table
from app.services.recurrence import sync_recurrence
from app.services.util import set_setting


def test_changing_recurrence_day_does_not_duplicate_month(session):
    rec = Recurrence(title="Affitto", amount_cents=-100000, frequency="mensile", day_of_month=5, start_date=date(2026, 9, 1))
    session.add(rec)
    session.flush()
    sync_recurrence(session, rec, date(2026, 9, 1))
    session.commit()
    october = session.query(PlannedItem).filter_by(recurrence_id=rec.id, due_date=date(2026, 10, 5)).one()
    october.status = "pagato"
    session.commit()
    rec.day_of_month = 25
    sync_recurrence(session, rec, date(2026, 10, 10))
    session.commit()
    oct_items = [p for p in session.query(PlannedItem).filter_by(recurrence_id=rec.id) if (p.due_date.year, p.due_date.month) == (2026, 10)]
    assert len(oct_items) == 1 and oct_items[0].status == "pagato"
    nov = [p for p in session.query(PlannedItem).filter_by(recurrence_id=rec.id) if p.due_date.month == 11 and p.due_date.year == 2026]
    assert [p.due_date for p in nov] == [date(2026, 11, 25)]


def test_semicolon_csv_with_commas_in_text():
    csv = (
        "Data;Descrizione;Importo;Saldo\n"
        "01/09/2026;PAGAMENTO POS, CARTA 1234, ESSELUNGA, MILANO;-45,20;1.954,80\n"
        "02/09/2026;BONIFICO, ROSSI, FATT. 12, SALDO;1.000,00;2.954,80\n"
        "03/09/2026;COMMISSIONI, CANONE;-2,50;2.952,30\n"
    ).encode()
    rows = read_table(csv, "x.csv")
    assert rows[0] == ["Data", "Descrizione", "Importo", "Saldo"]
    txs, errors = parse_rows(rows, guess_profile(rows))
    assert [t.amount_cents for t in txs] == [-4520, 100000, -250] and not errors


def test_importo_dare_avere_headers_use_split_mode():
    csv = "Data operazione;Descrizione;Importo dare;Importo avere\n01/09/2026;POS;45,20;\n02/09/2026;Bonifico;;1.000,00\n".encode()
    rows = read_table(csv, "x.csv")
    p = guess_profile(rows)
    assert p.amount_mode == "split" and p.out_col == "Importo dare" and p.in_col == "Importo avere"
    txs, _ = parse_rows(rows, p)
    assert [t.amount_cents for t in txs] == [-4520, 100000]


def test_excel_numbers_ignore_decimal_separator_choice():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Data", "Descrizione", "Importo"])
    ws.append([date(2026, 9, 1), "a", -12.5])
    ws.append([date(2026, 9, 2), "b", 1234.56])
    ws.append([date(2026, 9, 3), "c", 7])
    buf = io.BytesIO()
    wb.save(buf)
    rows = read_table(buf.getvalue(), "x.xlsx")
    for sep in ("auto", ",", "."):
        p = guess_profile(rows)
        p.decimal_sep = sep
        assert [t.amount_cents for t in parse_rows(rows, p)[0]] == [-1250, 123456, 700], sep


def test_weekly_upload_prefers_iban_over_matching_format(session):
    fmt = ImportProfile(header_row=0, date_col="Data", amount_col="Importo", desc_cols=["Descrizione"]).to_json()
    intesa = Account(name="Intesa", bank="Intesa", csv_profile=fmt)
    unicredit = Account(name="Unicredit", bank="Unicredit", iban="IT02L1234512345123456789012")
    session.add_all([intesa, unicredit])
    session.commit()
    rows = [["Data", "Descrizione", "Importo"], ["01/09/2026", "IT02L1234512345123456789012 bonifico", "-1"]]
    assert weekly.match_account(rows, "export.csv", [intesa, unicredit]) == (unicredit, True)
    assert weekly.match_account(rows[:1] + [["01/09/2026", "x", "-1"]], "unicredit_settembre.csv", [intesa, unicredit]) == (unicredit, True)
    assert weekly.match_account(rows[:1] + [["01/09/2026", "x", "-1"]], "export.csv", [intesa, unicredit]) == (intesa, False)


def test_due_day_change_updates_open_items(client, session):
    client.post("/impostazioni", data={"admin_start_period": "2026-09"})
    client.get("/documenti")
    cat = session.query(DocCategory).filter_by(code="movimenti_banche").one()
    form = {f"cat_{c.id}_active": "1" for c in session.query(DocCategory)}
    form[f"cat_{cat.id}_due_day"] = "20"
    client.post("/impostazioni", data=form)
    session.expire_all()
    item = session.query(ChecklistItem).filter_by(category_id=cat.id, period_key="2026-09").one()
    assert item.due_date == date(2026, 10, 20)


def test_mark_sent_includes_items_marked_ready_without_files(session):
    set_setting(session, "admin_start_period", "2026-09")
    session.commit()
    ad.ensure_checklist(session, date(2026, 10, 10))
    it = session.query(ChecklistItem).join(DocCategory).filter(DocCategory.code == "assegni_emessi", ChecklistItem.period_key == "2026-09").one()
    it.status = "completato"
    session.commit()
    ad.mark_sent(session, ad.package_items(session, "2026-09"))
    assert it.status == "inviato"


def test_invalid_month_in_url_does_not_crash(client):
    for url in ["/pacchetto?mese=2026-13", "/pacchetto?mese=abc", "/forecast?mese=1-01", "/forecast?mese=2026-00",
                "/movimenti?mese=2026-13", "/previsti?mese=xx"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/pacchetto/scarica?mese=../../x").status_code == 404
