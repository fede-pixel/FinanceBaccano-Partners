"""Assistente AI: strumenti in sola lettura e ciclo domanda/strumenti/risposta (Claude simulato)."""
import json
from datetime import date
from types import SimpleNamespace

from app.models import Account, ChatConversation, Invoice, PlannedItem, Setting, Transaction
from app.services import assistant


class Block(SimpleNamespace):
    def to_dict(self):
        return {k: v for k, v in vars(self).items()}


class FakeClient:
    """Prima risposta: chiede due strumenti; seconda: risponde col testo."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # copia: la lista viene poi estesa
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200)
        if len(self.calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", usage=usage, content=[
                Block(type="text", text="Controllo i dati."),
                Block(type="tool_use", id="t1", name="panoramica", input={}),
                Block(type="tool_use", id="t2", name="esegui_sql",
                      input={"query": "SELECT counterparty, SUM(total_cents) FROM invoices GROUP BY counterparty"}),
            ])
        return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[
            Block(type="text", text="Avete **1.000,00 €** da incassare da Alfa Spa.\n- consiglio: sollecitare"),
        ])


def _data(session):
    acc = Account(name="BCC", bank="BCC")
    session.add(acc)
    session.flush()
    session.add(Transaction(account_id=acc.id, date=date(2026, 9, 1), amount_cents=-5000, description="POS BAR", dedupe_key="a"))
    inv = Invoice(direction="attiva", doc_type="TD01", number="9", date=date(2026, 8, 1), counterparty="Alfa Spa",
                  total_cents=100000, file_name="x", file_hash="h")
    session.add(inv)
    session.flush()
    session.add(PlannedItem(title="Incasso fatt. 9", amount_cents=100000, due_date=date(2026, 8, 31), invoice_id=inv.id,
                            source="fattura"))
    session.add(Setting(key="eb_private_key", value="SEGRETO"))
    session.commit()


def test_sql_tool_is_read_only_and_hides_secrets(session):
    _data(session)
    ok = assistant.tool_sql(session, "SELECT counterparty, total_cents FROM invoices")
    assert ok["righe"] == [["Alfa Spa", 100000]]
    assert "errore" in assistant.tool_sql(session, "SELECT * FROM settings")
    assert "errore" in assistant.tool_sql(session, "WITH x AS (SELECT value FROM settings) SELECT * FROM x")
    assert "errore" in assistant.tool_sql(session, "DELETE FROM invoices")
    assert "errore" in assistant.tool_sql(session, "SELECT 1; DELETE FROM invoices")
    assert "errore" in assistant.tool_sql(session, "SELECT * FROM bank_connections")
    assert session.query(Invoice).count() == 1
    assert "settings" not in assistant.schema_text() and "invoices(" in assistant.schema_text()


def test_tools_return_data(session):
    _data(session)
    pan = json.loads(assistant.run_tool(session, "panoramica", {}))
    assert pan["conti"][0]["conto"] == "BCC" and pan["fatture_da_incassare"]["numero"] == 1
    fat = json.loads(assistant.run_tool(session, "fatture", {"direzione": "emesse", "stato": "aperta", "controparte": "alfa", "limite": 10}))
    assert fat["trovate"] == 1 and fat["fatture"][0]["residuo"].startswith("1.000,00")
    spese = json.loads(assistant.run_tool(session, "analisi_spese", {"da": "2026-09-01", "a": "2026-09-30"}))
    assert spese["uscite_totali"].startswith("50,00")
    assert "giorni" in json.loads(assistant.run_tool(session, "previsione_liquidita", {"giorni": 30}))
    assert "scadenze" in json.loads(assistant.run_tool(session, "scadenze_documenti", {"entro_giorni": 30}))
    assert "errore" in json.loads(assistant.run_tool(session, "analisi_spese", {"da": "boh", "a": "2026-09-30"}))


def test_conversation_loop(session):
    _data(session)
    conv = ChatConversation()
    session.add(conv)
    session.flush()
    fake = FakeClient()
    answer = assistant.ask(session, conv, "Chi ci deve pagare?", client=fake)
    assert "1.000,00" in answer
    # seconda chiamata: contiene i risultati di entrambi gli strumenti in un unico messaggio utente
    second = fake.calls[1]["messages"]
    results = second[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "Alfa Spa" in results[1]["content"]
    assert fake.calls[0]["tools"] and fake.calls[0]["fallbacks"] == "default"
    disp = json.loads(conv.display)
    assert disp[-1]["tools"] == ["esegui_sql", "panoramica"] and disp[-1]["cost_eur"] is not None
    assert conv.title == "Chi ci deve pagare?"
    # la conversazione continua: la storia viene rimandata
    fake2 = FakeClient()
    fake2.calls.append({})  # salta la fase strumenti: risponde subito
    assistant.ask(session, conv, "E i fornitori?", client=fake2)
    assert len(fake2.calls[1]["messages"]) == 5  # domanda, risposta+strumenti, risultati, risposta, nuova domanda


def test_page_and_markdown(client, monkeypatch):
    page = client.get("/assistente")
    assert page.status_code == 200 and "Assistente non attivo" in page.text
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)
    r = client.post("/assistente/chiedi", data={"question": "Come stiamo?", "conv_id": "0"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"].split("#")[0])
    assert "<strong>1.000,00 €</strong>" in page.text and "<li>consiglio: sollecitare</li>" in page.text
    assert "Come stiamo?" in page.text and "Dati consultati" in page.text
