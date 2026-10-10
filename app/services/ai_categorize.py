"""Classificazione dei gruppi di spesa con Claude (opzionale, usa la stessa ANTHROPIC_API_KEY).

A Claude vengono inviati solo i nomi degli esercenti e una descrizione d'esempio per ciascun gruppo
(niente importi, conti o date). Può scegliere esclusivamente tra le categorie esistenti; se non è sicuro
risponde "Altro". Le proposte diventano regole di tipo "ai", che le tue correzioni manuali scavalcano sempre."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CategoryRule
from app.services import categorize
from app.services.ai_extract import AIExtractionError, AIUnavailable, is_enabled, model_name

MAX_CLUSTERS = 80

PROMPT = """Sei l'assistente amministrativo di una piccola società italiana di servizi.
Per ogni esercente/controparte qui sotto (con una descrizione bancaria d'esempio) scegli la categoria di spesa più adatta,
usando SOLO una delle categorie ammesse. Se non sei ragionevolmente sicuro, rispondi "{other}".
Non usare "{internal}" (giroconti) né "Entrate".
Regola dell'azienda: i pagamenti e i bonifici verso persone fisiche (es. "Giorgio Cicero", "Davide Massimiani")
e verso piccole ditte o imprese individuali (es. "Renovatio di Cipriani e Boezio") sono operai e ditte che lavorano
per noi: classificali sempre come "{subcontract}". Restano esclusi i nomi di negozi, marchi, enti e servizi noti.

Categorie ammesse: {categories}

Esercenti:
{items}"""


def categories_for_ai(session: Session) -> list[str]:
    custom = set(session.scalars(select(CategoryRule.category).distinct()))
    cats = [c for c in categorize.CATEGORIES if c not in (categorize.INTERNAL,)]
    cats += sorted(custom - set(cats) - {categorize.INTERNAL, categorize.INCOME})
    return cats


def suggest(session: Session, clusters: list[categorize.Cluster], client=None) -> dict[str, str]:
    """Restituisce {chiave esercente: categoria} per i gruppi che Claude sa classificare."""
    if not clusters:
        return {}
    if client is None:
        if not is_enabled():
            raise AIUnavailable("Classificazione con AI non configurata: manca la variabile ANTHROPIC_API_KEY")
        import anthropic

        client = anthropic.Anthropic(timeout=300, max_retries=2)
    import anthropic

    cats = categories_for_ai(session)
    clusters = clusters[:MAX_CLUSTERS]
    items = "\n".join(f"{i + 1}. {c.name} — es. \"{c.example}\"" for i, c in enumerate(clusters))
    schema = {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}, "category": {"type": "string", "enum": cats}},
                    "required": ["n", "category"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assignments"],
        "additionalProperties": False,
    }
    try:
        message = client.beta.messages.create(
            model=model_name(),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": PROMPT.format(
                other=categorize.OTHER, internal=categorize.INTERNAL, subcontract=categorize.SUBCONTRACT, categories=", ".join(cats), items=items)}],
        )
    except anthropic.AuthenticationError as exc:
        raise AIUnavailable("Chiave API di Anthropic non valida: controlla ANTHROPIC_API_KEY") from exc
    except anthropic.APIStatusError as exc:
        raise AIExtractionError(f"Errore del servizio Anthropic ({exc.status_code}): riprova più tardi") from exc
    except anthropic.APIConnectionError as exc:
        raise AIExtractionError("Connessione al servizio Anthropic non riuscita") from exc
    if message.stop_reason == "refusal":
        raise AIExtractionError("Il modello non ha elaborato la richiesta")
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AIExtractionError("Risposta non leggibile dal modello: riprova") from exc
    out = {}
    for a in data.get("assignments", []):
        n, cat = a.get("n"), a.get("category")
        if isinstance(n, int) and 1 <= n <= len(clusters) and cat in cats and cat != categorize.OTHER:
            out[clusters[n - 1].key] = cat
    return out


def apply_suggestions(session: Session, suggestions: dict[str, str]) -> int:
    """Crea le regole proposte (tipo "ai") e ricalcola. Restituisce i movimenti aggiornati."""
    existing = {r.pattern: r for r in session.scalars(select(CategoryRule))}
    for key, cat in suggestions.items():
        key = " ".join(key.lower().split())
        rule = existing.get(key)
        if rule is None:
            session.add(CategoryRule(pattern=key, category=cat, priority=50, source="ai"))
        elif rule.source != "tua":  # una tua regola non viene mai sostituita
            rule.category, rule.priority, rule.source = cat, 50, "ai"
    session.flush()
    changed = categorize.apply(session)
    session.commit()
    return changed
