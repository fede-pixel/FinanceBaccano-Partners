"""Import dei movimenti da file esportati dall'home banking (CSV, XLSX, XLS).

Ogni banca esporta un formato diverso: la mappatura delle colonne viene indovinata e
poi salvata sul conto, così dal secondo import in poi basta trascinare il file."""
import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, BalanceSnapshot, Transaction
from app.services.util import parse_amount

DATE_FORMATS = [
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
]


@dataclass
class ImportProfile:
    header_row: int = 0
    date_col: str = ""
    date_format: str = "auto"
    amount_mode: str = "single"  # single | split
    amount_col: str = ""
    in_col: str = ""
    out_col: str = ""
    desc_cols: list[str] = field(default_factory=list)
    counterparty_col: str = ""
    id_col: str = ""
    balance_col: str = ""
    status_col: str = ""
    status_ok: str = ""  # es. COMPLETED (Revolut): righe con altro stato vengono saltate
    decimal_sep: str = "auto"
    invert_sign: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "ImportProfile | None":
        if not raw:
            return None
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ParsedTx:
    date: date
    amount_cents: int
    description: str
    counterparty: str
    external_id: str
    balance_cents: int | None
    row_index: int


# ---------- lettura file ----------

def _cell_to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S") if (v.hour or v.minute or v.second) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float):
        return repr(round(v, 2)) if v != int(v) else str(int(v))
    return str(v).strip()


def read_table(content: bytes, filename: str) -> list[list[str]]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [[_cell_to_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
    if name.endswith(".xls"):
        import xlrd

        book = xlrd.open_workbook(file_contents=content)
        sh = book.sheet_by_index(0)
        rows = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(_cell_to_str(xlrd.xldate_as_datetime(cell.value, book.datemode)))
                else:
                    row.append(_cell_to_str(cell.value))
            rows.append(row)
        return rows
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = "\n".join(text.splitlines()[:30])
    counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
    delimiter = max(counts, key=counts.get)
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


# ---------- euristiche ----------

_KEYWORDS = {
    "date_col": ["data contabile", "data operazione", "date completed", "completed date", "data valuta", "data", "date"],
    "amount_col": ["importo", "total amount", "amount", "importo (eur)", "importo eur"],
    "in_col": ["avere", "entrate", "accrediti", "money in", "credit"],
    "out_col": ["dare", "uscite", "addebiti", "money out", "debit"],
    "desc_cols": ["descrizione", "causale", "description", "reference", "dettagli", "descrizione operazione"],
    "counterparty_col": ["controparte", "beneficiario", "payer", "payee", "merchant", "ordinante", "counterparty"],
    "id_col": ["id", "transaction id", "id transazione"],
    "balance_col": ["saldo", "balance"],
    "status_col": ["state", "stato", "status"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def detect_header_row(rows: list[list[str]]) -> int:
    width = max((sum(1 for c in r if c) for r in rows[:40]), default=0)
    for i, row in enumerate(rows[:40]):
        filled = [c for c in row if c]
        if len(filled) >= max(2, int(width * 0.6)) and any(re.search(r"\bdata\b|\bdate\b", _norm(c)) for c in filled):
            return i
    for i, row in enumerate(rows[:40]):
        if sum(1 for c in row if c) == width:
            return i
    return 0


def _find(header: list[str], keys: list[str], exclude: set[str] = frozenset()) -> str:
    normed = [(_norm(h), h) for h in header if h and h not in exclude]
    for k in keys:  # prima corrispondenza esatta, nell'ordine di priorità
        for n, h in normed:
            if n == k:
                return h
    for k in keys:
        for n, h in normed:
            if k in n and len(k) > 2:
                return h
    return ""


def guess_profile(rows: list[list[str]]) -> ImportProfile:
    hr = detect_header_row(rows)
    header = rows[hr] if rows else []
    p = ImportProfile(header_row=hr)
    p.date_col = _find(header, _KEYWORDS["date_col"])
    used = {p.date_col}
    p.in_col = _find(header, _KEYWORDS["in_col"], used)
    p.out_col = _find(header, _KEYWORDS["out_col"], used | {p.in_col})
    p.amount_col = _find(header, _KEYWORDS["amount_col"], used)
    if p.in_col and p.out_col and not p.amount_col:
        p.amount_mode = "split"
    used |= {p.amount_col, p.in_col, p.out_col}
    desc = _find(header, _KEYWORDS["desc_cols"], used)
    p.desc_cols = [desc] if desc else []
    used |= set(p.desc_cols)
    p.counterparty_col = _find(header, _KEYWORDS["counterparty_col"], used)
    p.id_col = next((h for h in header if _norm(h) in _KEYWORDS["id_col"]), "")
    p.balance_col = _find(header, _KEYWORDS["balance_col"], used)
    p.status_col = next((h for h in header if _norm(h) in _KEYWORDS["status_col"]), "")
    if p.status_col:
        values = {r[header.index(p.status_col)].upper() for r in rows[hr + 1 : hr + 200] if len(r) > header.index(p.status_col)}
        p.status_ok = "COMPLETED" if "COMPLETED" in values else ""
    return p


def parse_date_value(value: str, fmt: str = "auto") -> date | None:
    value = value.strip()
    if not value:
        return None
    formats = DATE_FORMATS if fmt == "auto" else [fmt]
    for f in formats:
        try:
            return datetime.strptime(value, f).date()
        except ValueError:
            continue
    # Excel a volte esporta il numero seriale
    if fmt == "auto" and re.fullmatch(r"\d{5}(\.\d+)?", value):
        return date(1899, 12, 30) + timedelta(days=int(float(value)))
    return None


def parse_rows(rows: list[list[str]], p: ImportProfile) -> tuple[list[ParsedTx], list[str]]:
    header = rows[p.header_row] if rows else []
    idx = {h: i for i, h in enumerate(header) if h}

    def get(row: list[str], col: str) -> str:
        i = idx.get(col)
        return row[i] if i is not None and i < len(row) else ""

    dec = None if p.decimal_sep == "auto" else p.decimal_sep
    out: list[ParsedTx] = []
    errors: list[str] = []
    for n, row in enumerate(rows[p.header_row + 1 :], start=p.header_row + 2):
        if not any(row):
            continue
        if p.status_col and p.status_ok and get(row, p.status_col).upper() != p.status_ok.upper():
            continue
        raw_date = get(row, p.date_col)
        d = parse_date_value(raw_date, p.date_format)
        if d is None:
            # righe di totale/piè di pagina senza importo: le salto senza segnalarle
            amount_cols = (p.in_col, p.out_col) if p.amount_mode == "split" else (p.amount_col,)
            if any(parse_amount(get(row, c), dec) for c in amount_cols if c):
                errors.append(f"Riga {n}: data non riconosciuta '{raw_date}'")
            continue
        if p.amount_mode == "split":
            a_in = parse_amount(get(row, p.in_col), dec) or 0
            a_out = parse_amount(get(row, p.out_col), dec) or 0
            amount = abs(a_in) - abs(a_out)
        else:
            amount = parse_amount(get(row, p.amount_col), dec)
            if amount is None:
                errors.append(f"Riga {n}: importo non riconosciuto")
                continue
        if p.invert_sign:
            amount = -amount
        desc = " · ".join(v for v in (get(row, c) for c in p.desc_cols) if v)
        bal = parse_amount(get(row, p.balance_col), dec) if p.balance_col else None
        out.append(
            ParsedTx(
                date=d,
                amount_cents=amount,
                description=desc,
                counterparty=get(row, p.counterparty_col) if p.counterparty_col else "",
                external_id=get(row, p.id_col) if p.id_col else "",
                balance_cents=bal,
                row_index=n,
            )
        )
    return out, errors


def dedupe_keys(txs: list[ParsedTx]) -> list[str]:
    """Chiave stabile per riconoscere un movimento già importato anche se il file viene ricaricato."""
    seen: dict[str, int] = {}
    keys = []
    for t in txs:
        if t.external_id:
            base = f"id|{t.external_id}"
        else:
            base = f"{t.date.isoformat()}|{t.amount_cents}|{_norm(t.description)}"
            occ = seen.get(base, 0)
            seen[base] = occ + 1
            base = f"{base}|{occ}"
        keys.append(hashlib.sha256(base.encode()).hexdigest()[:40])
    return keys


@dataclass
class ImportResult:
    inserted: int
    skipped: int
    snapshot: tuple[date, int] | None


def closing_balance(txs: list[ParsedTx]) -> tuple[date, int] | None:
    """Saldo a fine dell'ultimo giorno, se il file ha una colonna saldo."""
    with_bal = [t for t in txs if t.balance_cents is not None]
    if not with_bal:
        return None
    last_day = max(t.date for t in with_bal)
    same_day = [t for t in with_bal if t.date == last_day]
    # Ordine del file: se è decrescente (più recenti in alto) l'ultimo movimento del giorno è il primo
    descending = with_bal[0].date > with_bal[-1].date
    pick = min(same_day, key=lambda t: t.row_index) if descending else max(same_day, key=lambda t: t.row_index)
    return last_day, pick.balance_cents


def import_transactions(
    session: Session, account: Account, txs: list[ParsedTx], batch: str, update_balance: bool = True
) -> ImportResult:
    keys = dedupe_keys(txs)
    existing = set(
        session.scalars(select(Transaction.dedupe_key).where(Transaction.account_id == account.id, Transaction.dedupe_key.in_(keys)))
    )
    inserted = 0
    for t, k in zip(txs, keys):
        if k in existing:
            continue
        existing.add(k)
        session.add(
            Transaction(
                account_id=account.id,
                date=t.date,
                amount_cents=t.amount_cents,
                description=t.description,
                counterparty=t.counterparty,
                source="import",
                dedupe_key=k,
                import_batch=batch,
            )
        )
        inserted += 1
    snap = closing_balance(txs) if update_balance else None
    if snap:
        row = session.scalar(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == snap[0]))
        if row:
            row.balance_cents, row.source = snap[1], "import"
        else:
            session.add(BalanceSnapshot(account_id=account.id, date=snap[0], balance_cents=snap[1], source="import"))
    session.commit()
    return ImportResult(inserted=inserted, skipped=len(txs) - inserted, snapshot=snap)
