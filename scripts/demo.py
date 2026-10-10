"""Riempie un archivio di prova con dati finti, per esplorare l'app senza toccare i dati veri.

Uso:  FBP_DATA_DIR=~/FinanceBaccano-demo python scripts/demo.py && FBP_DATA_DIR=~/FinanceBaccano-demo python run.py
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import bootstrap  # noqa: E402
from app.models import Account, BalanceSnapshot, PlannedItem, Recurrence, Transaction  # noqa: E402
from app.services.recurrence import sync_all  # noqa: E402
from app.services.util import set_setting  # noqa: E402

if config.DATA_DIR == Path.home() / "FinanceBaccano":
    sys.exit("Imposta FBP_DATA_DIR su una cartella di prova: non scrivo dati finti nell'archivio reale.")

bootstrap()
random.seed(7)
today = date.today()
start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
with SessionLocal() as s:
    set_setting(s, "company_vat", "IT01234567890")
    set_setting(s, "low_balance_threshold", "1000000")
    accs = [
        Account(name="Revolut Business", bank="Revolut", kind="conto", color="#2a78d6"),
        Account(name="Vivid Business", bank="Vivid", kind="conto", color="#1baf7a"),
        Account(name="BCC conto ordinario", bank="BCC", kind="conto", color="#6250d6"),
        Account(name="Carta Revolut", bank="Revolut", kind="carta", color="#eda100", include_in_forecast=False),
        Account(name="Cassa contanti", bank="", kind="cassa", color="#898781"),
    ]
    s.add_all(accs)
    s.flush()
    for a, bal in zip(accs, (2_500_000, 900_000, 1_800_000, 0, 30_000)):
        s.add(BalanceSnapshot(account_id=a.id, date=start - timedelta(days=1), balance_cents=bal))
    d, n = start, 0
    while d <= today:
        for _ in range(random.randint(0, 3)):
            a = random.choice(accs[:3])
            amt = random.choice([-1, -1, -1, 1]) * random.randint(2_000, 180_000)
            s.add(Transaction(account_id=a.id, date=d, amount_cents=amt, description=random.choice(
                ["Bonifico cliente", "Fornitore servizi", "Google Workspace", "Amazon Web Services", "Utenze", "Consulenza"]),
                dedupe_key=f"demo{n}"))
            n += 1
        d += timedelta(days=1)
    s.add_all([
        Recurrence(title="Stipendi", amount_cents=-1_400_000, frequency="mensile", day_of_month=27, start_date=start,
                   account_id=accs[2].id, category="Personale", business_day_rule="precedente"),
        Recurrence(title="F24 contributi e ritenute", amount_cents=-520_000, frequency="mensile", day_of_month=16,
                   start_date=start, account_id=accs[2].id, category="Tasse", business_day_rule="successivo"),
        Recurrence(title="Affitto ufficio", amount_cents=-180_000, frequency="mensile", day_of_month=5, start_date=start,
                   account_id=accs[0].id, category="Sede"),
        Recurrence(title="Canone cliente Alfa", amount_cents=950_000, frequency="mensile", day_of_month=10, start_date=start,
                   account_id=accs[0].id, category="Clienti"),
    ])
    for i, (days, amt, title) in enumerate([(4, 1_850_000, "Incasso fatt. 45 – Beta Srl"), (12, -640_000, "Pagamento fatt. 991 – Fornitore Gamma"),
                                             (20, 1_200_000, "Incasso fatt. 47 – Delta Spa"), (-3, -95_000, "Pagamento fatt. 13 – Studio Epsilon")]):
        s.add(PlannedItem(title=title, amount_cents=amt, due_date=today + timedelta(days=days), source="fattura"))
    s.commit()
    sync_all(s)
print(f"Dati demo creati in {config.DATA_DIR}")
