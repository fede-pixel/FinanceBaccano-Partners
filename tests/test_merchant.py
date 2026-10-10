"""Riconoscimento dell'esercente dalle descrizioni bancarie e raggruppamento delle spese."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.models import Account, CategoryRule, Transaction
from app.services import categorize as cz
from app.services.merchant import merchant_name


@pytest.mark.parametrize("desc,cp,expected", [
    ("PAGAMENTO POS 12/09/2026 ORE 10:22 CARTA N. ****1234 PRESSO FERRAMENTA BIANCHI MILANO IT", "", "Ferramenta Bianchi"),
    ("AMZN Mktp IT*1A2B3C4D5", "", "Amazon"),
    ("AMAZON EU SARL LUXEMBOURG", "", "Amazon"),
    ("AWS EMEA aws.amazon.com", "", "Amazon Web Services"),
    ("PAYPAL *NETFLIX 4029357733", "", "Netflix"),
    ("SUMUP *BAR CENTRALE ROMA", "", "Bar Centrale"),
    ("ADDEBITO DIRETTO SDD CORE MANDATO IT12ZZZ0000012345 ENEL ENERGIA SPA", "", "Enel"),
    ("BONIFICO SEPA A FAVORE DI STUDIO ROSSI COMMERCIALISTI TRN 0306260012345678 FATT 12/2026", "", "Studio Rossi Commercialisti"),
    ("Card payment", "Anthropic* Claude Sub", "Anthropic"),
    ("GOOGLE *GSUITE_baccano.it", "", "Google Workspace"),
    ("GOOGLE *ADS1234567", "", "Google Ads"),
    ("OPERAZIONE CARTA DEL 03/10/26 TELEPASS SPA", "", "Telepass"),
    ("F24 DELEGA UNIFICATA", "", "Agenzia delle Entrate"),
    ("POS TRATTORIA DA MARIO SNC 00185 ROMA ITA", "", "Trattoria Da Mario"),
    ("FACEBK *ADS 8XYZ12", "", "Meta Ads"),
])
def test_merchant_names(desc, cp, expected):
    assert merchant_name(desc, cp) == expected


def test_variants_of_same_merchant_share_category_and_group(session):
    cz.seed_rules(session)
    acc = Account(name="Revolut")
    session.add(acc)
    session.flush()
    descs = ["PAGAMENTO POS 01/09/2026 ORE 09:10 CARTA ****1234 PRESSO FERRAMENTA BIANCHI MILANO",
             "POS 15/09/2026 FERRAMENTA BIANCHI SRL MILANO IT",
             "OPERAZIONE CARTA DEL 20/09/26 FERRAMENTA BIANCHI"]
    txs = []
    for i, d in enumerate(descs):
        t = Transaction(account_id=acc.id, date=date(2026, 9, 1 + i), amount_cents=-1000 * (i + 1), description=d, dedupe_key=f"f{i}")
        session.add(t)
        txs.append(t)
    session.add(Transaction(account_id=acc.id, date=date(2026, 9, 5), amount_cents=-500, description="AMZN Mktp IT*99", dedupe_key="a"))
    session.flush()
    cz.apply(session)
    session.commit()
    clusters = cz.uncategorized_clusters(session)
    assert [(c.name, c.total, c.count) for c in clusters] == [("Ferramenta Bianchi", 6000, 3)]
    n = cz.assign_cluster(session, clusters[0].key, "Manutenzione sede")
    session.commit()
    assert n == 3 and {t.category for t in txs} == {"Manutenzione sede"}
    # un movimento futuro con un'altra variante finisce subito nella categoria giusta
    c = cz.Categorizer.load(session)
    assert c.category_for("PAGAMENTO POS 02/11/2026 ORE 18:00 FERRAMENTA BIANCHI ROMA", "", -100) == "Manutenzione sede"
    # le proposte dell'AI non sovrascrivono una tua scelta
    assert cz.assign_cluster(session, "ferramenta bianchi", "Altro", source="ai") == 0
    assert session.query(CategoryRule).filter_by(pattern="ferramenta bianchi").one().source == "tua"


class FakeAI:
    def __init__(self, answer):
        self.calls = []
        msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))])

        def create(**kw):
            self.calls.append(kw)
            return msg

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_ai_classification(session):
    from app.services import ai_categorize

    cz.seed_rules(session)
    acc = Account(name="BCC")
    session.add(acc)
    session.flush()
    for i, d in enumerate(["POS FERRAMENTA BIANCHI", "POS CARTOLERIA ROSSI", "POS MISTERO SRL"]):
        session.add(Transaction(account_id=acc.id, date=date(2026, 9, 1), amount_cents=-1000 - i, description=d, dedupe_key=d))
    session.flush()
    cz.apply(session)
    session.commit()
    clusters = cz.uncategorized_clusters(session)
    keys = {c.name: i + 1 for i, c in enumerate(clusters)}
    fake = FakeAI({"assignments": [
        {"n": keys["Ferramenta Bianchi"], "category": "Acquisti e forniture"},
        {"n": keys["Cartoleria Rossi"], "category": "Acquisti e forniture"},
        {"n": keys["Mistero"], "category": "Altro"},
        {"n": 99, "category": "Acquisti e forniture"},
    ]})
    sugg = ai_categorize.suggest(session, clusters, client=fake)
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "Ferramenta Bianchi" in prompt and "1001" not in prompt  # nessun importo inviato
    enum = fake.calls[0]["output_config"]["format"]["schema"]["properties"]["assignments"]["items"]["properties"]["category"]["enum"]
    assert "Giroconti" not in enum and "Acquisti e forniture" in enum
    assert sugg == {"ferramenta bianchi": "Acquisti e forniture", "cartoleria rossi": "Acquisti e forniture"}
    assert ai_categorize.apply_suggestions(session, sugg) == 2
    assert {r.source for r in session.query(CategoryRule).filter(CategoryRule.pattern.in_(list(sugg)))} == {"ai"}
    assert [c.name for c in cz.uncategorized_clusters(session)] == ["Mistero"]


def test_cluster_panel_from_ui(client, monkeypatch):
    from app.db import SessionLocal

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client.post("/conti/salva", data={"name": "BCC", "opening_balance": "0"})
    csv = "Data;Descrizione;Importo\n01/10/2026;POS 01/10/2026 FERRAMENTA BIANCHI MILANO;-30,00\n02/10/2026;POS FERRAMENTA BIANCHI SRL;-20,00\n".encode()
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("m.csv", csv)}, follow_redirects=False)
    token = r.headers["location"].split("/movimenti/import/")[1].split("?")[0]
    client.post(f"/movimenti/import/{token}/conferma?conto=1", data={"header_row": "0", "date_col": "Data", "amount_col": "Importo", "desc_cols": "Descrizione"})
    page = client.get("/analisi?mese=2026-10").text
    assert "Da classificare" in page and "Ferramenta Bianchi" in page and "Classifica con l" not in page
    r = client.post("/analisi/gruppo", data={"key": "ferramenta bianchi", "category": "Manutenzione sede", "back": "/analisi?mese=2026-10"})
    assert "aggiornati 2 movimenti" in r.text
    with SessionLocal() as s:
        assert {t.category for t in s.query(Transaction)} == {"Manutenzione sede"}
