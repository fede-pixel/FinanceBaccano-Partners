"""Percorsi e impostazioni di avvio.

I dati vivono fuori dal codice, così aggiornare l'app non li tocca.
Per default: ~/FinanceBaccano (sovrascrivibile con la variabile FBP_DATA_DIR).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FBP_DATA_DIR", Path.home() / "FinanceBaccano")).expanduser()
DB_PATH = DATA_DIR / "finance.db"
BACKUP_DIR = DATA_DIR / "backup"
DEFAULT_DOCS_DIR = DATA_DIR / "documenti"
DEFAULT_PACKAGES_DIR = DATA_DIR / "pacchetti"
IMPORTS_DIR = DATA_DIR / "import"

HOST = os.environ.get("FBP_HOST", "127.0.0.1")
PORT = int(os.environ.get("FBP_PORT", "8765"))
BACKUPS_TO_KEEP = 30

# Modalità online (es. su Render): niente cartelle locali da aprire, accesso con password obbligatorio
ONLINE = os.environ.get("FBP_ONLINE", "") == "1"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")


def ensure_dirs() -> None:
    for d in (DATA_DIR, BACKUP_DIR, DEFAULT_DOCS_DIR, DEFAULT_PACKAGES_DIR, IMPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
