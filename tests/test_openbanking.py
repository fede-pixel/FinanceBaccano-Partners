"""Open Banking con Enable Banking simulato (nessuna chiamata di rete)."""
import json
import re
from datetime import date, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.models import Account, BalanceSnapshot, BankConnection, Transaction
from app.services import bank_sync
from app.services.enablebanking import EBConfig, EnableBankingClient, pick_balance

TODAY = date.today()


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem.decode(), key.public_key()


class FakeEB:
    """Simula le risposte di api.enablebanking.com e verifica il JWT."""

    def __init__(self, public_key):
        self.public_key = public_key
        self.calls = []
        self.auth_body = None
        self.txs = [
            {"entry_reference": "r1", "transaction_amount": {"amount": "120.50", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
             "status": "BOOK", "booking_date": (TODAY - timedelta(days=3)).isoformat(), "remittance_information": ["Fattura 991"],
             "creditor": {"name": "Fornitore Gamma"}},
            {"entry_reference": "r2", "transaction_amount": {"amount": "1000.00", "currency": "EUR"}, "credit_debit_indicator": "CRDT",
             "status": "BOOK", "booking_date": (TODAY - timedelta(days=1)).isoformat(), "remittance_information": ["Saldo fatt 45"],
             "debtor": {"name": "Beta Srl"}},
            {"entry_reference": "p1", "transaction_amount": {"amount": "9.99", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
             "status": "PDNG", "transaction_date": TODAY.isoformat()},
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].split()[1]
        assert jwt.get_unverified_header(token)["kid"] == "app-123"
        jwt.decode(token, self.public_key, algorithms=["RS256"], audience="api.enablebanking.com")
        path = request.url.path
        self.calls.append((request.method, path, dict(request.url.params)))
        if path == "/application":
            return httpx.Response(200, json={"name": "Baccano", "environment": "PRODUCTION"})
        if path == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": "Revolut", "country": "IT", "maximum_consent_validity": 90 * 86400}]})
        if path == "/auth":
            self.auth_body = json.loads(request.content)
            return httpx.Response(200, json={"url": "https://bank.example/login?x=1", "authorization_id": "a1"})
        if path == "/sessions" and request.method == "POST":
            assert json.loads(request.content) == {"code": "CODE42"}
            return httpx.Response(200, json={
                "session_id": "sess-1",
                "access": {"valid_until": "2027-01-08T10:00:00+00:00"},
                "accounts": [
                    {"uid": "uid-main", "account_id": {"iban": "LT12 3456 7890 1234 5678"}, "name": "Main EUR", "currency": "EUR", "cash_account_type": "CACC"},
                    {"uid": "uid-card", "account_id": {}, "name": "Carta", "currency": "EUR", "cash_account_type": "CARD"},
                ],
            })
        if path.endswith("/balances"):
            return httpx.Response(200, json={"balances": [
                {"balance_type": "ITAV", "balance_amount": {"amount": "999.00", "currency": "EUR"}},
                {"balance_type": "CLBD", "balance_amount": {"amount": "5879.50", "currency": "EUR"}, "reference_date": (TODAY - timedelta(days=1)).isoformat()},
            ]})
        if path.endswith("/transactions"):
            params = dict(request.url.params)
            if "continuation_key" not in params:
                return httpx.Response(200, json={"transactions": self.txs[:1], "continuation_key": "next"})
            return httpx.Response(200, json={"transactions": self.txs[1:]})
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture()
def fake(keypair, monkeypatch, session):
    pem, pub = keypair
    fake = FakeEB(pub)
    transport = httpx.MockTransport(fake.handler)
    original = bank_sync.make_client
    monkeypatch.setattr(bank_sync, "make_client", lambda s, transport_=None: original(s, transport=transport))
    return fake, pem


def test_pick_balance_prefers_booked():
    d, cents = pick_balance([
        {"balance_type": "ITAV", "balance_amount": {"amount": "10.00"}},
        {"balance_type": "CLBD", "balance_amount": {"amount": "-20.10"}, "reference_date": "2026-01-01"},
    ])
    assert d == date(2026, 1, 1) and cents == -2010


def test_to_parsed_skips_pending_and_signs():
    t = bank_sync.to_parsed({"transaction_amount": {"amount": "5"}, "credit_debit_indicator": "DBIT", "booking_date": "2026-09-01",
                             "remittance_information": ["A", "B"], "creditor": {"name": "X"}}, 0)
    assert t.amount_cents == -500 and t.description == "A B" and t.counterparty == "X"
    assert bank_sync.to_parsed({"status": "PDNG", "transaction_amount": {"amount": "5"}, "booking_date": "2026-09-01"}, 0) is None


def test_full_flow_from_ui(client, fake, tmp_path):
    fake_eb, pem = fake
    key = tmp_path / "k.pem"
    key.write_text(pem)
    r = client.post("/banche/configurazione", data={"application_id": "app-123", "sync_hours": "6", "initial_days": "30"},
                    files={"key_file": ("app-123.pem", key.read_bytes(), "application/x-pem-file")})
    assert "Collegamento a Enable Banking riuscito" in r.text

    r = client.get("/banche")
    assert '<option value="Revolut">' in r.text

    # un conto esistente con lo stesso IBAN viene ricollegato in automatico
    client.post("/conti/salva", data={"name": "Revolut Business", "iban": "LT123456789012345678", "include_in_forecast": "1",
                                      "opening_balance": "0", "opening_date": (TODAY - timedelta(days=40)).isoformat()})

    r = client.post("/banche/collega", data={"aspsp": "Revolut", "country": "IT", "psu_type": "business"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://bank.example/login?x=1"
    assert fake_eb.auth_body["psu_type"] == "business"
    assert fake_eb.auth_body["redirect_url"].endswith("/banche/callback")
    state = fake_eb.auth_body["state"]

    r = client.get("/banche/callback?code=CODE42&state=wrong")
    assert "state non valido" in r.text

    r = client.get(f"/banche/callback?code=CODE42&state={state}")
    assert "autorizzata" in r.text
    assert re.search(r'<option value="1" selected>Revolut Business', r.text)  # proposto per IBAN

    r = client.post("/banche/1/conti", data={"map_uid-main": "1", "map_uid-card": "nuovo"})
    assert "Collegati 2 conti" in r.text

    tx_calls = [c for c in fake_eb.calls if c[1].endswith("/transactions")]
    assert tx_calls[0][2]["date_from"] == (TODAY - timedelta(days=30)).isoformat()
    r = client.get("/movimenti?conto=1")
    assert "Fattura 991" in r.text and "Saldo fatt 45" in r.text and "9,99" not in r.text

    r = client.get("/conti")
    assert "5.879,50 €" in r.text  # saldo dal CLBD
    assert "Carta" in r.text

    # seconda sincronizzazione: nessun doppione, riparte con 5 giorni di sovrapposizione
    r = client.post("/banche/1/sincronizza")
    assert "0 nuovi movimenti" in r.text
    last = [c for c in fake_eb.calls if c[1].endswith("/transactions")][-1]
    assert last[2]["date_from"] == (TODAY - timedelta(days=1 + bank_sync.OVERLAP_DAYS)).isoformat()

    r = client.post("/banche/1/elimina")
    assert "Collegamento rimosso" in r.text
    assert any(c[0] == "DELETE" for c in fake_eb.calls)


def test_sync_continues_after_file_import(session, fake, tmp_path):
    fake_eb, pem = fake
    acc = Account(name="BCC", external_uid="uid-main")
    conn = BankConnection(aspsp_name="BCC", session_id="s", status="attivo")
    acc.connection = conn
    session.add_all([acc, conn])
    session.flush()
    session.add(Transaction(account_id=acc.id, date=TODAY - timedelta(days=2), amount_cents=-1, dedupe_key="csv", source="import"))
    session.commit()
    assert bank_sync.sync_start_date(session, acc, TODAY) == TODAY - timedelta(days=1)

    client = EnableBankingClient(EBConfig("app-123", pem), transport=httpx.MockTransport(fake_eb.handler))
    res = bank_sync.sync_connection(session, conn, client)
    assert res.accounts == 1 and res.errors == []
    assert session.query(BalanceSnapshot).filter_by(account_id=acc.id, source="open banking").count() == 1


def test_expired_consent_is_not_called(session):
    from datetime import datetime

    conn = BankConnection(aspsp_name="Vivid", session_id="s", status="attivo", valid_until=datetime(2020, 1, 1))
    session.add(conn)
    session.commit()
    res = bank_sync.sync_connection(session, conn, client=object())
    assert conn.status == "scaduto" and res.errors
