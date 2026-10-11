"""Cantieri: descrizioni dagli XML, assegnazione delle fatture, EBITDA per cantiere."""
import json
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from app.models import Invoice, Project
from app.services import kpi, projects
from app.services.fatturapa import import_invoices, parse_invoice_xml
from tests.test_fatturapa import make_xml


def _xml_with_lines(numero="7"):
    xml = make_xml(numero=numero).decode()
    xml = xml.replace("<ImportoTotaleDocumento>1220.00</ImportoTotaleDocumento></DatiGeneraliDocumento>",
                      "<ImportoTotaleDocumento>1220.00</ImportoTotaleDocumento><Causale>Lavori cantiere Via Gramsci 85 Monterotondo</Causale>"
                      "</DatiGeneraliDocumento>")
    xml = xml.replace("</DatiGenerali>", """</DatiGenerali><DatiBeniServizi>
      <DettaglioLinee><NumeroLinea>1</NumeroLinea><Descrizione>Fornitura piastrelle   bagno</Descrizione><PrezzoTotale>800.00</PrezzoTotale></DettaglioLinee>
      <DettaglioLinee><NumeroLinea>2</NumeroLinea><Descrizione>Trasporto</Descrizione><PrezzoTotale>200.00</PrezzoTotale></DettaglioLinee>
      <DatiRiepilogo><ImponibileImporto>1000.00</ImponibileImporto><Imposta>220.00</Imposta></DatiRiepilogo></DatiBeniServizi>""", 1)
    return xml.encode()


def test_description_from_xml_and_filled_on_existing(session):
    inv = parse_invoice_xml(_xml_with_lines())[0]
    assert inv.description == ("Causale: Lavori cantiere Via Gramsci 85 Monterotondo | "
                               "Righe: Fornitura piastrelle bagno (800.00); Trasporto (200.00)")
    # fattura già caricata dal report Excel (senza descrizione): l'XML aggiunge solo la descrizione
    session.add(Invoice(direction="passiva", doc_type="TD01", number="7", date=date(2026, 9, 30), counterparty="Fornitore Srl",
                        counterparty_vat="01234567890", total_cents=122_000, vat_cents=22_000, file_name="r", file_hash="r"))
    session.commit()
    res = import_invoices(session, [("a.xml", _xml_with_lines())], company_vat="IT09876543210")
    assert not res.imported and res.descriptions_completed == 1
    assert "Gramsci" in session.scalars(select(Invoice)).one().description


def _inv(session, n, direction, cp, taxable, desc="", d=date(2026, 9, 1)):
    i = Invoice(direction=direction, doc_type="TD01", number=n, date=d, counterparty=cp, total_cents=taxable * 110 // 100,
                vat_cents=taxable * 10 // 100, description=desc, file_name=n, file_hash=n)
    session.add(i)
    return i


def test_auto_assign_and_report_match_kpi(session):
    dmg = Project(name="Del Moro – Gramsci", client="DMG Analytics & Consulting", keywords="dmg, Gramsci, Monterotondo")
    luongo = Project(name="Luongo", client="Luciano Luongo", keywords="luongo")
    session.add_all([dmg, luongo])
    _inv(session, "E1", "attiva", "DMG ANALYTICS & CONSULTING SRLS", 1_000_000)
    _inv(session, "E2", "attiva", "Luciano Luongo", 500_000)
    a = _inv(session, "F1", "passiva", "Edil Srl", 300_000, "Causale: materiali cantiere via Gramsci 85")
    b = _inv(session, "F2", "passiva", "Ferramenta", 100_000, "Righe: viti per lavori casa Luongo")
    c = _inv(session, "F3", "passiva", "Telefonia Spa", 50_000, "Canone mensile")
    m = _inv(session, "F4", "passiva", "Ambiguo Srl", 20_000, "Gramsci e Luongo")  # due cantieri: non decide
    session.flush()
    projects.auto_assign(session)
    assert (a.project_id, a.project_source) == (dmg.id, "auto")
    assert b.project_id == luongo.id and c.project_id is None and m.project_id is None
    c.project_source = projects.GENERAL  # scelta manuale: spesa generale
    session.commit()
    rep = projects.report(session, 2026)
    rows = {r.project.name: r for r in rep.rows}
    assert (rows["Del Moro – Gramsci"].revenue, rows["Del Moro – Gramsci"].costs) == (1_000_000, 300_000)
    assert rows["Luongo"].margin == 400_000
    assert rep.general_costs == 50_000 and rep.unassigned_costs == 20_000 and rep.n_unassigned == 1
    assert rep.ebitda == kpi.build(session, 12, date(2026, 10, 11)).ytd_ebitda  # stessi conti dei KPI
    # le scelte manuali e "generale" non vengono toccate dalle regole
    projects.auto_assign(session)
    assert c.project_source == projects.GENERAL


def test_seed_and_client_keywords(session):
    _inv(session, "E1", "attiva", "DMG ANALYTICS & CONSULTING SOCIETA' A RESPONSABILITA' LIMITATA SEMPLIFICATA", 100_000)
    _inv(session, "E2", "attiva", "Manuel Lanza", 100_000)
    session.flush()
    assert projects.seed_from_clients(session, 2026) == 2
    kws = sorted(p.keywords for p in session.scalars(select(Project)))
    assert kws == ["dmg", "lanza"]
    assert projects.seed_from_clients(session, 2026) == 0


def test_ai_assign_with_fake_client(session):
    p = Project(name="Salerno", client="Salerno", keywords="salerno")
    session.add(p)
    x = _inv(session, "F1", "passiva", "Edil Srl", 300_000, "materiali appartamento sig. Rossi")
    y = _inv(session, "F2", "passiva", "Commercialista", 120_000, "consulenza")
    z = _inv(session, "F3", "passiva", "Boh", 100_000, "")
    session.flush()

    class Fake:
        class beta:
            class messages:
                @staticmethod
                def create(**kw):
                    order = [l.split(".")[0] for l in kw["messages"][0]["content"].split("Fatture:")[1].strip().splitlines()]
                    assert len(order) == 3
                    data = {"assignments": [{"n": 1, "project_id": p.id}, {"n": 2, "project_id": 0}, {"n": 3, "project_id": -1}]}
                    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(data))])

    assigned, general = projects.ai_assign(session, 2026, client=Fake)
    assert (assigned, general) == (1, 1)
    srcs = {i.number: (i.project_id, i.project_source) for i in (x, y, z)}
    # in ordine di importo: F1 (300) poi F2/F3
    assert srcs["F1"] == (p.id, "ai") and srcs["F2"] == (None, "generale") and srcs["F3"] == (None, "")


def test_pages(client):
    from app.db import SessionLocal

    with SessionLocal() as s:
        _inv(s, "E1", "attiva", "Manuel Lanza", 100_000)
        _inv(s, "F1", "passiva", "Edil Srl", 50_000, "lavori casa Lanza")
        s.commit()
    r = client.post("/cantieri/crea-da-clienti", data={"anno": "2026"})
    assert r.status_code == 200 and "Manuel Lanza" in r.text
    with SessionLocal() as s:
        p = s.scalars(select(Project)).one()
        f1 = s.scalars(select(Invoice).where(Invoice.number == "F1")).one()
        assert f1.project_id == p.id  # assegnata dalla parola chiave "lanza"
        fid, pid = f1.id, p.id
    page = client.get(f"/cantieri/{pid}?anno=2026").text
    assert "lavori casa Lanza" in page
    client.post("/cantieri/fattura", data={"invoice_id": fid, "target": "generale"})
    with SessionLocal() as s:
        assert s.get(Invoice, fid).project_source == "generale"
    assert "EBITDA per cantiere" in client.get("/cantieri?anno=2026").text
