"""Note per il commercialista."""
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AccountantNote, ChatConversation
from app.services import notes as ns
from app.services.util import get_setting
from app.web import redirect, render

router = APIRouter()


def _ids(raw) -> list[int]:
    return [int(i) for i in raw if str(i).isdigit()]


@router.get("/commercialista/note")
def notes_page(request: Request, session: Session = Depends(get_session)):
    todo = ns.pending(session)
    sent = list(session.scalars(select(AccountantNote).where(AccountantNote.status != "da_inviare")
                                .order_by(AccountantNote.sent_at.desc().nullslast(), AccountantNote.id.desc()).limit(50)))
    return render(request, "notes.html", todo=todo, sent=sent, mailto=ns.mailto(session, todo) if todo else "",
                  smtp=ns.smtp_enabled(), email=get_setting(session, "accountant_email"))


@router.post("/commercialista/note/nuova")
def note_new(title: str = Form(...), body: str = Form(""), session: Session = Depends(get_session)):
    ns.create(session, title, body)
    session.commit()
    return redirect("/commercialista/note", msg="Nota aggiunta")


@router.post("/commercialista/note/{note_id}/salva")
def note_save(note_id: int, title: str = Form(...), body: str = Form(""), reply: str = Form(""), status: str = Form(""),
              session: Session = Depends(get_session)):
    note = session.get(AccountantNote, note_id)
    if note is None:
        raise HTTPException(404, "Non trovata")
    note.title, note.body, note.reply = title.strip()[:200], body.strip(), reply.strip()
    if status in ("da_inviare", "inviata", "risolta"):
        note.status = status
    session.commit()
    return redirect("/commercialista/note", msg="Nota salvata")


@router.post("/commercialista/note/{note_id}/elimina")
def note_delete(note_id: int, session: Session = Depends(get_session)):
    note = session.get(AccountantNote, note_id)
    if note:
        session.delete(note)
        session.commit()
    return redirect("/commercialista/note", msg="Nota eliminata")


@router.post("/commercialista/note/invia")
async def notes_send(request: Request, session: Session = Depends(get_session)):
    """Invia le note selezionate: via SMTP se configurato, altrimenti le segna inviate dopo l'email dal tuo programma."""
    form = await request.form()
    ids = _ids(form.getlist("note_ids"))
    chosen = [n for n in ns.pending(session) if n.id in ids] if ids else ns.pending(session)
    if not chosen:
        return redirect("/commercialista/note", err="Nessuna nota da inviare")
    if form.get("mode") == "segna":
        ns.mark_sent(chosen)
        session.commit()
        return redirect("/commercialista/note", msg=f"{len(chosen)} note segnate come inviate")
    try:
        to = ns.send(session, chosen)
    except Exception as exc:  # errore SMTP o configurazione: lo mostro
        return redirect("/commercialista/note", err=f"Email non inviata: {exc}")
    session.commit()
    return redirect("/commercialista/note", msg=f"Email inviata a {to} con {len(chosen)} note")


@router.post("/assistente/{conv_id}/nota")
def note_from_chat(conv_id: int, index: int = Form(...), session: Session = Depends(get_session)):
    """Salva una risposta dell'assistente come nota per il commercialista."""
    conv = session.get(ChatConversation, conv_id)
    if conv is None:
        raise HTTPException(404, "Non trovata")
    display = json.loads(conv.display or "[]")
    if not 0 <= index < len(display) or display[index].get("role") != "assistant":
        raise HTTPException(404, "Messaggio non trovato")
    question = next((d["text"] for d in reversed(display[:index]) if d.get("role") == "user"), "Domanda")
    ns.create(session, question[:200], display[index]["text"], source="assistente", conversation_id=conv_id)
    session.commit()
    return redirect(f"/assistente?c={conv_id}", msg="Salvata tra le note per il commercialista")
