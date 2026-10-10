from datetime import date

from app.models import Account, BalanceSnapshot, Transaction
from app.services.bank_import import guess_profile, import_transactions, parse_rows, read_table

REVOLUT = b"""Date started (UTC),Date completed (UTC),ID,Type,State,Description,Reference,Payer,Card number,Orig currency,Orig amount,Payment currency,Amount,Fee,Balance,Account
2026-09-03 10:00:00,2026-09-03 10:01:00,id-3,CARD_PAYMENT,COMPLETED,Google Workspace,,,4444,USD,-12.00,EUR,-11.10,0.00,4888.90,EUR Main
2026-09-02 09:00:00,2026-09-02 09:00:10,id-2,TRANSFER,COMPLETED,Cliente Rossi,Fatt 12,,,EUR,1000.00,EUR,1000.00,0.00,4900.00,EUR Main
2026-09-02 08:00:00,,id-x,CARD_PAYMENT,PENDING,Amazon,,,4444,EUR,-5.00,EUR,-5.00,0.00,,EUR Main
2026-09-01 08:00:00,2026-09-01 08:00:10,id-1,FEE,COMPLETED,Canone,,,,EUR,-100.00,EUR,-100.00,0.00,3900.00,EUR Main
"""

BCC = """Estratto movimenti conto 12345
Intestatario: Baccano Partners srl

Data contabile;Data valuta;Dare;Avere;Descrizione operazione
01/09/2026;01/09/2026;1.250,00;;BONIFICO A FAVORE DI FORNITORE SPA
05/09/2026;05/09/2026;;3.000,50;BONIFICO DA CLIENTE BIANCHI
05/09/2026;05/09/2026;;3.000,50;BONIFICO DA CLIENTE BIANCHI
Saldo finale;;;;
""".encode("cp1252")


def test_revolut_profile_and_import(session):
    rows = read_table(REVOLUT, "revolut.csv")
    p = guess_profile(rows)
    assert p.date_col == "Date completed (UTC)"
    assert p.amount_col == "Amount"
    assert p.id_col == "ID"
    assert p.balance_col == "Balance"
    assert p.status_ok == "COMPLETED"
    txs, errors = parse_rows(rows, p)
    assert [t.amount_cents for t in txs] == [-1110, 100000, -10000]
    assert errors == []

    acc = Account(name="Revolut")
    session.add(acc)
    session.commit()
    res = import_transactions(session, acc, txs, "b1")
    assert res.inserted == 3
    assert res.snapshot == (date(2026, 9, 3), 488890)
    # reimport: niente doppioni
    res2 = import_transactions(session, acc, txs, "b2")
    assert res2.inserted == 0 and res2.skipped == 3
    assert session.query(BalanceSnapshot).filter_by(account_id=acc.id).count() == 1


def test_bcc_split_columns_with_preamble(session):
    rows = read_table(BCC, "bcc.csv")
    p = guess_profile(rows)
    assert p.header_row == 3
    assert p.amount_mode == "split"
    assert p.in_col == "Avere" and p.out_col == "Dare"
    assert p.date_col == "Data contabile"
    txs, errors = parse_rows(rows, p)
    assert [t.amount_cents for t in txs] == [-125000, 300050, 300050]
    assert errors == []
    acc = Account(name="BCC")
    session.add(acc)
    session.commit()
    assert import_transactions(session, acc, txs, "b").inserted == 3  # due righe identiche sono due movimenti
    assert import_transactions(session, acc, txs, "c").inserted == 0
    assert session.query(Transaction).count() == 3


def test_xlsx_import(tmp_path):
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Data", "Descrizione", "Importo"])
    ws.append([date(2026, 9, 1), "Spesa", -10.5])
    buf = io.BytesIO()
    wb.save(buf)
    rows = read_table(buf.getvalue(), "x.xlsx")
    p = guess_profile(rows)
    txs, _ = parse_rows(rows, p)
    assert txs[0].date == date(2026, 9, 1) and txs[0].amount_cents == -1050
