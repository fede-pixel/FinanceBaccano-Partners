"""Lettura PDF con Claude: client simulato, nessuna chiamata reale all'API."""
import json
from types import SimpleNamespace

import pytest

from app.services import ai_extract

PDF = b"%PDF-1.4 finto"
DATA = {
    "bank_name": "BCC di Prova", "account_iban": "IT60X0542811101000000123456", "currency": "EUR",
    "transactions": [
        {"date": "2026-10-01", "description": "BONIFICO A FORNITORE  SRL", "counterparty": "Fornitore Srl", "amount": -1250.0, "balance": 8750.0},
        {"date": "2026-10-03", "description": "POS ESSELUNGA", "counterparty": "", "amount": -45.2, "balance": 8704.8},
        {"date": "2026-10-05", "description": "ACCREDITO CLIENTE", "counterparty": "Rossi", "amount": 3000.5, "balance": 11705.3},
        {"date": "non-una-data", "description": "riga rotta", "counterparty": "", "amount": 1, "balance": None},
    ],
}


class FakeClient:
    def __init__(self, text=json.dumps(DATA), stop_reason="end_turn"):
        self.calls = []
        msg = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                              usage=SimpleNamespace(input_tokens=12000, output_tokens=900), model="claude-opus-5-5")
        outer = self

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return msg

        def stream(**kwargs):
            outer.calls.append(kwargs)
            return Stream()

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))


def test_extract_builds_request_and_parses():
    client = FakeClient()
    res = ai_extract.extract_pdf(PDF, client=client)
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["messages"][0]["content"][0]["type"] == "document"
    assert [t.amount_cents for t in res.transactions] == [-125000, -4520, 300050]  # la riga rotta è scartata
    assert res.transactions[0].description == "BONIFICO A FORNITORE SRL"
    assert res.transactions[2].balance_cents == 1170530
    assert ai_extract.estimated_cost_eur(res) == round((12000 * 4 + 900 * 20) / 1e6 * 0.92, 2)
    again = ai_extract.AIResult.from_json(res.to_json())
    assert [t.amount_cents for t in again.transactions] == [-125000, -4520, 300050]


def test_refusal_and_truncation_are_reported():
    with pytest.raises(ai_extract.AIExtractionError, match="non ha elaborato"):
        ai_extract.extract_pdf(PDF, client=FakeClient(stop_reason="refusal"))
    with pytest.raises(ai_extract.AIExtractionError, match="troppo lungo"):
        ai_extract.extract_pdf(PDF, client=FakeClient(stop_reason="max_tokens"))
    with pytest.raises(ai_extract.AIExtractionError, match="non sono stati trovati"):
        ai_extract.extract_pdf(PDF, client=FakeClient(text=json.dumps({**DATA, "transactions": []})))


def test_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not ai_extract.is_enabled()
    with pytest.raises(ai_extract.AIUnavailable):
        ai_extract.extract_pdf(PDF)


def test_ai_flow_from_ui(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import Transaction

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    real = ai_extract.extract_pdf
    monkeypatch.setattr(ai_extract, "extract_pdf", lambda content, client=None: real(content, client=fake))
    client.post("/conti/salva", data={"name": "BCC", "iban": "IT99Z0000000000000000000000", "opening_balance": "0"})
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(40, 800, "Estratto illeggibile")
    c.save()
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("ec.pdf", buf.getvalue())}, follow_redirects=False)
    preview_url = r.headers["location"]
    token = preview_url.split("/movimenti/import/")[1].split("?")[0]
    assert "Leggi con l&#39;AI" in client.get(preview_url).text or "Leggi con l'AI" in client.get(preview_url).text
    r = client.post(f"/movimenti/import/{token}/ai?conto=1")
    assert "Lettura con AI – BCC" in r.text and "3</div>" in r.text
    assert "IBAN nel documento" in r.text  # avviso: IBAN diverso dal conto
    r = client.post(f"/movimenti/import/{token}/ai/conferma?conto=1", data={"update_balance": "1"})
    assert "Importati 3 movimenti" in r.text and "Saldo aggiornato al 05/10/2026" in r.text
    with SessionLocal() as s:
        assert sorted(t.amount_cents for t in s.query(Transaction)) == [-125000, -4520, 300050]
        assert {t.category for t in s.query(Transaction)} >= {"Entrate"}  # categorizzati anche i movimenti letti con l'AI


def test_ai_button_hidden_without_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client.post("/conti/salva", data={"name": "BCC", "opening_balance": "0"})
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(40, 800, "x")
    c.save()
    r = client.post("/movimenti/import", data={"account_id": "1"}, files={"file": ("ec.pdf", buf.getvalue())})
    assert "ANTHROPIC_API_KEY" in r.text and "Leggi con l" not in r.text
