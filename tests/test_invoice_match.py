"""Controllo incrociato fatture <-> movimenti bancari."""
from datetime import date

from app.models import Account, Invoice, PlannedItem, Transaction
from app.services import invoice_match as im
from app.services import reconcile


def _acc(session):
    acc = Account(name="BCC", bank="BCC")
    session.add(acc)
    session.flush()
    return acc


def _inv(session, number, party, cents, d, direction="passiva", tipo="TD01", vat="", status="previsto", due=None):
    inv = Invoice(direction=direction, doc_type=tipo, number=number, date=d, counterparty=party, counterparty_vat=vat,
                  total_cents=cents, file_name="x.xml", file_hash=f"{number}{party}{tipo}")
    session.add(inv)
    session.flush()
    sign = -1 if direction == "passiva" else 1
    if tipo == "TD04":
        sign = -sign
    session.add(PlannedItem(title=f"fatt {number}", amount_cents=sign * abs(cents), due_date=due or d, invoice_id=inv.id,
                            source="fattura", status=status, counterparty=party))
    session.flush()
    return inv


def _tx(session, acc, d, cents, desc, cp=""):
    t = Transaction(account_id=acc.id, date=d, amount_cents=cents, description=desc, counterparty=cp, dedupe_key=f"{d}{cents}{desc}")
    session.add(t)
    session.flush()
    return t


def _state(session, inv):
    return next(s for s in im.invoice_statuses(session) if s.invoice.id == inv.id)


def test_name_and_number_helpers():
    assert im.name_matches("ZENZERO S.R.L.", "PAGAMENTO POS ZENZERO ROMA")
    assert im.name_matches("FLORENTIN GEORGIAN ROSU", "Bonifico a favore di ROSU FLORENTIN")
    assert not im.name_matches("FLORENTIN GEORGIAN ROSU", "Bonifico a favore di Mario Florentin Bianchi")  # una sola parola su 3
    assert im.name_matches("AMAZON EU SARL", "AMZN Mktp IT*1A2B3")  # stesso marchio
    assert not im.name_matches("STUDIO ASS.TO S.R.L.", "Bonifico studio")  # solo parole generiche
    assert im.number_matches("FPR 25/26", "BONIFICO SALDO FATT. FPR 25/26 CIANCARELLA")
    assert im.number_matches("059", "saldo fattura n. 59 del 09/10")
    assert not im.number_matches("1", "fattura 1")  # numeri di una cifra: non bastano
    assert not im.number_matches("7527/A/2026", "fattura 7527")


def test_auto_match_and_statuses(session):
    acc = _acc(session)
    a = _inv(session, "FPR 25/26", "Ciancarella Alessio", 80200, date(2026, 9, 1), due=date(2026, 10, 1))
    b = _inv(session, "106/ZN", "ZENZERO S.R.L.", 20993, date(2026, 9, 28))
    c = _inv(session, "9", "Alfa Beta Spa", 122000, date(2026, 8, 1), direction="attiva")
    d = _inv(session, "77", "Fornitore Beta", 50000, date(2026, 8, 1))  # nessun pagamento
    e = _inv(session, "88", "Gamma Srl", 30000, date(2026, 1, 10), status="pagato")  # segnata pagata (storico)
    t1 = _tx(session, acc, date(2026, 10, 3), -80200, "BONIFICO A FAVORE DI CIANCARELLA ALESSIO")
    t2 = _tx(session, acc, date(2026, 9, 28), -20993, "PAGAMENTO POS ZENZERO")
    t3 = _tx(session, acc, date(2026, 9, 2), 122000, "BONIFICO DA ALFA BETA SPA SALDO FATTURA 9")
    _tx(session, acc, date(2026, 9, 5), -50000, "PRELIEVO")  # stesso importo di d ma nessuna prova
    _tx(session, acc, date(2026, 2, 1), -30000, "Bonifico Gamma srl fattura 88")
    res = im.run(session)
    assert res.matched == 4
    assert t1.planned_item_id == a.planned_items[0].id and t2.planned_item_id == b.planned_items[0].id
    assert t3.planned_item_id == c.planned_items[0].id
    assert _state(session, a).state == "pagata" and _state(session, c).state == "pagata"
    assert _state(session, e).state == "pagata"  # verificata con il movimento
    st = _state(session, d)
    assert st.state == "aperta" and st.open_cents == 50000
    # proposta solo importo (unico candidato, sopra i 50 €)
    assert len(res.proposals) == 1 and res.proposals[0].items[0].invoice_id == d.id
    # annullando l'abbinamento la fattura torna da pagare
    reconcile.unmatch(session, t1)
    assert _state(session, a).state == "aperta"


def test_one_transfer_for_many_invoices_and_credit_notes(session):
    acc = _acc(session)
    i1 = _inv(session, "001", "Renovatio di Cipriani e Boezio", 100000, date(2026, 7, 1), vat="111")
    i2 = _inv(session, "002", "Renovatio di Cipriani e Boezio", 250000, date(2026, 7, 15), vat="111")
    i3 = _inv(session, "003", "Renovatio di Cipriani e Boezio", 70000, date(2026, 7, 20), vat="111")
    t = _tx(session, acc, date(2026, 8, 5), -350000, "Bonifico SEPA a favore di RENOVATIO DI CIPRIANI E BOEZIO")
    f = _inv(session, "027", "ROSSI MARIO", 346000, date(2026, 4, 1), vat="222")
    n = _inv(session, "027", "ROSSI MARIO", 346000, date(2026, 4, 1), tipo="TD04", vat="222")
    res = im.run(session)
    assert res.matched == 2 and res.compensated == 1
    assert _state(session, i1).state == "pagata" and _state(session, i2).state == "pagata"
    assert _state(session, i3).state == "aperta"
    assert _state(session, i2).transactions == [t]
    assert _state(session, f).state == "compensata" and _state(session, n).state == "compensata"
    reconcile.unmatch(session, t)  # scollega tutte le fatture del bonifico
    assert _state(session, i1).state == "aperta" and _state(session, i2).state == "aperta"


def test_situation_page_and_export(client):
    from app.db import SessionLocal

    with SessionLocal() as s:
        acc = _acc(s)
        _inv(s, "FPR 25/26", "Ciancarella Alessio", 80200, date(2026, 9, 1))
        _inv(s, "77", "Fornitore Beta", 50000, date(2026, 8, 1))
        _inv(s, "9", "Alfa Beta Spa", 122000, date(2026, 8, 1), direction="attiva")
        _tx(s, acc, date(2026, 9, 20), -80200, "BONIFICO CIANCARELLA ALESSIO")
        s.commit()
    page = client.get("/fatture/situazione")
    assert page.status_code == 200
    assert "Fornitore Beta" in page.text and "Ciancarella" not in page.text.split("Da confermare")[0].split("<table")[-1]
    assert "1 scadenze abbinate" in page.text
    inc = client.get("/fatture/situazione?vista=incassare")
    assert "Alfa Beta" in inc.text
    paid = client.get("/fatture/situazione?vista=pagate")
    assert "Ciancarella" in paid.text and "Scollega" in paid.text
    x = client.get("/fatture/situazione.xlsx?vista=pagare")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    # segna pagata a mano
    with SessionLocal() as s:
        item = s.query(PlannedItem).join(Invoice).filter(Invoice.number == "77").one()
        iid = item.id
    r = client.post("/fatture/situazione/segna", data={"item_ids": str(iid), "paid_date": "2026-09-01"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Fornitore Beta" in client.get("/fatture/situazione?vista=pagate").text

