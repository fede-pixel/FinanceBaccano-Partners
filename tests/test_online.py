"""Modalità online: password obbligatoria, sessione firmata, niente cartelle locali."""
import pytest

from app import auth, config


@pytest.fixture()
def online(monkeypatch):
    monkeypatch.setattr(config, "ONLINE", True)
    monkeypatch.setattr(config, "APP_PASSWORD", "segreta-123")
    monkeypatch.setattr(config, "SESSION_SECRET", "x" * 32)
    auth._fails.clear()
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)


def test_online_without_password_refuses(client, monkeypatch):
    monkeypatch.setattr(config, "ONLINE", True)
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    assert client.get("/").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_login_flow(client, online):
    r = client.get("/documenti?anno=2026", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/documenti%3Fanno%3D2026"
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/css/app.css").status_code == 200
    assert client.post("/conti/salva", data={"name": "x"}).status_code == 401

    r = client.post("/login", data={"password": "sbagliata", "next": "/"})
    assert r.status_code == 401 and "Password errata" in r.text
    r = client.post("/login", data={"password": "segreta-123", "next": "/documenti"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/documenti"
    cookie = r.cookies.get(auth.COOKIE)
    assert cookie and auth.valid_token(cookie)
    assert client.get("/").status_code == 200
    assert "Esci" in client.get("/").text
    # niente apertura cartelle sul server
    r = client.post("/apri-cartella", data={"path": str(config.DATA_DIR), "back": "/"})
    assert "Online le cartelle non si aprono" in r.text
    assert "Scarica backup completo" in client.get("/impostazioni").text
    r = client.post("/logout", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_open_redirect_and_forged_tokens(client, online):
    r = client.post("/login", data={"password": "segreta-123", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert not auth.valid_token("9999999999.deadbeef")
    assert not auth.valid_token("1.abc")
    assert not auth.valid_token(None)
    expired = auth.make_token(now=0)
    assert not auth.valid_token(expired)


def test_bruteforce_lockout(client, online):
    for _ in range(auth.MAX_FAILS):
        client.post("/login", data={"password": "no"})
    r = client.post("/login", data={"password": "segreta-123"})
    assert r.status_code == 429


def test_backup_download(client, online):
    import io
    import zipfile

    client.post("/login", data={"password": "segreta-123"})
    r = client.get("/impostazioni/backup.zip")
    assert r.status_code == 200
    assert "finance.db" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
