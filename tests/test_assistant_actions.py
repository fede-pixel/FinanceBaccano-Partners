"""L'assistente propone modifiche; si eseguono solo con il clic dell'utente."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.db import SessionLocal
from app.models import Account, CategoryRule, ChatConversation, Invoice, PlannedItem, Recurrence, Transaction
from app.services import assistant, assistant_actions as aa, invoice_match
from tests.test_assistant import Block


def _setup(s):
    bcc, rev = Account(name="BCC", bank="BCC"), Account(name="Revolut Business", bank="Revolut")
    s.add_all([bcc, rev])
    s.flush()
    ids = {}
    for key, fh in (("a", "fuL5W"), ("b", "d11b0")):  # FPR 17/26 caricata due volte
        inv = Invoice(direction="attiva", doc_type="TD01", number="FPR 17/26", date=date(2026, 5, 22), counterparty="Priscilla Gerico",
                      total_cents=990_000, vat_cents=0, file_name=f"x_{fh}.xml.p7m", file_hash=fh)
        s.add(inv)
        s.flush()
        s.add(PlannedItem(title="Incasso FPR 17/26", amount_cents=990_000, due_date=date(2026, 6, 21), invoice_id=inv.id, source="fattura"))
        ids[key] = inv.id
    s.add(Transaction(account_id=bcc.id, date=date(2026, 6, 1), amount_cents=990_000, description="BONIFICO DA PRISCILLA GERICO FPR 17/26", dedupe_key="g"))
    barone = Invoice(direction="attiva", doc_type="TD01", number="FPR 31/26", date=date(2026, 7, 2), counterparty="Simona Francesca Barone",
                     total_cents=2_335_575, vat_cents=0, file_name="x", file_hash="b31")
    s.add(barone)
    s.flush()
    s.add(PlannedItem(title="Incasso FPR 31/26", amount_cents=2_335_575, due_date=date(2026, 8, 1), invoice_id=barone.id, source="fattura"))
    s.add(Transaction(account_id=bcc.id, date=date(2026, 4, 27), amount_cents=689_000, description="BONIFICO SEPA DA S. BARONE", dedupe_key="acc"))
    s.add(Transaction(account_id=rev.id, date=date(1900, 1, 14), amount_cents=0, description="", dedupe_key="v1"))
    s.add(Transaction(account_id=bcc.id, date=date(2026, 5, 3), amount_cents=-500_000, description="BONIFICO A IT62G0306903200100000003428385", dedupe_key="iban"))
    s.add(Transaction(account_id=bcc.id, date=date(2026, 6, 3), amount_cents=-150_000, description="BONIFICO A LT403250012345678901", dedupe_key="lt"))
    s.commit()
    invoice_match.run(s)
    s.commit()
    ids["barone"] = barone.id
    ids.update({t.dedupe_key: t.id for t in s.query(Transaction)})
    return ids


def test_validation_rules(session):
    ids = _setup(session)
    linked = next(i for i in (ids["a"], ids["b"]) if invoice_match.invoice_statuses(session) and any(
        it.transactions for it in session.get(Invoice, i).planned_items))
    other = ids["b"] if linked == ids["a"] else ids["a"]
    with pytest.raises(aa.ActionError, match="collegata a un movimento"):
        aa.propose(session, "proponi_elimina_fattura_doppia", {"fattura_id": linked})
    assert "Elimina la fattura doppia" in aa.propose(session, "proponi_elimina_fattura_doppia", {"fattura_id": other})["summary"]
    with pytest.raises(aa.ActionError, match="non ha un doppione"):
        aa.propose(session, "proponi_elimina_fattura_doppia", {"fattura_id": ids["barone"]})
    with pytest.raises(aa.ActionError, match="nessun movimento"):
        aa.propose(session, "proponi_regola_categoria", {"testo": "0000003428385", "categoria": "Fornitori"})  # IBAN parziale
    p = aa.propose(session, "proponi_regola_categoria", {"testo": "IT62G0306903200100000003428385", "categoria": "Subappalti e conto terzi"})
    assert "1 movimenti" in p["summary"]
    with pytest.raises(aa.ActionError, match="importi aperti|supera"):  # fattura già incassata
        aa.propose(session, "proponi_acconto", {"movimento_id": ids["acc"], "fattura_id": linked})
    # proporre non modifica nulla
    assert session.query(Invoice).count() == 3 and session.query(CategoryRule).filter(CategoryRule.source == "tua").count() == 0


class FakeClient:
    def __init__(self, ids):
        self.ids, self.calls = ids, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        if len(self.calls) == 1:
            i = self.ids
            with SessionLocal() as s:
                dup = next(x for x in (i["a"], i["b"]) if not any(it.transactions for it in s.get(Invoice, x).planned_items))
            calls = [
                ("proponi_elimina_fattura_doppia", {"fattura_id": dup}),
                ("proponi_elimina_movimento", {"movimento_id": i["v1"]}),
                ("proponi_acconto", {"movimento_id": i["acc"], "fattura_id": i["barone"]}),
                ("proponi_regola_categoria", {"testo": "IT62G0306903200100000003428385", "categoria": "Subappalti e conto terzi"}),
                ("proponi_giroconto", {"movimenti_ids": [i["lt"]]}),
                ("proponi_ricorrenza", {"titolo": "Stipendi", "importo": 4577.5, "tipo": "uscita", "frequenza": "mensile",
                                        "giorno": 27, "inizio": "2026-10-01", "conto_id": 0, "categoria": "Stipendi e collaboratori"}),
                ("proponi_elimina_fattura_doppia", {"fattura_id": i["barone"]}),  # rifiutata: non è un doppione
            ]
            return SimpleNamespace(stop_reason="tool_use", usage=usage, content=[
                Block(type="tool_use", id=f"t{n}", name=name, input=args) for n, (name, args) in enumerate(calls)])
        return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[
            Block(type="text", text="Ti ho preparato 6 modifiche: controllale e premi Esegui.")])


def test_proposals_then_user_executes(client, monkeypatch):
    with SessionLocal() as s:
        ids = _setup(s)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient(ids)
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)
    r = client.post("/assistente/chiedi", data={"question": "Sistema i dati", "conv_id": "0"}, follow_redirects=False)
    conv_url = r.headers["location"].split("#")[0]
    page = client.get(conv_url)
    assert "Modifiche proposte (6)" in page.text and "Esegui tutte" in page.text
    # finché non premo Esegui non cambia nulla
    with SessionLocal() as s:
        assert s.query(Invoice).count() == 3 and s.query(Transaction).filter(Transaction.amount_cents == 0).count() == 1
        conv = s.query(ChatConversation).one()
        results = [m for m in json.loads(conv.api_messages) if m["role"] == "user" and isinstance(m["content"], list)][0]["content"]
        assert "non ha un doppione" in results[-1]["content"]  # errore restituito a Claude
        actions = json.loads(conv.display)[-1]["actions"]
        conv_id = conv.id
    client.post(f"/assistente/{conv_id}/azione", data={"action_ids": ",".join(a["id"] for a in actions)})
    with SessionLocal() as s:
        assert s.query(Invoice).filter(Invoice.number == "FPR 17/26").count() == 1
        assert s.query(Transaction).filter(Transaction.amount_cents == 0).count() == 0
        st = next(x for x in invoice_match.invoice_statuses(s) if x.invoice.id == ids["barone"])
        assert st.state == "aperta" and st.open_cents == 2_335_575 - 689_000  # acconto collegato
        assert s.get(Transaction, ids["iban"]).category == "Subappalti e conto terzi"
        assert s.get(Transaction, ids["lt"]).category == "Giroconti"
        assert s.query(Recurrence).one().amount_cents == -457_750
        disp = json.loads(s.query(ChatConversation).one().display)
        assert all(a["status"] == "eseguita" for a in next(d for d in disp if d.get("actions"))["actions"])
        assert sum(1 for d in disp if d["role"] == "event") == 6
    # rieseguire non fa nulla due volte
    client.post(f"/assistente/{conv_id}/azione", data={"action_ids": actions[0]["id"]})
    with SessionLocal() as s:
        assert s.query(Invoice).filter(Invoice.number == "FPR 17/26").count() == 1
    # alla domanda successiva l'assistente sa cosa è stato eseguito
    fake2 = FakeClient(ids)
    fake2.calls.append({})
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake2)
    client.post("/assistente/chiedi", data={"question": "Ricontrolla", "conv_id": str(conv_id)})
    last_user = fake2.calls[1]["messages"][-2]["content"]  # la lista è condivisa: l'ultima è la risposta
    assert "[Modifiche eseguite dall'utente nell'app:" in last_user and "Eliminata" in last_user
