"""Caricamento settimanale: un solo trascinamento con tutti i file, l'app li riconosce e li smista.

- CSV/XLSX/XLS  -> movimenti bancari (conto riconosciuto da mappatura salvata, IBAN o nome file)
- XML/P7M/ZIP con FatturaPA -> fatture (scadenze in entrate/uscite previste)
- PDF, foto, altro -> documenti per il commercialista (tipologia e mese indovinati dal nome)
"""
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import Account, ChecklistItem, DocCategory
from app.services import admin_docs as ad
from app.services.bank_import import ImportProfile, UnreadableFile, import_transactions, new_batch_id, parse_rows, read_table
from app.services.fatturapa import import_invoices
from app.services.util import get_setting

TABLE_EXT = (".csv", ".txt", ".xlsx", ".xlsm", ".xls")
INVOICE_EXT = (".xml", ".p7m")

# parole nel nome del file -> tipologia documento (in ordine di priorità)
CATEGORY_HINTS = [
    ("ec_trimestrali", r"trimestr|e[ _-]?c[ _-]?ufficial|estratto[ _-]?conto[ _-]?(al[ _-]?)?(31[ ._-]?0?3|30[ ._-]?0?6|30[ ._-]?0?9|31[ ._-]?12)"),
    ("ec_carte", r"carta|card|amex|nexi|visa|mastercard|prepagat"),
    ("assegni_emessi", r"assegn\w*[ _-]?emess"),
    ("assegni_ricevuti", r"assegn\w*[ _-]?(ricevut|versat)|versament"),
    ("contanti", r"contant|cassa|prima[ _-]?nota"),
    ("fatture_estere", r"invoice|receipt|google|aws|amazon[ _-]?web|meta|facebook|stripe|apple|microsoft|adobe|openai|anthropic|notion|slack|zoom|linkedin|canva|shopify|github|atlassian|dropbox|figma"),
    ("spese_varie", r"autostrad|telepass|pedagg|ristorant|bar|scontrin|ricevut|assicuraz|carburant|benzin|parcheggi|taxi|treno|trenitalia|italo|hotel|albergo"),
    ("movimenti_banche", r"moviment|estratto|e[ _-]?c\b|statement"),
]


@dataclass
class UploadItem:
    token: str
    name: str
    kind: str  # banca | fattura | documento
    account_id: int = 0
    category: str = ""
    period: str = ""  # YYYY-MM
    needs_mapping: bool = False
    note: str = ""


@dataclass
class Batch:
    id: str
    created_at: str
    items: list[UploadItem] = field(default_factory=list)


def _batch_path(batch_id: str) -> Path:
    return config.IMPORTS_DIR / f"batch-{Path(batch_id).name}.json"


def save_batch(batch: Batch) -> None:
    _batch_path(batch.id).write_text(json.dumps(asdict(batch)))


def load_batch(batch_id: str) -> Batch | None:
    p = _batch_path(batch_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return Batch(id=data["id"], created_at=data["created_at"], items=[UploadItem(**i) for i in data["items"]])


def file_path(item: UploadItem) -> Path:
    return config.IMPORTS_DIR / Path(item.token).name


# ---------- riconoscimento ----------

def is_invoice(name: str, content: bytes) -> bool:
    n = name.lower()
    if n.endswith(INVOICE_EXT):
        return b"FatturaElettronica" in content or n.endswith(".p7m")
    if n.endswith(".zip"):
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                return any(f.lower().endswith(INVOICE_EXT) for f in zf.namelist())
        except zipfile.BadZipFile:
            return False
    return False


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_account(rows: list[list[str]], filename: str, accounts: list[Account]) -> tuple[Account | None, bool]:
    """Trova il conto di un estratto movimenti. Restituisce (conto, serve_mappatura)."""
    text = " ".join(c for r in rows[:40] for c in r).upper().replace(" ", "")
    fname = _norm(filename)
    by_iban = [a for a in accounts if a.iban and len(a.iban) >= 8 and a.iban in text]
    by_profile = []
    for a in accounts:
        p = ImportProfile.from_json(a.csv_profile)
        if not p or p.header_row >= len(rows):
            continue
        header = set(rows[p.header_row])
        needed = [p.date_col, *p.desc_cols] + ([p.in_col, p.out_col] if p.amount_mode == "split" else [p.amount_col])
        if all(c in header for c in needed if c):
            by_profile.append(a)

    def by_name(cands: list[Account]) -> list[Account]:
        hits = [a for a in cands if _norm(a.name) and _norm(a.name) in fname]
        return hits or [a for a in cands if _norm(a.bank) and _norm(a.bank) in fname]

    # L'IBAN nel file o il nome del file identificano il conto meglio del solo formato delle colonne
    strong = by_iban if len(by_iban) == 1 else by_name(accounts)
    if len(strong) == 1:
        return strong[0], strong[0] not in by_profile
    if len(by_profile) == 1:
        return by_profile[0], False
    if by_profile:
        narrowed = [a for a in by_profile if a in by_iban] or by_name(by_profile)
        if len(narrowed) == 1:
            return narrowed[0], False
        return None, False  # più conti con lo stesso formato: lo sceglie l'utente
    return None, True


def guess_category(filename: str) -> str:
    n = filename.lower()
    for code, pattern in CATEGORY_HINTS:
        if re.search(pattern, n):
            return code
    return "spese_varie"


def guess_period(filename: str, default: str) -> str:
    n = filename
    patterns = [
        (r"(20\d\d)[-_. ]?(0[1-9]|1[0-2])[-_. ]?(?:[0-3]\d)?(?!\d)", lambda m: (m.group(1), m.group(2))),
        (r"(?<!\d)(?:[0-3]\d)[-_. ](0[1-9]|1[0-2])[-_. ](20\d\d)", lambda m: (m.group(2), m.group(1))),
        (r"(?<!\d)(0[1-9]|1[0-2])[-_. ](20\d\d)", lambda m: (m.group(2), m.group(1))),
    ]
    for pat, get in patterns:
        m = re.search(pat, n)
        if m:
            y, mth = get(m)
            return f"{y}-{mth}"
    return default


def default_period(today: date | None = None) -> str:
    today = today or date.today()
    # nei primi giorni del mese si caricano di solito i documenti del mese precedente
    if today.day <= 7:
        from app.services.util import add_months

        prev = add_months(today.replace(day=1), -1)
        return f"{prev.year}-{prev.month:02d}"
    return f"{today.year}-{today.month:02d}"


def create_batch(session: Session, files: list[tuple[str, bytes]], today: date | None = None) -> Batch:
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    period0 = default_period(today)
    batch = Batch(id=uuid.uuid4().hex[:12], created_at=datetime.now().isoformat(timespec="seconds"))
    config.IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in files:
        if not content:
            continue
        suffix = Path(name).suffix.lower() or ".bin"
        token = f"{uuid.uuid4().hex}{suffix}"
        (config.IMPORTS_DIR / token).write_bytes(content)
        item = UploadItem(token=token, name=name, kind="documento", period=guess_period(name, period0))
        if is_invoice(name, content):
            item.kind = "fattura"
        elif name.lower().endswith(TABLE_EXT):
            try:
                rows = read_table(content, name)
            except Exception as exc:  # file illeggibile: lo tratto come documento
                item.note = f"Non leggo il file come tabella ({exc}): lo archivio come documento"
                item.category = guess_category(name)
            else:
                item.kind = "banca"
                acc, needs_mapping = match_account(rows, name, accounts)
                item.account_id = acc.id if acc else 0
                item.needs_mapping = needs_mapping
        if item.kind == "documento" and not item.category:
            item.category = guess_category(name)
        batch.items.append(item)
    save_batch(batch)
    return batch


# ---------- elaborazione ----------

def checklist_item_for(session: Session, code: str, period: str) -> ChecklistItem | None:
    cat = session.scalar(select(DocCategory).where(DocCategory.code == code))
    if cat is None:
        return None
    y, m = (int(x) for x in period.split("-"))
    key = f"{y}-Q{(m - 1) // 3 + 1}" if cat.frequency == "trimestrale" else f"{y}-{m:02d}"
    item = session.scalar(select(ChecklistItem).where(ChecklistItem.category_id == cat.id, ChecklistItem.period_key == key))
    if item is None:
        _, end = ad.period_range(key)
        item = ChecklistItem(category=cat, period_key=key, period_end=end, due_date=ad.due_date_for(cat, end))
        session.add(item)
        session.flush()
    return item


@dataclass
class BatchResult:
    movements: int = 0
    invoices: int = 0
    documents: int = 0
    matched: int = 0
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending: list[UploadItem] = field(default_factory=list)  # estratti da mappare a mano


def process_batch(session: Session, batch: Batch) -> BatchResult:
    from app.services import reconcile

    res = BatchResult()
    invoice_files = []
    for item in batch.items:
        path = file_path(item)
        if item.kind == "ignora" or not path.exists():
            continue
        content = path.read_bytes()
        if item.kind == "fattura":
            invoice_files.append((item.name, content))
            path.unlink(missing_ok=True)
        elif item.kind == "banca":
            account = session.get(Account, item.account_id) if item.account_id else None
            profile = ImportProfile.from_json(account.csv_profile) if account else None
            if account is None or profile is None:
                res.pending.append(item)  # il file resta: si completa dalla schermata di mappatura
                continue
            try:
                rows = read_table(content, item.name)
            except UnreadableFile as exc:
                res.errors.append(f"{item.name}: {exc}")
                continue
            txs, errors = parse_rows(rows, profile)
            if not txs:
                res.errors.append(f"{item.name}: nessun movimento riconosciuto con la mappatura di '{account.name}'")
                res.pending.append(item)
                continue
            r = import_transactions(session, account, txs, new_batch_id(), update_balance=bool(profile.balance_col))
            res.movements += r.inserted
            res.lines.append(f"{account.name}: {r.inserted} nuovi movimenti" + (f", {r.skipped} già presenti" if r.skipped else ""))
            if errors:
                res.errors.append(f"{item.name}: {len(errors)} righe non lette")
            path.unlink(missing_ok=True)
        else:
            target = checklist_item_for(session, item.category or "spese_varie", item.period)
            if target is None:
                res.errors.append(f"{item.name}: tipologia sconosciuta")
                continue
            ad.store_document(session, target, item.name, content)
            res.documents += 1
            path.unlink(missing_ok=True)
    session.commit()
    if invoice_files:
        inv = import_invoices(
            session, invoice_files, company_vat=get_setting(session, "company_vat"),
            default_payment_days=int(get_setting(session, "default_payment_days") or 30),
        )
        res.invoices = len(inv.imported)
        if inv.duplicates:
            res.lines.append(f"{len(inv.duplicates)} fatture già importate, saltate")
        res.errors += inv.errors
    # abbinamenti certi tra movimenti appena caricati e voci previste
    tol = int(get_setting(session, "match_tolerance_days") or 7)
    for s in reconcile.suggest_matches(session, tol):
        if s.certain:
            reconcile.match(session, s.transaction, s.item)
            res.matched += 1
    from app.services.util import set_setting

    set_setting(session, "last_upload_at", datetime.now().isoformat(timespec="seconds"))
    session.commit()
    batch.items = res.pending
    if batch.items:
        save_batch(batch)
    else:
        _batch_path(batch.id).unlink(missing_ok=True)
    return res
