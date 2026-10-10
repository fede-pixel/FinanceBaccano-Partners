"""Client per l'API di Enable Banking (Open Banking PSD2, sola lettura: saldi e movimenti).

Flusso:
1. `start_auth`: chiede a Enable Banking l'URL di login della banca; l'utente autorizza nella banca.
2. la banca rimanda a `redirect_url?code=...&state=...`
3. `create_session(code)`: restituisce session_id e la lista dei conti autorizzati.
4. `balances(uid)` / `transactions(uid, date_from)`: lettura dati, fino alla scadenza del consenso.

L'autenticazione verso Enable Banking è un JWT RS256 firmato con la chiave privata
dell'applicazione registrata nel Control Panel (https://enablebanking.com/cp/applications).
"""
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt

API_URL = "https://api.enablebanking.com"


class EnableBankingError(Exception):
    pass


@dataclass
class EBConfig:
    application_id: str
    private_key: str  # PEM

    @classmethod
    def from_files(cls, application_id: str, key_path: str | Path) -> "EBConfig":
        path = Path(key_path).expanduser()
        if not application_id or not path.is_file():
            raise EnableBankingError("Configura Application ID e chiave privata in Banche collegate → Configurazione")
        return cls(application_id=application_id.strip(), private_key=path.read_text())


class EnableBankingClient:
    def __init__(self, cfg: EBConfig, transport: httpx.BaseTransport | None = None, base_url: str = API_URL):
        self.cfg = cfg
        self.http = httpx.Client(base_url=base_url, timeout=30, transport=transport)

    def _token(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
            self.cfg.private_key,
            algorithm="RS256",
            headers={"kid": self.cfg.application_id},
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            r = self.http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise EnableBankingError(f"Connessione a Enable Banking non riuscita: {exc}") from exc
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("message") or body.get("detail") or body.get("error") or r.text
            except ValueError:
                detail = r.text
            raise EnableBankingError(f"Enable Banking {r.status_code}: {detail}")
        return r.json() if r.content else {}

    # --- API ---

    def application(self) -> dict:
        return self._request("GET", "/application")

    def aspsps(self, country: str = "IT", psu_type: str = "business") -> list[dict]:
        data = self._request("GET", "/aspsps", params={"country": country, "psu_type": psu_type})
        return data.get("aspsps", [])

    def start_auth(
        self, aspsp_name: str, country: str, redirect_url: str, state: str, psu_type: str = "business", days: int = 180
    ) -> dict:
        valid_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        return self._request(
            "POST",
            "/auth",
            json={
                "access": {"valid_until": valid_until},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": psu_type,
            },
        )

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    def balances(self, account_uid: str) -> list[dict]:
        return self._request("GET", f"/accounts/{account_uid}/balances").get("balances", [])

    def transactions(self, account_uid: str, date_from: date, date_to: date | None = None) -> list[dict]:
        params = {"date_from": date_from.isoformat()}
        if date_to:
            params["date_to"] = date_to.isoformat()
        out: list[dict] = []
        for _ in range(100):  # le banche paginano con continuation_key
            data = self._request("GET", f"/accounts/{account_uid}/transactions", params=params)
            out.extend(data.get("transactions", []))
            key = data.get("continuation_key")
            if not key:
                break
            params["continuation_key"] = key
        return out


# ---------- conversione dei dati ----------

# Preferenza: saldo contabile (coerente con i movimenti contabilizzati), poi gli altri
BALANCE_PREFERENCE = ["CLBD", "ITBD", "XPCD", "ITAV", "CLAV", "OPBD", "PRCD", "OTHR"]


def pick_balance(balances: list[dict]) -> tuple[date, int] | None:
    from app.services.util import parse_amount

    if not balances:
        return None
    ranked = sorted(
        balances,
        key=lambda b: BALANCE_PREFERENCE.index(b.get("balance_type")) if b.get("balance_type") in BALANCE_PREFERENCE else 99,
    )
    b = ranked[0]
    amount = parse_amount(str(b.get("balance_amount", {}).get("amount", "")), ".")
    if amount is None:
        return None
    ref = b.get("reference_date") or b.get("last_change_date_time") or ""
    try:
        d = date.fromisoformat(ref[:10]) if ref else date.today()
    except ValueError:
        d = date.today()
    return min(d, date.today()), amount


def account_label(acc: dict) -> str:
    ident = acc.get("account_id") or {}
    iban = ident.get("iban") or (ident.get("other") or {}).get("identification") or ""
    name = acc.get("name") or acc.get("details") or acc.get("product") or "Conto"
    return f"{name} {iban}".strip()


def account_iban(acc: dict) -> str:
    ident = acc.get("account_id") or {}
    return (ident.get("iban") or "").replace(" ", "").upper()
