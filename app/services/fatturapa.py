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
# Integrazioni/autofatture per reverse charge (es. servizi esteri come Google, Anthropic, AWS): documenti fiscali
# emessi da noi stessi, il pagamento vero va al fornitore estero ed è già nei movimenti -> nessuna uscita prevista
SELF_INVOICE_TYPES = {"TD16", "TD17", "TD18", "TD19", "TD20", "TD21", "TD27"}
IMMEDIATE_METHODS = {"MP01", "MP08"}  # contanti e carta: la fattura arriva già pagata
REPORT_EXT = (".xls", ".xlsx", ".csv")
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
    vat_cents: int | None = None


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
        riepilogo = body.findall("DatiBeniServizi/DatiRiepilogo")
        vat = sum(parse_amount(_text(r, "Imposta"), ".") or 0 for r in riepilogo) if riepilogo else None
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
                vat_cents=vat,
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


def _existing(session: Session, direction: str, number: str, inv_date: date, vat: str, doc_type: str) -> Invoice | None:
    """Stessa fattura già presente (ad es. importata prima come XML e ora dal report Excel, o viceversa)."""
    vat = _vat_digits(vat)
    for inv in session.scalars(select(Invoice).where(
        Invoice.direction == direction, Invoice.number == number, Invoice.date == inv_date, Invoice.doc_type == doc_type
    )):
        if not vat or _vat_digits(inv.counterparty_vat) == vat:
            return inv
    return None


# ---------- report Excel di Aruba ("ReportFattureRicevute.xls" / "ReportFattureEmesse.xls") ----------

@dataclass
class ReportRow:
    sdi_id: str
    file_name: str
    number: str
    date: date
    doc_type: str
    counterparty: str
    vat: str
    total_cents: int
    to_pay_cents: int
    vat_cents: int | None
    method_code: str
    paid: bool
    paid_date: date | None


def _report_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(rows[:15]):
        cols = {c.strip().lower(): n for n, c in enumerate(row) if c and c.strip()}
        if "id sdi" in cols and "totale documento" in cols and "data documento" in cols:
            return i, cols
    return None


def is_aruba_report(filename: str, content: bytes) -> bool:
    if not filename.lower().endswith(REPORT_EXT):
        return False
    from app.services.bank_import import read_table

    try:
        return _report_header(read_table(content, filename)) is not None
    except Exception:
        return False


def parse_aruba_report(filename: str, content: bytes) -> tuple[str, list[ReportRow], list[str]]:
    """Restituisce (direzione ricavata dal report: attiva/passiva/"", righe, errori)."""
    from app.services.bank_import import parse_date_value, read_table

    rows = read_table(content, filename)
    found = _report_header(rows)
    if found is None:
        raise ValueError("non è un report fatture di Aruba (mancano le colonne ID SdI / Totale documento)")
    start, cols = found
    if "fornitore" in cols or "ricevute" in filename.lower():
        direction, party = "passiva", cols.get("fornitore")
    elif any(k in cols for k in ("cliente", "destinatario", "committente")) or "emesse" in filename.lower():
        direction, party = "attiva", cols.get("cliente", cols.get("destinatario", cols.get("committente")))
    else:
        direction, party = "", None

    def get(row, key):
        n = cols.get(key)
        return row[n].strip() if n is not None and n < len(row) and row[n] else ""

    out, errors = [], []
    for i, row in enumerate(rows[start + 1:], start=start + 2):
        if not any(c.strip() for c in row):
            continue
        number, sdi = get(row, "numero"), get(row, "id sdi")
        d = parse_date_value(get(row, "data documento")[:10], "auto")
        total = parse_amount(get(row, "totale documento"), ".")
        if d is None:
            errors.append(f"riga {i} (n. {number or '?'}): data documento mancante, saltata")
            continue
        if total is None:  # succede quando Aruba non riesce a leggere gli importi della fattura
            errors.append(f"riga {i} (n. {number or '?'}): il report non riporta il totale, fattura archiviata senza scadenza "
                          "(carica l'XML per avere l'importo)")
            total = 0
        net = parse_amount(get(row, "netto a pagare"), ".")
        tipo = re.search(r"TD\d{2}", get(row, "tipo documento"))
        method = re.match(r"MP\d{2}", get(row, "metodo di pagamento"))
        out.append(ReportRow(
            sdi_id=sdi, file_name=get(row, "nome file"), number=number, date=d,
            doc_type=tipo.group(0) if tipo else "TD01",
            counterparty=row[party].strip() if party is not None and party < len(row) else "",
            vat=get(row, "p.iva") or get(row, "codice fiscale"),
            total_cents=total, to_pay_cents=net if net is not None else total,
            vat_cents=parse_amount(get(row, "totale iva"), ".") if "totale iva" in cols else None,
            method_code=method.group(0) if method else "",
            paid=get(row, "pagamenti").lower() == "pagata",
            paid_date=parse_date_value(get(row, "data pagamento")[:10], "auto") if get(row, "data pagamento") else None,
        ))
    return direction, out, errors


def _import_report(session, res, name, content, forced_direction, default_payment_days, account_id, paid_before) -> None:
    try:
        direction, rows, errors = parse_aruba_report(name, content)
    except Exception as exc:
        res.errors.append(f"{name}: {exc}")
        return
    if forced_direction in ("attiva", "passiva"):
        direction = forced_direction
    if not direction:
        res.errors.append(f"{name}: non capisco se il report è di fatture emesse o ricevute: scegli il tipo nell'import")
        return
    res.errors.extend(f"{name}: {e}" for e in errors)
    for r in rows:
        key = hashlib.sha256(f"sdi:{r.sdi_id or (r.number, r.date, r.vat)}".encode()).hexdigest()
        dup = session.scalar(select(Invoice).where(Invoice.file_hash == key)) or _existing(
            session, direction, r.number, r.date, r.vat, r.doc_type
        )
        if dup is not None:
            if dup.vat_cents is None and r.vat_cents is not None:
                dup.vat_cents = r.vat_cents  # ricaricando il report si completano le fatture importate senza IVA
                res.vat_completed += 1
            if r.doc_type in SELF_INVOICE_TYPES and direction == "passiva":
                # la stessa autofattura compare anche tra le emesse (intestata a noi): qui c'è il vero fornitore
                dup.direction, dup.counterparty, dup.counterparty_vat = "passiva", r.counterparty, r.vat
            res.duplicates.append(r.number)
            continue
        # le autofatture (TD16-TD19…) sono sempre un costo, anche quando Aruba le elenca tra le fatture emesse
        row_direction = "passiva" if r.doc_type in SELF_INVOICE_TYPES else direction
        invoice = Invoice(
            direction=row_direction, doc_type=r.doc_type, number=r.number, date=r.date, counterparty=r.counterparty,
            counterparty_vat=r.vat, total_cents=r.total_cents, vat_cents=r.vat_cents, file_name=r.file_name or name,
            file_hash=key,
        )
        session.add(invoice)
        session.flush()
        res.imported.append(invoice)
        if r.doc_type in SELF_INVOICE_TYPES or not r.to_pay_cents:
            continue
        sign = 1 if direction == "attiva" else -1
        if r.doc_type in CREDIT_NOTE_TYPES:
            sign = -sign
        if r.paid:
            due, estimated, status = r.paid_date or r.date, False, "pagato"
        elif r.method_code in IMMEDIATE_METHODS:
            due, estimated, status = r.date, False, "pagato"
        else:
            due, estimated = r.date + timedelta(days=default_payment_days), True
            status = "pagato" if paid_before and due < paid_before else "previsto"
        label = "Incasso" if sign > 0 else "Pagamento"
        session.add(PlannedItem(
            title=f"{label} fatt. {r.number} – {r.counterparty}",
            amount_cents=sign * abs(r.to_pay_cents),
            due_date=due,
            account_id=account_id,
            counterparty=r.counterparty,
            category="Fatture attive" if direction == "attiva" else "Fatture passive",
            source="fattura",
            estimated_date=estimated,
            invoice_id=invoice.id,
            notes=MODALITA.get(r.method_code, r.method_code),
            status=status,
        ))


@dataclass
class InvoiceImportResult:
    imported: list[Invoice] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    vat_completed: int = 0  # fatture già presenti a cui è stata aggiunta l'IVA
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
        if upload_name.lower().endswith(REPORT_EXT):
            _import_report(session, res, upload_name, content, forced_direction, default_payment_days, account_id, paid_before)
            continue
        try:
            entries = list(iter_invoice_files(upload_name, content))
        except zipfile.BadZipFile:
            res.errors.append(f"{upload_name}: ZIP non valido")
            continue
        for name, data in entries:
            file_hash = hashlib.sha256(data).hexdigest()
            dups = list(session.scalars(select(Invoice).where(
                (Invoice.file_hash == file_hash) | Invoice.file_hash.like(f"{file_hash[:56]}-%"))))
            if dups:
                if any(d.vat_cents is None for d in dups):
                    # file già importato prima che l'app salvasse l'IVA: la completo senza duplicare
                    try:
                        parsed = parse_invoice_xml(extract_xml(data))
                    except Exception:
                        parsed = []
                    for d in dups:
                        i = 0 if d.file_hash == file_hash else int(d.file_hash.rsplit("-", 1)[1])
                        if d.vat_cents is None and i < len(parsed) and parsed[i].vat_cents is not None:
                            d.vat_cents = parsed[i].vat_cents
                            res.vat_completed += 1
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
                if inv.doc_type in SELF_INVOICE_TYPES:
                    direction = "passiva"  # autofattura: costo verso il fornitore (estero), anche se emessa da noi
                counterparty = inv.customer_name if direction == "attiva" else inv.supplier_name
                cp_vat = inv.customer_vat if direction == "attiva" else inv.supplier_vat
                old = _existing(session, direction, inv.number, inv.date, cp_vat, inv.doc_type)
                if old is not None and old.total_cents == 0 and not old.planned_items:
                    session.delete(old)  # riga del report Excel senza importi: l'XML la sostituisce
                    session.flush()
                elif old is not None:
                    if old.vat_cents is None and inv.vat_cents is not None:
                        old.vat_cents = inv.vat_cents
                        res.vat_completed += 1
                    res.duplicates.append(name)
                    continue
                invoice = Invoice(
                    direction=direction,
                    doc_type=inv.doc_type,
                    number=inv.number,
                    date=inv.date,
                    counterparty=counterparty,
                    counterparty_vat=cp_vat,
                    total_cents=inv.total_cents,
                    vat_cents=inv.vat_cents,
                    file_name=name,
                    file_hash=file_hash if i == 0 else f"{file_hash[:56]}-{i}",
                )
                session.add(invoice)
                session.flush()
                # entrata per le attive, uscita per le passive; le note di credito invertono il segno
                sign = 1 if direction == "attiva" else -1
                if inv.doc_type in CREDIT_NOTE_TYPES:
                    sign = -sign
                if inv.doc_type in SELF_INVOICE_TYPES:
                    res.imported.append(invoice)
                    continue
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


def fix_self_invoices(session: Session) -> int:
    """Correzione dei dati già importati: le autofatture caricate come "emesse" diventano costi (passive).
    Le loro eventuali scadenze senza movimenti vengono tolte (il pagamento vero è al fornitore estero)."""
    n = 0
    for inv in session.scalars(select(Invoice).where(Invoice.doc_type.in_(SELF_INVOICE_TYPES))):
        if inv.direction != "passiva":
            inv.direction = "passiva"
            n += 1
        for it in list(inv.planned_items):
            if not it.transactions and not it.payment_tx_id:
                session.delete(it)
    return n
