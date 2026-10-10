"""Note per il commercialista: raccolta, email (diretta via SMTP o dal tuo programma di posta) e stato."""
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountantNote
from app.services.util import get_setting


def pending(session: Session) -> list[AccountantNote]:
    return list(session.scalars(select(AccountantNote).where(AccountantNote.status == "da_inviare")
                                .order_by(AccountantNote.created_at)))


def create(session: Session, title: str, body: str, source: str = "manuale", conversation_id: int | None = None) -> AccountantNote:
    note = AccountantNote(title=title.strip()[:200] or "Domanda", body=body.strip(), source=source,
                          conversation_id=conversation_id)
    session.add(note)
    session.flush()
    return note


def compose(session: Session, notes: list[AccountantNote]) -> tuple[str, str]:
    company = get_setting(session, "company_name") or "la società"
    subject = f"{company} – domande per lo studio ({len(notes)})" if len(notes) > 1 else f"{company} – {notes[0].title}"
    parts = [f"Buongiorno,\n\nvi sottopongo {'alcune domande' if len(notes) > 1 else 'una domanda'} su {company}:\n"]
    for n, note in enumerate(notes, 1):
        parts.append(f"{n}. {note.title}\n{note.body}\n" if note.body else f"{n}. {note.title}\n")
    parts.append("Grazie, cordiali saluti")
    return subject, "\n".join(parts)


def section_for_package(notes: list[AccountantNote]) -> str:
    """Testo da aggiungere all'email del pacchetto mensile."""
    if not notes:
        return ""
    lines = ["\n\nInoltre vi sottopongo queste domande:"]
    for n, note in enumerate(notes, 1):
        lines.append(f"{n}. {note.title}" + (f"\n   {note.body.replace(chr(10), chr(10) + '   ')}" if note.body else ""))
    return "\n".join(lines)


def mailto(session: Session, notes: list[AccountantNote]) -> str:
    subject, body = compose(session, notes)
    return f"mailto:{get_setting(session, 'accountant_email')}?subject={quote(subject)}&body={quote(body)}"


def smtp_enabled() -> bool:
    return bool(os.environ.get("FBP_SMTP_HOST") and os.environ.get("FBP_SMTP_USER") and os.environ.get("FBP_SMTP_PASSWORD"))


def send(session: Session, notes: list[AccountantNote]) -> str:
    """Invia direttamente l'email via SMTP (variabili FBP_SMTP_*). Restituisce il destinatario."""
    to = get_setting(session, "accountant_email").strip()
    if not to:
        raise ValueError("Imposta l'email del commercialista nelle Impostazioni")
    if not smtp_enabled():
        raise ValueError("Invio diretto non configurato: usa 'Prepara email' oppure imposta le variabili FBP_SMTP_*")
    subject, body = compose(session, notes)
    msg = EmailMessage()
    sender = os.environ.get("FBP_SMTP_FROM") or os.environ["FBP_SMTP_USER"]
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    if os.environ.get("FBP_SMTP_CC_ME", "1") == "1":
        msg["Cc"] = sender  # copia a te, così la risposta resta nella tua casella
    msg.set_content(body)
    port = int(os.environ.get("FBP_SMTP_PORT") or 587)
    host = os.environ["FBP_SMTP_HOST"]
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(os.environ["FBP_SMTP_USER"], os.environ["FBP_SMTP_PASSWORD"])
        server.send_message(msg)
    mark_sent(notes)
    return to


def mark_sent(notes: list[AccountantNote]) -> None:
    now = datetime.now()
    for n in notes:
        if n.status == "da_inviare":
            n.status, n.sent_at = "inviata", now
