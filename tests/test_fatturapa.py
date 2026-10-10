from datetime import date

from asn1crypto import cms, core

from app.models import Invoice, PlannedItem
from app.services.fatturapa import extract_xml, import_invoices, parse_invoice_xml


def make_xml(supplier_vat="01234567890", customer_vat="09876543210", tipo="TD01", payments=True, numero="12/A"):
    pay = """
    <DatiPagamento>
      <CondizioniPagamento>TP01</CondizioniPagamento>
      <DettaglioPagamento><ModalitaPagamento>MP05</ModalitaPagamento><DataScadenzaPagamento>2026-10-31</DataScadenzaPagamento><ImportoPagamento>610.00</ImportoPagamento></DettaglioPagamento>
      <DettaglioPagamento><ModalitaPagamento>MP05</ModalitaPagamento><DataScadenzaPagamento>2026-11-30</DataScadenzaPagamento><ImportoPagamento>610.00</ImportoPagamento></DettaglioPagamento>
    </DatiPagamento>""" if payments else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica versione="FPR12" xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2">
  <FatturaElettronicaHeader>
    <CedentePrestatore><DatiAnagrafici><IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>{supplier_vat}</IdCodice></IdFiscaleIVA>
      <Anagrafica><Denominazione>Fornitore Srl</Denominazione></Anagrafica></DatiAnagrafici></CedentePrestatore>
    <CessionarioCommittente><DatiAnagrafici><IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>{customer_vat}</IdCodice></IdFiscaleIVA>
      <Anagrafica><Nome>Mario</Nome><Cognome>Rossi</Cognome></Anagrafica></DatiAnagrafici></CessionarioCommittente>
  </FatturaElettronicaHeader>
  <FatturaElettronicaBody>
    <DatiGenerali><DatiGeneraliDocumento><TipoDocumento>{tipo}</TipoDocumento><Divisa>EUR</Divisa><Data>2026-09-30</Data>
      <Numero>{numero}</Numero><ImportoTotaleDocumento>1220.00</ImportoTotaleDocumento></DatiGeneraliDocumento></DatiGenerali>
    {pay}
  </FatturaElettronicaBody>
</p:FatturaElettronica>""".encode()


def test_parse_installments():
    inv = parse_invoice_xml(make_xml())[0]
    assert inv.number == "12/A" and inv.total_cents == 122000
    assert inv.customer_name == "Mario Rossi"
    assert [i.due_date for i in inv.installments] == [date(2026, 10, 31), date(2026, 11, 30)]
    assert inv.installments[0].method == "bonifico"


def test_p7m_extraction():
    xml = make_xml()
    signed = cms.ContentInfo({
        "content_type": "signed_data",
        "content": cms.SignedData({
            "version": "v1",
            "digest_algorithms": [],
            "encap_content_info": {"content_type": "data", "content": core.OctetString(xml)},
            "signer_infos": [],
        }),
    }).dump()
    assert extract_xml(signed) == xml
    import base64
    assert extract_xml(base64.b64encode(signed)) == xml


def test_import_direction_and_duplicates(session):
    our = "IT09876543210"
    res = import_invoices(session, [("a.xml", make_xml())], company_vat=our)
    assert len(res.imported) == 1 and res.imported[0].direction == "passiva"
    items = session.query(PlannedItem).order_by(PlannedItem.due_date).all()
    assert [i.amount_cents for i in items] == [-61000, -61000]
    assert "rata 1/2" in items[0].title

    again = import_invoices(session, [("a.xml", make_xml())], company_vat=our)
    assert again.duplicates == ["a.xml"]

    # fattura attiva senza scadenze: data stimata
    res = import_invoices(session, [("b.xml", make_xml(supplier_vat="09876543210", customer_vat="111", payments=False, numero="7"))],
                          company_vat=our, default_payment_days=60)
    inv = res.imported[0]
    assert inv.direction == "attiva"
    item = inv.planned_items[0]
    assert item.amount_cents == 122000 and item.estimated_date and item.due_date == date(2026, 11, 29)

    # nota di credito passiva -> entrata
    res = import_invoices(session, [("c.xml", make_xml(tipo="TD04", payments=False, numero="NC1"))], company_vat=our)
    assert res.imported[0].planned_items[0].amount_cents == 122000

    # senza P.IVA configurata non indovina
    res = import_invoices(session, [("d.xml", make_xml(numero="99"))], company_vat="")
    assert res.errors and not res.imported
    assert session.query(Invoice).count() == 3


def test_zip_and_paid_before(session):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fatture/a.xml", make_xml(numero="1"))
        zf.writestr("fatture/b.xml", make_xml(numero="2"))
        zf.writestr("readme.txt", "x")
    res = import_invoices(session, [("export.zip", buf.getvalue())], company_vat="09876543210", paid_before=date(2026, 11, 1))
    assert len(res.imported) == 2
    statuses = sorted((i.due_date, i.status) for inv in res.imported for i in inv.planned_items)
    assert statuses[0] == (date(2026, 10, 31), "pagato")
    assert statuses[-1] == (date(2026, 11, 30), "previsto")


def test_non_invoice_xml_is_reported(session):
    res = import_invoices(session, [("altro.xml", b"<?xml version='1.0'?><ordine/>")], company_vat="09876543210")
    assert not res.imported and res.errors == ["altro.xml: non è una fattura elettronica (FatturaPA)"]
