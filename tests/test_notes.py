"""Note per il commercialista: dall'assistente all'email."""
import json
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import AccountantNote, ChatConversation, Setting
from app.services import assistant_actions as aa, notes as ns
from tests.test_assistant import Block


def _setup():
    with SessionLocal() as s:
        s.merge(Setting(key="accountant_email", value="studio@example.com"))
        s.merge(Setting(key="company_name", value="Baccano Partners"))
        s.commit()


def test_assistant_proposes_note_and_user_saves_it(client, monkeypatch):
    _setup()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []

    def create(**kw):
        calls.append(kw)
        usage = SimpleNamespace(input_tokens=10, output_tokens=10)
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", usage=usage, content=[Block(
                type="tool_use", id="n1", name="proponi_nota_commercialista",
                input={"titolo": "Svalutazione crediti Barone", "testo": "FPR 31/26 e 39/26, 30.941,75 €: come svalutarli?"})])
        return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[Block(type="text", text="Chiedi al commercialista.")])

    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    r = client.post("/assistente/chiedi", data={"question": "Barone?", "conv_id": "0"}, follow_redirects=False)
    page = client.get(r.headers["location"].split("#")[0])
    assert "Salva nota" in page.text and "Salva come nota per il commercialista" in page.text
    with SessionLocal() as s:
        assert s.query(AccountantNote).count() == 0  # finché non clicchi, niente
        conv = s.query(ChatConversation).one()
        action = json.loads(conv.display)[-1]["actions"][0]
        conv_id = conv.id
    client.post(f"/assistente/{conv_id}/azione", data={"action_ids": action["id"]})
    # anche l'intera risposta si può salvare come nota
    client.post(f"/assistente/{conv_id}/nota", data={"index": 1})
    with SessionLocal() as s:
        notes = s.query(AccountantNote).order_by(AccountantNote.id).all()
        assert [n.title for n in notes] == ["Svalutazione crediti Barone", "Barone?"]
        assert all(n.source == "assistente" and n.status == "da_inviare" for n in notes)
    page = client.get("/commercialista/note")
    assert "Da inviare (2)" in page.text and "mailto:studio@example.com" in page.text


def test_send_via_smtp_and_package(client, monkeypatch):
    _setup()
    client.post("/commercialista/note/nuova", data={"title": "IVA Barone", "body": "Si recupera l'IVA?"})
    # pacchetto: le note entrano nel testo dell'email e risultano inviate con il pacchetto
    page = client.get("/pacchetto?mese=2026-09")
    assert "IVA%20Barone" in page.text and "1 note per il commercialista" in page.text
    client.post("/commercialista/note/nuova", data={"title": "Leasing auto", "body": "Deducibilità?"})
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, pwd):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("msg", msg["To"], msg["Subject"], msg.get_content()))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    for k, v in {"FBP_SMTP_HOST": "smtp.example.com", "FBP_SMTP_USER": "me@example.com", "FBP_SMTP_PASSWORD": "x"}.items():
        monkeypatch.setenv(k, v)
    assert "Invia al commercialista" in client.get("/commercialista/note").text
    with SessionLocal() as s:
        ids = [n.id for n in ns.pending(s)]
    r = client.post("/commercialista/note/invia", data={"mode": "smtp", "note_ids": [str(i) for i in ids]}, follow_redirects=False)
    assert "Email+inviata" in r.headers["location"] or "Email%20inviata" in r.headers["location"]
    msg = next(x for x in sent if x[0] == "msg")
    assert msg[1] == "studio@example.com" and "IVA Barone" in msg[3] and "Leasing auto" in msg[3]
    with SessionLocal() as s:
        assert not ns.pending(s) and all(n.sent_at for n in s.query(AccountantNote))


def test_package_marks_notes_sent(client):
    _setup()
    client.post("/commercialista/note/nuova", data={"title": "Domanda", "body": ""})
    client.post("/pacchetto/inviato", data={"mese": "2026-09"})
    with SessionLocal() as s:
        assert s.query(AccountantNote).one().status == "inviata"


def test_validation():
    import pytest

    with pytest.raises(aa.ActionError):
        aa.check_note(None, " ", "x")
