"""Lettura degli estratti conto PDF con Claude (opzionale).

Si usa quando la lettura automatica non riesce a interpretare l'impaginazione della banca.
Il PDF viene inviato all'API di Anthropic come documento: Claude vede la pagina come la vedi tu
(funziona anche con le scansioni) e restituisce i movimenti in JSON validato da uno schema.

Richiede la variabile d'ambiente ANTHROPIC_API_KEY. Il modello si può cambiare con FBP_AI_MODEL."""
import base64
import json
import os
from dataclasses import dataclass
from datetime import date

from app.services.bank_import import ParsedTx

DEFAULT_MODEL = "claude-opus-5-5"
MAX_PDF_BYTES = 30 * 1024 * 1024  # limite della richiesta API: 32 MB

SCHEMA = {
    "type": "object",
    "properties": {
        "bank_name": {"type": "string"},
        "account_iban": {"type": "string", "description": "IBAN del conto se presente, altrimenti stringa vuota"},
        "currency": {"type": "string"},
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Data contabile/di operazione in formato YYYY-MM-DD"},
                    "description": {"type": "string"},
                    "counterparty": {"type": "string", "description": "Controparte o esercente se riconoscibile, altrimenti stringa vuota"},
                    "amount": {"type": "number", "description": "Importo con segno: negativo per uscite/addebiti, positivo per entrate/accrediti"},
                    "balance": {"type": ["number", "null"], "description": "Saldo dopo il movimento se indicato, altrimenti null"},
                },
                "required": ["date", "description", "counterparty", "amount", "balance"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bank_name", "account_iban", "currency", "transactions"],
    "additionalProperties": False,
}

PROMPT = """Questo è un estratto conto o una lista movimenti di una banca o di una carta.
Estrai TUTTI i movimenti, da tutte le pagine, nell'ordine in cui compaiono nel documento.

Regole:
- Un elemento per ogni movimento. Non inventare movimenti e non ometterne nessuno.
- Escludi le righe che non sono movimenti: saldo iniziale/finale, totali, riepiloghi, intestazioni ripetute, note.
- "amount" con segno: negativo per uscite, addebiti, pagamenti, commissioni, prelievi; positivo per entrate, accrediti, rimborsi.
  Se il documento ha colonne separate (es. Dare/Avere, Uscite/Entrate, Money out/Money in) usa la colonna per decidere il segno.
- Importi in numero decimale con il punto (es. 1234.56), senza simbolo di valuta.
- "date" in formato YYYY-MM-DD: usa la data contabile o di operazione (non la data valuta, se sono entrambe presenti).
- "description": il testo del movimento così come scritto, su una riga sola.
- "balance": il saldo dopo il movimento, se il documento lo riporta riga per riga; altrimenti null."""


class AIUnavailable(Exception):
    pass


class AIExtractionError(Exception):
    pass


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.environ.get("FBP_AI_MODEL", DEFAULT_MODEL)


@dataclass
class AIResult:
    transactions: list[ParsedTx]
    bank_name: str
    account_iban: str
    currency: str
    input_tokens: int
    output_tokens: int
    model: str

    def to_json(self) -> str:
        return json.dumps({
            "bank_name": self.bank_name, "account_iban": self.account_iban, "currency": self.currency,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "model": self.model,
            "transactions": [
                {"date": t.date.isoformat(), "description": t.description, "counterparty": t.counterparty,
                 "amount_cents": t.amount_cents, "balance_cents": t.balance_cents}
                for t in self.transactions
            ],
        })

    @classmethod
    def from_json(cls, raw: str) -> "AIResult":
        d = json.loads(raw)
        txs = [
            ParsedTx(date=date.fromisoformat(t["date"]), amount_cents=t["amount_cents"], description=t["description"],
                     counterparty=t["counterparty"], external_id="", balance_cents=t["balance_cents"], row_index=i)
            for i, t in enumerate(d["transactions"])
        ]
        return cls(txs, d["bank_name"], d["account_iban"], d["currency"], d["input_tokens"], d["output_tokens"], d["model"])


def _cents(value) -> int | None:
    if value is None:
        return None
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_parsed(data: dict) -> list[ParsedTx]:
    out = []
    for i, t in enumerate(data.get("transactions", [])):
        try:
            d = date.fromisoformat(str(t["date"])[:10])
            amount = _cents(t["amount"])
        except (KeyError, ValueError, TypeError):
            continue  # riga malformata: la salto, verrà segnalata dal conteggio
        if amount is None:
            continue
        out.append(ParsedTx(
            date=d, amount_cents=amount, description=" ".join(str(t.get("description", "")).split()),
            counterparty=str(t.get("counterparty", "")).strip(), external_id="",
            balance_cents=_cents(t.get("balance")), row_index=i + 1,
        ))
    return out


def extract_pdf(content: bytes, client=None) -> AIResult:
    """Invia il PDF a Claude e restituisce i movimenti. `client` serve ai test."""
    if client is None:
        if not is_enabled():
            raise AIUnavailable("Lettura con AI non configurata: manca la variabile ANTHROPIC_API_KEY")
        import anthropic

        client = anthropic.Anthropic(timeout=600, max_retries=2)
    if len(content) > MAX_PDF_BYTES:
        raise AIExtractionError("Il PDF supera i 30 MB: dividilo in più file")
    model = model_name()
    import anthropic

    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=128000,  # estratti lunghi = molti movimenti in uscita; lo streaming evita i timeout
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # se il modello rifiuta, l'API riprova su un modello alternativo
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                    "data": base64.standard_b64encode(content).decode("ascii")}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise AIUnavailable("Chiave API di Anthropic non valida: controlla ANTHROPIC_API_KEY") from exc
    except anthropic.RateLimitError as exc:
        raise AIExtractionError("Limite di richieste raggiunto: riprova tra qualche minuto") from exc
    except anthropic.BadRequestError as exc:
        raise AIExtractionError(f"Richiesta rifiutata dall'API: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AIExtractionError(f"Errore del servizio Anthropic ({exc.status_code}): riprova più tardi") from exc
    except anthropic.APIConnectionError as exc:
        raise AIExtractionError("Connessione al servizio Anthropic non riuscita") from exc

    if message.stop_reason == "refusal":
        raise AIExtractionError("Il modello non ha elaborato il documento: usa l'export CSV/Excel della banca")
    if message.stop_reason == "max_tokens":
        raise AIExtractionError("L'estratto è troppo lungo per una sola lettura: dividilo per periodo (es. un mese per file)")
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AIExtractionError("Risposta non leggibile dal modello: riprova") from exc
    txs = to_parsed(data)
    if not txs:
        raise AIExtractionError("Nel documento non sono stati trovati movimenti")
    return AIResult(
        transactions=txs, bank_name=data.get("bank_name", ""), account_iban=data.get("account_iban", ""),
        currency=data.get("currency", ""), input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens, model=message.model,
    )


def estimated_cost_eur(result: AIResult) -> float | None:
    """Stima indicativa (prezzi di listino Claude Opus 5.5: 4 $/MTok input, 20 $/MTok output; 1 $ ≈ 0,92 €)."""
    if not result.model.startswith("claude-opus-5-5"):
        return None
    usd = result.input_tokens * 4 / 1_000_000 + result.output_tokens * 20 / 1_000_000
    return round(usd * 0.92, 2)
