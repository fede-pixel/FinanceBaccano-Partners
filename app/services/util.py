"""Utility condivise: importi, date italiane, impostazioni."""
import calendar
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models import Setting

MONTHS_IT = [
    "", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]
WEEKDAYS_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def parse_amount(value: str | float | int | None, decimal_sep: str | None = None) -> int | None:
    """Converte un importo in centesimi. Accetta '1.234,56', '1,234.56', '-12,5', '€ 3', '(45,00)'.
    Con `decimal_sep` ("," o ".") il separatore decimale è esplicito e non viene indovinato."""
    if value is None:
        return None
    if getattr(value, "is_number", False):  # cella numerica di Excel: già un numero esatto
        value = float(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    s = str(value).strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2012", "-")  # meno tipografici dei PDF
    s = re.sub(r"[€$£\s\u00a0\u202f]|EUR|USD", "", s, flags=re.IGNORECASE)
    if s.endswith("-"):
        negative, s = not negative, s[:-1]
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    if not s:
        return None
    last_comma, last_dot = s.rfind(","), s.rfind(".")
    if decimal_sep == ",":
        s = s.replace(".", "").replace(",", ".")
    elif decimal_sep == ".":
        s = s.replace(",", "")
    elif last_comma > last_dot:
        # formato italiano: il punto è separatore delle migliaia
        s = s.replace(".", "").replace(",", ".")
    elif last_dot > last_comma and last_comma != -1:
        s = s.replace(",", "")
    elif last_dot != -1 and last_comma == -1 and s.count(".") > 1:
        s = s.replace(".", "")
    elif last_dot != -1 and last_comma == -1 and len(s) - last_dot - 1 == 3 and s[:last_dot].isdigit() and len(s[:last_dot]) <= 3 and s[:last_dot] != "0":
        # "1.234" ambiguo: in Italia è quasi sempre migliaia
        s = s.replace(".", "")
    try:
        cents = int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    return -cents if negative else cents


def fmt_eur(cents: int | None, sign: bool = False) -> str:
    if cents is None:
        return ""
    neg = cents < 0
    whole, frac = divmod(abs(cents), 100)
    s = f"{whole:,}".replace(",", ".") + f",{frac:02d}"
    prefix = "-" if neg else ("+" if sign and cents > 0 else "")
    return f"{prefix}{s} €"


def fmt_date(d: date | datetime | None) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def month_label(year: int, month: int) -> str:
    return f"{MONTHS_IT[month].capitalize()} {year}"


def parse_month(value: str | None) -> tuple[int, int] | None:
    """'2026-09' -> (2026, 9); None se il valore non è un mese valido."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", (value or "").strip())
    if not m:
        return None
    y, mth = int(m.group(1)), int(m.group(2))
    return (y, mth) if 2000 <= y <= 2100 and 1 <= mth <= 12 else None


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def add_months(d: date, months: int, day: int | None = None) -> date:
    total = d.year * 12 + (d.month - 1) + months
    y, m = divmod(total, 12)
    m += 1
    target_day = day if day is not None else d.day
    return date(y, m, min(target_day, calendar.monthrange(y, m)[1]))


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


# --- Festività italiane (per spostare le scadenze al giorno lavorativo) ---

def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def italian_holidays(year: int) -> set[date]:
    fixed = [(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)]
    hol = {date(year, m, d) for m, d in fixed}
    hol.add(_easter(year) + timedelta(days=1))  # Pasquetta
    return hol


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in italian_holidays(d.year)


def adjust_business_day(d: date, rule: str) -> date:
    if rule == "successivo":
        while not is_business_day(d):
            d += timedelta(days=1)
    elif rule == "precedente":
        while not is_business_day(d):
            d -= timedelta(days=1)
    return d


# --- Impostazioni chiave/valore ---

SETTING_DEFAULTS = {
    "company_name": "Baccano Partners",
    "company_vat": "",
    "accountant_email": "",
    "docs_dir": "",
    "packages_dir": "",
    "low_balance_threshold": "500000",  # centesimi
    "default_payment_days": "30",
    # le voci scadute non abbinate di solito sono già pagate (il saldo della banca le comprende): non si tolgono dal previsto
    "include_overdue_in_forecast": "0",
    "reminder_days": "5",
    "match_tolerance_days": "7",
    "eb_application_id": "",
    "eb_key_path": "",
    "eb_redirect_url": "",
    "eb_sync_hours": "6",
    "eb_initial_days": "90",
}


def get_setting(session: Session, key: str) -> str:
    row = session.get(Setting, key)
    return row.value if row else SETTING_DEFAULTS.get(key, "")


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
