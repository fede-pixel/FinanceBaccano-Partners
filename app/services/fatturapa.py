"""Lettura delle fatture elettroniche (FatturaPA XML, anche firmate .p7m) esportate da Aruba.

Dalle fatture si ricavano le scadenze di pagamento: fatture attive -> entrate previste,
fatture passive -> uscite previste."""
import base64
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice, PlannedItem
from app.services.util import parse_amount

CREDIT_NOTE_TYPES = {"TD04", "TD08"}  # nota di credito / nota di credito semplificata
MODALITA = {
    "MP01": "contanti", "MP02": "assegno", "MP05": "bonifico", "MP08": "carta di pagamento",
    "MP12": "RIBA", "MP19": "SEPA Direct Debit", "MP21": "SEPA DD B2B", "MP23": "PagoPA",
}


@dataclass
class Installment:
    due_date: date | None
    amount_cents: int
    method: str


@dataclass
class ParsedInvoice:
    doc_type: str
    number: str
    date: date
    supplier_name: str
    supplier_vat: str
    customer_name: str
    customer_vat: str
    total_cents: int
    installments: list[Installment] = field(default_factory=list)


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def extract_xml(content: bytes) -> bytes:
    """Restituisce l'XML anche se il file è firmato (.p7m, DER o base64)."""
    stripped = content.lstrip()
    if stripped.startswith(b"<"):
        return content
    raw = content
    if not raw[:1] == b"\x30":  # non DER: probabilmente p7m in base64
        try:
            raw = base64.b64decode(re.sub(rb"\s+", b"", content), validate=False)
        except Exception:
            raw = content
    try:
        from asn1crypto import cms

        info = cms.ContentInfo.load(raw)
        data = info["content"]["encap_content_info"]["content"].native
        if data:
            return data
    except Exception:
        pass
    # ultima risorsa: estrae il blocco XML dai byte
    m = re.search(rb"<\?xml.*?</(?:\w+:)?FatturaElettronica>", raw, re.S)
    if m:
        return m.group(0)
    raise ValueError("Formato non riconosciuto: non è un XML FatturaPA né un .p7m valido")


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _party_name(anag: ET.Element | None) -> str:
    if anag is None:
        return ""
    name = _text(anag, "Anagrafica/Denominazione")
    if not name:
        name = f"{_text(anag, 'Anagrafica/Nome')} {_text(anag, 'Anagrafica/Cognome')}".strip()
    return name


def _vat(anag: ET.Element | None) -> str:
    if anag is None:
        return ""
    code = _text(anag, "IdFiscaleIVA/IdCodice")
    return code or _text(anag, "CodiceFiscale")


def parse_invoice_xml(xml_bytes: bytes) -> list[ParsedInvoice]:
    root = _strip_ns(ET.fromstring(xml_bytes))
    header = root.find("FatturaElettronicaHeader")
    sup = header.find("CedentePrestatore/DatiAnagrafici") if header is not None else None
    cus = header.find("CessionarioCommittente/DatiAnagrafici") if header is not None else None
    out = []
    for body in root.findall("FatturaElettronicaBody"):
        gen = body.find("DatiGenerali/DatiGeneraliDocumento")
        doc_type = _text(gen, "TipoDocumento") or "TD01"
        total = parse_amount(_text(gen, "ImportoTotaleDocumento"), ".")
        if total is None:
            # somma imponibile + imposta dal riepilogo
            total = 0
            for r in body.findall("DatiBeniServizi/DatiRiepilogo"):
                total += (parse_amount(_text(r, "ImponibileImporto"), ".") or 0) + (parse_amount(_text(r, "Imposta"), ".") or 0)
        installments = []
        for dp in body.findall("DatiPagamento"):
            for det in dp.findall("DettaglioPagamento"):
                due = _text(det, "DataScadenzaPagamento")
                amount = parse_amount(_text(det, "ImportoPagamento"), ".")
                if amount is None:
                    continue
                installments.append(
                    Installment(
                        due_date=date.fromisoformat(due[:10]) if due else None,
                        amount_cents=amount,
                        method=MODALITA.get(_text(det, "ModalitaPagamento"), _text(det, "ModalitaPagamento")),
                    )
                )
        out.append(
            ParsedInvoice(
                doc_type=doc_type,
                number=_text(gen, "Numero"),
                date=date.fromisoformat(_text(gen, "Data")[:10]),
                supplier_name=_party_name(sup),
                supplier_vat=_vat(sup),
                customer_name=_party_name(cus),
                customer_vat=_vat(cus),
                total_cents=total,
                installments=installments,
            )
        )
    return out


def iter_invoice_files(filename: str, content: bytes):
    """Espande gli ZIP (Aruba permette l'export massivo in zip) e restituisce (nome, bytes)."""
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for info in zf.infolist():
                n = info.filename.lower()
                if not info.is_dir() and (n.endswith(".xml") or n.endswith(".p7m")) and "__macosx" not in n:
                    yield info.filename.rsplit("/", 1)[-1], zf.read(info)
    else:
        yield filename, content


def _vat_digits(v: str) -> str:
    return re.sub(r"^[A-Z]{2}", "", v.strip().upper().replace(" ", ""))


@dataclass
class InvoiceImportResult:
    imported: list[Invoice] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def import_invoices(
    session: Session,
    files: list[tuple[str, bytes]],
    company_vat: str,
    forced_direction: str = "auto",
    default_payment_days: int = 30,
    account_id: int | None = None,
    paid_before: date | None = None,
) -> InvoiceImportResult:
    """`paid_before`: le scadenze precedenti a questa data vengono segnate come già saldate
    (utile al primo import dello storico, per non riempire il forecast di finti scaduti)."""
    res = InvoiceImportResult()
    our_vat = _vat_digits(company_vat)
    for upload_name, content in files:
        try:
            entries = list(iter_invoice_files(upload_name, content))
        except zipfile.BadZipFile:
            res.errors.append(f"{upload_name}: ZIP non valido")
            continue
        for name, data in entries:
            file_hash = hashlib.sha256(data).hexdigest()
            if session.scalar(select(Invoice.id).where(Invoice.file_hash == file_hash)):
                res.duplicates.append(name)
                continue
            try:
                parsed_list = parse_invoice_xml(extract_xml(data))
            except Exception as exc:  # file malformato: lo segnalo e proseguo con gli altri
                res.errors.append(f"{name}: {exc}")
                continue
            if not parsed_list:
                res.errors.append(f"{name}: non è una fattura elettronica (FatturaPA)")
                continue
            for i, inv in enumerate(parsed_list):
                if forced_direction in ("attiva", "passiva"):
                    direction = forced_direction
                elif our_vat and _vat_digits(inv.supplier_vat) == our_vat:
                    direction = "attiva"
                elif our_vat and _vat_digits(inv.customer_vat) == our_vat:
                    direction = "passiva"
                else:
                    res.errors.append(
                        f"{name}: impossibile capire se è attiva o passiva. Imposta la P.IVA aziendale nelle Impostazioni "
                        "oppure scegli la direzione nell'import."
                    )
                    continue
                counterparty = inv.customer_name if direction == "attiva" else inv.supplier_name
                cp_vat = inv.customer_vat if direction == "attiva" else inv.supplier_vat
                invoice = Invoice(
                    direction=direction,
                    doc_type=inv.doc_type,
                    number=inv.number,
                    date=inv.date,
                    counterparty=counterparty,
                    counterparty_vat=cp_vat,
                    total_cents=inv.total_cents,
                    file_name=name,
                    file_hash=file_hash if i == 0 else f"{file_hash[:56]}-{i}",
                )
                session.add(invoice)
                session.flush()
                # entrata per le attive, uscita per le passive; le note di credito invertono il segno
                sign = 1 if direction == "attiva" else -1
                if inv.doc_type in CREDIT_NOTE_TYPES:
                    sign = -sign
                installments = inv.installments or [Installment(None, inv.total_cents, "")]
                for n, inst in enumerate(installments, start=1):
                    estimated = inst.due_date is None
                    due = inst.due_date or inv.date + timedelta(days=default_payment_days)
                    label = "Incasso" if sign > 0 else "Pagamento"
                    suffix = f" (rata {n}/{len(installments)})" if len(installments) > 1 else ""
                    session.add(
                        PlannedItem(
                            title=f"{label} fatt. {inv.number} – {counterparty}{suffix}",
                            amount_cents=sign * abs(inst.amount_cents),
                            due_date=due,
                            account_id=account_id,
                            counterparty=counterparty,
                            category="Fatture attive" if direction == "attiva" else "Fatture passive",
                            source="fattura",
                            estimated_date=estimated,
                            invoice_id=invoice.id,
                            notes=inst.method,
                            status="pagato" if paid_before and due < paid_before else "previsto",
                        )
                    )
                res.imported.append(invoice)
    session.commit()
    return res
