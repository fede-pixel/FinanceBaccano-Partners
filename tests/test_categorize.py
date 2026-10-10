"""Categorizzazione automatica dei movimenti e analisi delle spese."""
from datetime import date

from app.models import Account, CategoryRule, Transaction
from app.services import categorize as cz


def _tx(session, acc, desc, cents, d=date(2026, 9, 10), counterparty=""):
    t = Transaction(account_id=acc.id, date=d, amount_cents=cents, description=desc, counterparty=counterparty,
                    dedupe_key=f"{desc}{cents}{d}")
    session.add(t)
    return t


def test_default_rules(session):
    cz.seed_rules(session)
    c = cz.Categorizer.load(session)
    cases = {
        "PAGAMENTO POS TELEPASS SPA": "Carburante e trasporti",
        "Google Workspace": "Software e abbonamenti",
        "GOOGLE ADS 1234": "Pubblicità e marketing",
        "AMAZON WEB SERVICES EMEA": "Software e abbonamenti",
        "AMZN Mktp IT": "Acquisti e forniture",
        "F24 DELEGA UNIFICATA": "Tasse e contributi",
        "BONIFICO STIPENDIO SETTEMBRE": "Stipendi e collaboratori",
        "BAR CENTRALE MILANO": "Ristoranti e bar",
        "BARBIERI SRL": "Altro",  # "bar" solo come parola intera
        "COMMISSIONI BONIFICO": "Banca e commissioni",
        "Giroconto verso conto BCC": "Giroconti",
        "COFFEE SHOP": "Altro",  # "fee" non deve scattare dentro "coffee"
        "Prelievo ATM": "Prelievi contanti",
    }
    for desc, expected in cases.items():
        assert c.category_for(desc, "", -1000) == expected, desc
    assert c.category_for("Bonifico da cliente Rossi", "", 50000) == "Entrate"
    assert c.category_for("Giroconto da Revolut", "", 50000) == "Giroconti"


def test_learning_from_manual_correction(session):
    cz.seed_rules(session)
    acc = Account(name="BCC")
    session.add(acc)
    session.flush()
    a = _tx(session, acc, "PAGAMENTO POS 12/09/2026 ORE 10:22 FERRAMENTA BIANCHI MILANO", -4500)
    b = _tx(session, acc, "PAGAMENTO POS 03/10/2026 ORE 18:01 FERRAMENTA BIANCHI MILANO", -1200, d=date(2026, 10, 3))
    other = _tx(session, acc, "PAGAMENTO POS ESSELUNGA", -3000)
    cz.apply(session, [a, b, other])
    session.commit()
    assert a.category == "Altro" and other.category == "Acquisti e forniture"
    key, updated = cz.learn(session, a, "Manutenzione sede")
    assert key == "ferramenta bianchi"  # città, data, ora e numeri esclusi
    assert a.category_source == "manuale" and b.category == "Manutenzione sede" and updated >= 1
    assert other.category == "Acquisti e forniture"
    # i movimenti futuri dello stesso fornitore vengono riconosciuti, anche con parole in mezzo
    c = cz.Categorizer.load(session)
    assert c.category_for("POS 99 FERRAMENTA 123 BIANCHI MILANO", "", -100) == "Manutenzione sede"
    # una correzione manuale non viene mai sovrascritta da un ricalcolo
    other.category, other.category_source = "Personale", "manuale"
    cz.apply(session)
    assert other.category == "Personale"


def test_spending_report(session):
    cz.seed_rules(session)
    acc = Account(name="Revolut")
    session.add(acc)
    session.flush()
    for desc, cents in [("Google Workspace", -1200), ("Notion", -800), ("TELEPASS", -5000), ("Bonifico cliente", 100000),
                        ("Giroconto a BCC", -20000), ("FERRAMENTA X", -1000)]:
        _tx(session, acc, desc, cents)
    session.commit()
    r = cz.spending_report(session, date(2026, 9, 1), date(2026, 9, 30))
    assert r.total == 8000 and r.income == 100000 and r.internal == 20000
    assert [c.category for c in r.categories] == ["Carburante e trasporti", "Software e abbonamenti", "Altro"]
    assert r.categories[1].total == 2000 and r.categories[1].count == 2
    assert round(r.uncategorized_share, 3) == 0.125
    assert r.top_merchants[0][0] == "Telepass"


def test_analysis_page_and_rules_from_ui(client):
    from app.db import SessionLocal

    client.post("/conti/salva", data={"name": "BCC", "opening_balance": "0"})
    csv = "Data;Descrizione;Importo\n01/10/2026;TELEPASS SPA;-50,00\n02/10/2026;Google Workspace;-12,00\n03/10/2026;FERRAMENTA ROSSI;-30,00\n".encode()
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("m.csv", csv)}, follow_redirects=False)
    token = r.headers["location"].split("/movimenti/import/")[1].split("?")[0]
    client.post(f"/movimenti/import/{token}/conferma?conto=1",
                data={"header_row": "0", "date_col": "Data", "amount_col": "Importo", "desc_cols": "Descrizione"})
    with SessionLocal() as s:
        cats = {t.description: t.category for t in s.query(Transaction)}
    assert cats == {"TELEPASS SPA": "Carburante e trasporti", "Google Workspace": "Software e abbonamenti", "FERRAMENTA ROSSI": "Altro"}

    r = client.get("/analisi?mese=2026-10")
    assert r.status_code == 200 and "Carburante e trasporti" in r.text and "spending-data" in r.text
    r = client.post("/analisi/regole", data={"pattern": "Ferramenta", "category": "Manutenzione sede"})
    assert "aggiornati 1 movimenti" in r.text
    with SessionLocal() as s:
        rule_id = s.query(CategoryRule).filter_by(pattern="ferramenta").one().id
    client.post(f"/analisi/regole/{rule_id}/elimina")
    with SessionLocal() as s:
        assert s.query(Transaction).filter_by(description="FERRAMENTA ROSSI").one().category == "Altro"

    # correzione dalla pagina Movimenti: crea la regola
    with SessionLocal() as s:
        tx_id = s.query(Transaction).filter_by(description="FERRAMENTA ROSSI").one().id
    r = client.post(f"/movimenti/{tx_id}/categoria", data={"category": "Manutenzione sede", "back": "/movimenti"})
    assert "Regola creata" in r.text
    r = client.get("/movimenti?categoria=Manutenzione%20sede")
    assert "FERRAMENTA ROSSI" in r.text and "TELEPASS" not in r.text
    for p in ["mese", "mese_prec", "3m", "6m", "12m", "anno"]:
        assert client.get(f"/analisi?periodo={p}").status_code == 200


def test_keywords_match_only_at_word_start(session):
    cz.seed_rules(session)
    c = cz.Categorizer.load(session)
    assert c.category_for("PAGAMENTO POS PRESSO FERRAMENTA BIANCHI", "", -100) == "Altro"  # non "esso"
    assert c.category_for("ESSO STATION ROMA", "", -100) == "Carburante e trasporti"
    assert c.category_for("ASSICURAZIONE AUTO RCA", "", -100) == "Assicurazioni"  # prefisso "assicuraz"
    assert c.category_for("SPESE BONIFICO", "", -100) == "Banca e commissioni"


def test_transfers_to_people_and_small_firms_are_subcontracts(session):
    cz.seed_rules(session)
    c = cz.Categorizer.load(session)
    sub = "Subappalti e conto terzi"
    cases = [
        ("Bonifico SEPA a favore di Giorgio Cicero", ""),
        ("BON. ISTANTANEO DAVIDE MASSIMIANI CRO 123456", ""),
        ("Bonifico disposto a favore di RENOVATIO DI CIPRIANI E BOEZIO", ""),
        ("To Davide Massimiani", ""),  # Revolut
        ("Bonifico saldo fattura 12/2026", "Giorgio Cicero"),  # "fattura" generica: vince il subappalto
        ("Pagamento manodopera cantiere", ""),
    ]
    for desc, cp in cases:
        assert c.category_for(desc, cp, -150000) == sub, desc
    # le categorie specifiche e i giroconti restano tali
    assert c.category_for("BONIFICO STIPENDIO SETTEMBRE MARIO ROSSI", "", -150000) == "Stipendi e collaboratori"
    assert c.category_for("Bonifico a favore Studio commercialista Verdi", "", -50000) == "Consulenze e professionisti"
    assert c.category_for("Bonifico F24 Agenzia delle Entrate", "", -50000) == "Tasse e contributi"
    assert c.category_for("Bonifico a favore di Baccano Partners srl", "", -50000) == "Giroconti"
    assert c.category_for("To EUR pocket", "", -50000) == "Giroconti"
    # entrate e pagamenti con carta non cambiano
    assert c.category_for("Bonifico da Giorgio Cicero", "", 50000) == "Entrate"
    assert c.category_for("PAGAMENTO POS FERRAMENTA BIANCHI", "", -5000) == "Altro"
