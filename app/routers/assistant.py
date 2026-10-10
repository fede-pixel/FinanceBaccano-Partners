"""Assistente AI: chat sui dati dell'app."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import ChatConversation
from app.services import assistant
from app.services.ai_extract import AIExtractionError, AIUnavailable, is_enabled
from app.web import redirect, render

router = APIRouter()

EXAMPLES = [
    "Come stiamo messi di liquidità nei prossimi 30 giorni?",
    "Quali clienti devono ancora pagarci e da quanto tempo?",
    "Quali fornitori dobbiamo pagare questo mese?",
    "Dove abbiamo speso di più negli ultimi 3 mesi rispetto ai 3 precedenti?",
    "Cosa devo mandare al commercialista entro il 6?",
    "Dammi 3 consigli concreti per migliorare la cassa.",
]


@router.get("/assistente")
def page(request: Request, c: int = 0, session: Session = Depends(get_session)):
    conv = session.get(ChatConversation, c) if c else None
    return render(
        request, "assistant.html", conv=conv, messages=json.loads(conv.display) if conv else [],
        conversations=assistant.conversations(session), ai_enabled=is_enabled(), examples=EXAMPLES,
    )


@router.post("/assistente/chiedi")
def ask(question: str = Form(...), conv_id: int = Form(0), session: Session = Depends(get_session)):
    question = question.strip()
    if not question:
        return redirect(f"/assistente?c={conv_id}" if conv_id else "/assistente")
    conv = session.get(ChatConversation, conv_id) if conv_id else None
    if conv is None:
        conv = ChatConversation(title="", created_at=datetime.now())
        session.add(conv)
        session.flush()
    try:
        assistant.ask(session, conv, question)
    except (AIUnavailable, AIExtractionError) as exc:
        session.rollback()
        return redirect(f"/assistente?c={conv_id}" if conv_id else "/assistente", err=str(exc))
    conv.updated_at = datetime.now()
    session.commit()
    return redirect(f"/assistente?c={conv.id}#ultima")


@router.post("/assistente/{conv_id}/elimina")
def delete(conv_id: int, session: Session = Depends(get_session)):
    conv = session.get(ChatConversation, conv_id)
    if conv is None:
        raise HTTPException(404, "Non trovata")
    session.delete(conv)
    session.commit()
    return redirect("/assistente", msg="Conversazione eliminata")
