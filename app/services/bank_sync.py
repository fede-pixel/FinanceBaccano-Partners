"""Sincronizzazione di saldi e movimenti dai conti collegati via Open Banking."""
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.models import Account, BalanceSnapshot, BankConnection, Transaction
from app.services.bank_import import ParsedTx, import_transactions, new_batch_id
from app.services.enablebanking import (
    EBConfig,
    EnableBankingClient,
    EnableBankingError,
    account_iban,
    pick_balance,
)
from app.services.util import get_setting, parse_amount

log = logging.getLogger(__name__)
_lock = threading.Lock()  # una sola sincronizzazione alla volta (manuale o automatica)
OVERLAP_DAYS = 5  # riscarico qualche giorno già visto: le banche contabilizzano in ritardo
KEYS_DIR = config.DATA_DIR / "chiavi"


def make_client(session: Session, transport: httpx.BaseTransport | None = None) -> EnableBankingClient:
    cfg = EBConfig.from_files(get_setting(session, "eb_application_id"), get_setting(session, "eb_key_path"))
    return EnableBankingClient(cfg, transport=transport)


def is_configured(session: Session) -> bool:
    from pathlib import Path

    key = get_setting(session, "eb_key_path")
    return bool(get_setting(session, "eb_application_id") and key and Path(key).expanduser().is_file())


# ---------- conversione movimenti ----------

def to_parsed(tx: dict, n: int) -> ParsedTx | None:
    status = tx.get("status")
    if status and status != "BOOK":
        return None  # i movimenti in attesa (PDNG) arriveranno contabilizzati
    raw_date = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
    if not raw_date:
        return None
    amount = parse_amount(str((tx.get("transaction_amount") or {}).get("amount", "")), ".")
    if amount is None:
        return None
    indicator = tx.get("credit_debit_indicator")
    if indicator == "DBIT":
        amount = -abs(amount)
    elif indicator == "CRDT":
        amount = abs(amount)
    remittance = tx.get("remittance_information") or []
    if isinstance(remittance, str):
        remittance = [remittance]
    description = " ".join(r.strip() for r in remittance if r) or tx.get("note") or ""
    if not description:
        code = tx.get("bank_transaction_code") or {}
        description = code.get("description") or ""
    party = (tx.get("debtor") if amount > 0 else tx.get("creditor")) or {}
    return ParsedTx(
        date=date.fromisoformat(raw_date[:10]),
        amount_cents=amount,
        description=description.strip(),
        counterparty=(party.get("name") or "").strip(),
        external_id=tx.get("entry_reference") or tx.get("transaction_id") or "",
        balance_cents=None,
        row_index=n,
    )


def sync_start_date(session: Session, account: Account, today: date) -> date:
    last_api = session.scalar(
        select(func.max(Transaction.date)).where(Transaction.account_id == account.id, Transaction.source == "api")
    )
    if last_api:
        return last_api - timedelta(days=OVERLAP_DAYS)
    last_any = session.scalar(select(func.max(Transaction.date)).where(Transaction.account_id == account.id))
    if last_any:
        # il conto ha già movimenti importati da file: proseguo dal giorno dopo per non duplicarli
        return last_any + timedelta(days=1)
    days = int(get_setting(session, "eb_initial_days") or 90)
    return today - timedelta(days=days)


@dataclass
class SyncResult:
    accounts: int = 0
    inserted: int = 0
    errors: list[str] = field(default_factory=list)


def sync_account(session: Session, client: EnableBankingClient, account: Account, today: date | None = None) -> int:
    today = today or date.today()
    start = sync_start_date(session, account, today)
    raw = client.transactions(account.external_uid, start) if start <= today else []
    parsed = [p for p in (to_parsed(t, i) for i, t in enumerate(raw)) if p]
    batch = new_batch_id("api-")
    res = import_transactions(session, account, parsed, batch, update_balance=False, source="api")
    bal = pick_balance(client.balances(account.external_uid))
    if bal:
        d, cents = bal
        row = session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == d))
        if row:
            row.balance_cents, row.source = cents, "open banking"
        else:
            session.add(BalanceSnapshot(account_id=account.id, date=d, balance_cents=cents, source="open banking"))
    session.commit()
    return res.inserted


def sync_connection(session: Session, conn: BankConnection, client: EnableBankingClient | None = None) -> SyncResult:
    with _lock:
        return _sync_connection(session, conn, client)


def _sync_connection(session: Session, conn: BankConnection, client: EnableBankingClient | None) -> SyncResult:
    result = SyncResult()
    if conn.valid_until and conn.valid_until < datetime.now():
        conn.status = "scaduto"
        session.commit()
        result.errors.append(f"{conn.aspsp_name}: consenso scaduto, va rinnovato")
        return result
    client = client or make_client(session)
    errors = []
    for acc in conn.accounts:
        if not acc.external_uid or not acc.active:
            continue
        try:
            result.inserted += sync_account(session, client, acc)
            result.accounts += 1
        except EnableBankingError as exc:
            session.rollback()
            errors.append(f"{acc.name}: {exc}")
            if " 401" in str(exc) or " 403" in str(exc):
                conn.status = "scaduto"
    conn.last_sync_at = datetime.now()
    conn.last_error = "\n".join(errors)
    if not errors and conn.status != "scaduto":
        conn.status = "attivo"
    session.commit()
    result.errors = errors
    return result


def sync_all(session: Session, client: EnableBankingClient | None = None) -> SyncResult:
    total = SyncResult()
    if not is_configured(session) and client is None:
        return total
    for conn in session.scalars(select(BankConnection).where(BankConnection.status.in_(["attivo", "errore"]))):
        try:
            r = sync_connection(session, conn, client)
        except EnableBankingError as exc:
            total.errors.append(f"{conn.aspsp_name}: {exc}")
            continue
        total.accounts += r.accounts
        total.inserted += r.inserted
        total.errors += r.errors
    return total


# ---------- collegamento conti dopo l'autorizzazione ----------

def store_session(session: Session, conn: BankConnection, data: dict) -> list[dict]:
    """Salva la sessione e ricollega in automatico (per IBAN) i conti già noti, es. dopo un rinnovo."""
    conn.session_id = data.get("session_id", "")
    accounts = data.get("accounts", [])
    conn.session_accounts = json.dumps(accounts)
    access = data.get("access") or {}
    if access.get("valid_until"):
        conn.valid_until = datetime.fromisoformat(access["valid_until"].replace("Z", "+00:00")).replace(tzinfo=None)
    conn.status = "attivo"
    conn.state = ""
    by_iban = {a.iban: a for a in session.scalars(select(Account).where(Account.iban != ""))}
    for acc in accounts:
        iban = account_iban(acc)
        if iban and iban in by_iban:
            local = by_iban[iban]
            local.connection_id, local.external_uid = conn.id, acc.get("uid", "")
    session.commit()
    return accounts


def session_accounts(conn: BankConnection) -> list[dict]:
    try:
        return json.loads(conn.session_accounts) if conn.session_accounts else []
    except ValueError:
        return []
