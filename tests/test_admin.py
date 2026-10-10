import zipfile
from datetime import date

from app.models import Account, BalanceSnapshot, ChecklistItem, DocCategory, Transaction
from app.services import admin_docs as ad
from app.services.util import set_setting


def test_checklist_generation_and_deadlines(session):
    set_setting(session, "admin_start_period", "2026-08")
    session.commit()
    ad.ensure_checklist(session, date(2026, 10, 10))
    keys = {i.period_key for i in session.query(ChecklistItem)}
    assert keys == {"2026-08", "2026-09", "2026-10", "2026-Q3", "2026-Q4"}
    banks = session.query(ChecklistItem).join(DocCategory).filter(DocCategory.code == "movimenti_banche", ChecklistItem.period_key == "2026-09").one()
    assert banks.due_date == date(2026, 10, 6)
    q3 = session.query(ChecklistItem).join(DocCategory).filter(DocCategory.code == "ec_trimestrali", ChecklistItem.period_key == "2026-Q3").one()
    assert q3.due_date == date(2026, 10, 15)
    late = ad.open_deadlines(session, date(2026, 10, 10))
    assert any(d.item.id == banks.id and d.overdue for d in late)
    # idempotente
    n = session.query(ChecklistItem).count()
    ad.ensure_checklist(session, date(2026, 10, 10))
    assert session.query(ChecklistItem).count() == n


def test_upload_export_and_package(session, tmp_path):
    set_setting(session, "admin_start_period", "2026-09")
    set_setting(session, "docs_dir", str(tmp_path / "docs"))
    set_setting(session, "packages_dir", str(tmp_path / "pkg"))
    acc = Account(name="Cassa", kind="cassa")
    session.add(acc)
    session.flush()
    session.add(BalanceSnapshot(account_id=acc.id, date=date(2026, 8, 31), balance_cents=10000))
    session.add(Transaction(account_id=acc.id, date=date(2026, 9, 3), amount_cents=-2500, description="Caffè", dedupe_key="k"))
    session.commit()
    ad.ensure_checklist(session, date(2026, 10, 10))

    def item(code, key):
        return session.query(ChecklistItem).join(DocCategory).filter(DocCategory.code == code, ChecklistItem.period_key == key).one()

    spese = item("spese_varie", "2026-09")
    ad.store_document(session, spese, "ricevuta.pdf", b"%PDF-1")
    ad.store_document(session, spese, "ricevuta.pdf", b"%PDF-2")
    session.commit()
    assert spese.status == "completato"
    assert [d.file_name for d in spese.documents] == ["ricevuta.pdf", "ricevuta (2).pdf"]

    cassa = item("contanti", "2026-09")
    docs = ad.generate_auto_exports(session, cassa)
    assert len(docs) == 1 and docs[0].file_name == "Prima nota cassa Cassa 2026-09.xlsx"
    ad.generate_auto_exports(session, cassa)  # rigenera senza duplicare
    assert len(cassa.documents) == 1

    import openpyxl
    wb = openpyxl.load_workbook(ad.document_path(session, cassa.documents[0]))
    values = [r for r in wb.active.iter_rows(values_only=True)]
    assert values[4][6] == 100.0 and values[5][5] == 25.0 and values[-1][6] == 75.0

    q3 = item("ec_trimestrali", "2026-Q3")
    ad.store_document(session, q3, "ec_q3.pdf", b"%PDF")
    item("assegni_emessi", "2026-09").status = "non_applicabile"
    session.commit()

    path = ad.build_package(session, "2026-09")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert "Riepilogo.txt" in names
    assert any(n.endswith("ricevuta (2).pdf") for n in names)
    assert any("(2026-Q3)" in n for n in names)

    ad.mark_sent(session, ad.package_items(session, "2026-09"))
    assert spese.status == "inviato" and q3.status == "inviato"
    assert item("fatture_estere", "2026-09").status == "da_fare"  # vuoto: non segnato
    # il trimestrale inviato non ricompare nel pacchetto del mese dopo
    assert q3 not in ad.package_items(session, "2026-10")

    doc = spese.documents[0]
    p = ad.document_path(session, doc)
    ad.delete_document(session, doc)
    assert not p.exists()
