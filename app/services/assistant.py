"""Assistente: una chat con Claude che conosce i dati dell'app e risponde a domande, con consigli.

Claude non vede il database in blocco: a ogni domanda decide quali dati gli servono e li chiede con degli
strumenti in SOLA LETTURA (panoramica, fatture, spese, previsione, scadenze, e una query SQL di sola lettura).
Le impostazioni e le connessioni bancarie (che contengono chiavi e token) non sono mai leggibili.
Richiede ANTHROPIC_API_KEY, come la lettura dei PDF; il modello si cambia con FBP_AI_MODEL."""
import json
import sqlite3
import time
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Base, ChatConversation
from app.services import categorize, invoice_match
from app.services.ai_extract import AIExtractionError, AIUnavailable, is_enabled, model_name
from app.services.util import fmt_eur, get_setting

MAX_TOOL_ROUNDS = 12
MAX_SQL_ROWS = 200
BLOCKED_TABLES = {"settings", "bank_connections", "chat_conversations"}  # chiavi, token e la chat stessa

SYSTEM = """Sei l'assistente finanziario e amministrativo di {company}, una piccola società italiana.
Hai accesso in sola lettura ai dati della sua app di gestione: conti e carte con saldi e movimenti bancari,
entrate e uscite previste, ricorrenze, fatture elettroniche emesse e ricevute (con il controllo incrociato dei
pagamenti), categorie di spesa e lo scadenzario dei documenti da consegnare al commercialista.

Come lavori:
- Rispondi in italiano, in modo diretto e concreto. Prima la risposta, poi i dettagli.
- Usa SEMPRE gli strumenti per leggere i dati prima di rispondere: non inventare numeri. Se un dato non c'è, dillo.
- Per le domande generiche parti da `panoramica`. Per tutto il resto usa lo strumento più specifico;
  `esegui_sql` quando serve una risposta che gli altri strumenti non danno.
- Importi in euro con il formato italiano (1.234,56 €). Date in formato gg/mm/aaaa.
- Quando dai consigli (liquidità, incassi da sollecitare, spese da ridurre, scadenze), basali sui dati che hai letto
  e spiega il perché in una riga. Segnala rischi concreti: saldo previsto sotto la soglia, fatture scadute, clienti che
  pagano in ritardo, spese in crescita, documenti per il commercialista in scadenza.
- Ricorda i limiti dei dati: le fatture anteriori ai movimenti importati non sono verificabili; le scadenze "stimate"
  sono calcolate dalla data fattura; i saldi dipendono dall'ultimo caricamento dei movimenti.
- Non dai consulenza fiscale o legale vincolante: per adempimenti e interpretazioni rimanda al commercialista.
- Formattazione semplice: paragrafi brevi, elenchi puntati, **grassetto** per le cifre chiave, tabelle markdown
  solo quando confronti più righe. Niente titoli lunghi.

Struttura del database (SQLite) per `esegui_sql`:
{schema}

Note sul database:
- Gli importi sono in CENTESIMI (colonne *_cents): dividi per 100. In transactions e planned_items il segno conta:
  negativo = uscita/pagamento, positivo = entrata/incasso.
- transactions: movimenti reali. category = categoria di spesa ("Giroconti" = trasferimenti tra conti propri, da escludere
  dalle analisi di spesa). planned_item_id = voce prevista/fattura che il movimento salda.
- planned_items: entrate/uscite previste. status: previsto | pagato | annullato. source: manuale | ricorrente | fattura.
  invoice_id collega alla fattura. estimated_date = 1 se la scadenza è stimata.
- invoices: direction "attiva" = emessa (da incassare), "passiva" = ricevuta (da pagare). doc_type TD04 = nota di credito,
  TD16-TD19 = autofatture reverse charge (nessun pagamento).
- Le date sono stringhe ISO (YYYY-MM-DD): usa strftime/substr per raggruppare per mese."""

TOOLS = [
    {
        "name": "panoramica",
        "description": "Situazione generale aggiornata: saldi di conti e carte, liquidità prevista a 30 e 90 giorni "
                       "(minimo, data del minimo, soglia), voci scadute, fatture da pagare e da incassare, periodo coperto "
                       "dai movimenti, ultimo caricamento, scadenze documenti per il commercialista. Usalo per primo per "
                       "domande generiche ('come stiamo?', 'cosa devo fare questa settimana?').",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "fatture",
        "description": "Elenco fatture con lo stato del pagamento ricavato dal controllo incrociato con i movimenti: "
                       "aperta (da pagare/incassare), pagata (movimento trovato), presunta (segnata pagata senza movimento), "
                       "compensata (nota di credito). Include scadenza, giorni di ritardo, residuo e movimento di pagamento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direzione": {"type": "string", "enum": ["ricevute", "emesse", "tutte"]},
                "stato": {"type": "string", "enum": ["aperta", "pagata", "presunta", "compensata", "tutte"]},
                "controparte": {"type": "string", "description": "Filtro per nome (anche parziale); stringa vuota = tutte"},
                "limite": {"type": "integer", "description": "Massimo numero di fatture (1-300)"},
            },
            "required": ["direzione", "stato", "controparte", "limite"],
            "additionalProperties": False,
        },
    },
    {
        "name": "analisi_spese",
        "description": "Spese per categoria in un periodo (giroconti ed entrate esclusi), con i principali fornitori/esercenti "
                       "di ogni categoria e la classifica generale dei fornitori.",
        "input_schema": {
            "type": "object",
            "properties": {
                "da": {"type": "string", "description": "Data iniziale YYYY-MM-DD"},
                "a": {"type": "string", "description": "Data finale YYYY-MM-DD"},
            },
            "required": ["da", "a"],
            "additionalProperties": False,
        },
    },
    {
        "name": "previsione_liquidita",
        "description": "Saldo previsto giorno per giorno da oggi per N giorni (solo i giorni con entrate/uscite previste), "
                       "con le voci di ogni giorno, il minimo del periodo e i giorni sotto la soglia minima.",
        "input_schema": {
            "type": "object",
            "properties": {"giorni": {"type": "integer", "description": "Orizzonte in giorni (1-365)"}},
            "required": ["giorni"],
            "additionalProperties": False,
        },
    },
    {
        "name": "scadenze_documenti",
        "description": "Documenti da consegnare al commercialista (scadenzario): cosa manca, per quale periodo, entro quando.",
        "input_schema": {
            "type": "object",
            "properties": {"entro_giorni": {"type": "integer", "description": "Scadenze entro N giorni (1-180)"}},
            "required": ["entro_giorni"],
            "additionalProperties": False,
        },
    },
    {
        "name": "esegui_sql",
        "description": "Esegue UNA query SQL di sola lettura (SELECT o WITH) sul database SQLite dell'app e restituisce "
                       f"al massimo {MAX_SQL_ROWS} righe. Usala per domande puntuali (es. movimenti di un fornitore, totali per "
                       "mese, confronti tra periodi). Ricorda: importi in centesimi.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]
for _t in TOOLS:
    _t["strict"] = True


def schema_text() -> str:
    lines = []
    for table in Base.metadata.sorted_tables:
        if table.name in BLOCKED_TABLES:
            continue
        cols = ", ".join(f"{c.name} {c.type.compile()}" for c in table.columns)
        lines.append(f"- {table.name}({cols})")
    return "\n".join(lines)


# ---------- strumenti ----------

def _eur(cents: int | None) -> str | None:
    return None if cents is None else fmt_eur(cents)


def tool_panoramica(session: Session) -> dict:
    from app.services.admin_docs import ensure_checklist, open_deadlines, period_label
    from app.services.forecast import account_statuses, build_forecast

    today = date.today()
    accounts = [
        {"conto": s.account.name, "tipo": s.account.kind, "valuta": s.account.currency, "saldo": _eur(s.balance),
         "nel_forecast": bool(s.account.include_in_forecast), "ultimo_movimento": s.last_tx_date and s.last_tx_date.isoformat()}
        for s in account_statuses(session, today)
    ]
    out = {"oggi": today.isoformat(), "conti": accounts}
    for days in (30, 90):
        fc = build_forecast(session, today, today + timedelta(days=days), today)
        out[f"previsione_{days}_giorni"] = {
            "saldo_oggi": _eur(fc.balance_today), "saldo_finale": _eur(fc.end_balance),
            "minimo": _eur(fc.min_balance), "data_minimo": fc.min_date and fc.min_date.isoformat(),
            "entrate_previste": _eur(fc.planned_in), "uscite_previste": _eur(fc.planned_out),
            "soglia_minima": _eur(fc.threshold),
            "giorni_sotto_soglia": sum(1 for d in fc.days if d.below_threshold),
        }
        if days == 30:
            out["voci_scadute_non_saldate"] = {"numero": len(fc.overdue), "totale": _eur(fc.overdue_total)}
    statuses = invoice_match.invoice_statuses(session)
    for direction, label in (("passiva", "fatture_da_pagare"), ("attiva", "fatture_da_incassare")):
        rows = [s for s in statuses if s.state == "aperta" and s.invoice.direction == direction]
        overdue = [s for s in rows if s.due and s.due < today]
        out[label] = {"numero": len(rows), "totale": _eur(sum(s.open_cents for s in rows)),
                      "scadute": len(overdue), "totale_scadute": _eur(sum(s.open_cents for s in overdue))}
    cov = invoice_match.coverage(session)
    out["movimenti_importati"] = {
        "periodo_coperto_da": cov.start and cov.start.isoformat(),
        "conti": [{"conto": n, "dal": a.isoformat(), "al": b.isoformat(), "movimenti": c} for n, a, b, c in cov.accounts],
        "ultimo_caricamento": get_setting(session, "last_upload_at") or None,
    }
    ensure_checklist(session, today)
    out["documenti_commercialista_in_scadenza"] = [
        {"documento": d.item.category.name, "periodo": period_label(d.item.period_key), "entro": d.item.due_date.isoformat(),
         "stato": d.item.status} for d in open_deadlines(session, today, within_days=20)
    ]
    return out


def tool_fatture(session: Session, direzione: str, stato: str, controparte: str, limite: int) -> dict:
    direction = {"ricevute": "passiva", "emesse": "attiva"}.get(direzione)
    rows = invoice_match.invoice_statuses(session, direction)
    if stato != "tutte":
        rows = [s for s in rows if s.state == stato]
    if controparte:
        rows = [s for s in rows if controparte.lower() in s.invoice.counterparty.lower()]
    rows.sort(key=lambda s: (s.due or s.invoice.date))
    total = len(rows)
    rows = rows[: max(1, min(limite, 300))]
    cov = invoice_match.coverage(session)
    return {
        "trovate": total, "mostrate": len(rows),
        "totale_residuo": _eur(sum(s.open_cents for s in rows if s.state == "aperta")),
        "periodo_coperto_dai_movimenti_da": cov.start and cov.start.isoformat(),
        "fatture": [
            {
                "numero": s.invoice.number, "data": s.invoice.date.isoformat(), "tipo": s.invoice.doc_type,
                "direzione": "emessa" if s.invoice.direction == "attiva" else "ricevuta",
                "controparte": s.invoice.counterparty, "totale": _eur(s.invoice.total_cents), "stato": s.state,
                "residuo": _eur(s.open_cents) if s.state == "aperta" else None,
                "scadenza": s.due and s.due.isoformat(), "giorni_ritardo": s.overdue_days or None,
                "scadenza_stimata": any(it.estimated_date for it in s.items) or None,
                "pagata_il": s.paid_date and s.paid_date.isoformat(),
                "movimenti": [f"{t.date.isoformat()} {t.account.name} {fmt_eur(t.amount_cents)} {t.description[:60]}"
                              for t in s.transactions] or None,
            }
            for s in rows
        ],
    }


def tool_analisi_spese(session: Session, da: str, a: str) -> dict:
    start, end = date.fromisoformat(da), date.fromisoformat(a)
    rep = categorize.spending_report(session, start, end)
    return {
        "periodo": f"{start.isoformat()} - {end.isoformat()}", "uscite_totali": _eur(rep.total), "entrate": _eur(rep.income),
        "quota_non_classificata": f"{rep.uncategorized_share * 100:.1f}%",
        "categorie": [{"categoria": c.category, "totale": _eur(c.total), "quota": f"{c.share * 100:.1f}%", "movimenti": c.count,
                       "principali": [f"{n}: {fmt_eur(t)} ({k})" for n, t, k in c.merchants]} for c in rep.categories],
        "fornitori_principali": [f"{n} [{c}]: {fmt_eur(t)} ({k} mov.)" for n, c, t, k in rep.top_merchants],
    }


def tool_previsione(session: Session, giorni: int) -> dict:
    from app.services.forecast import build_forecast

    today = date.today()
    fc = build_forecast(session, today, today + timedelta(days=max(1, min(giorni, 365))), today)
    days = []
    for d in fc.days:
        if d.items or d.below_threshold:
            days.append({"data": d.date.isoformat(), "saldo_previsto": _eur(d.projected), "sotto_soglia": d.below_threshold or None,
                         "voci": [f"{p.title}: {fmt_eur(p.amount_cents)}" for p in d.items][:15]})
    return {"saldo_oggi": _eur(fc.balance_today), "soglia": _eur(fc.threshold), "minimo": _eur(fc.min_balance),
            "data_minimo": fc.min_date and fc.min_date.isoformat(), "saldo_finale": _eur(fc.end_balance),
            "voci_scadute_non_saldate": _eur(fc.overdue_total), "giorni": days[:120]}


def tool_scadenze(session: Session, entro_giorni: int) -> dict:
    from app.services.admin_docs import ensure_checklist, open_deadlines, period_label

    today = date.today()
    ensure_checklist(session, today)
    return {"scadenze": [
        {"documento": d.item.category.name, "periodo": period_label(d.item.period_key), "entro": d.item.due_date.isoformat(),
         "stato": d.item.status, "scaduto": d.item.due_date < today}
        for d in open_deadlines(session, today, within_days=max(1, min(entro_giorni, 180)))
    ]}


def tool_sql(session: Session, query: str) -> dict:
    q = query.strip().rstrip(";").strip()
    if not q.lower().startswith(("select", "with")) or ";" in q:
        return {"errore": "Solo una singola query SELECT/WITH di lettura."}
    path = session.get_bind().url.database
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        def authorizer(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_READ and arg1 in BLOCKED_TABLES:
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        deadline = time.monotonic() + 5
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cur = conn.execute(q)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(MAX_SQL_ROWS + 1)
    except sqlite3.Error as exc:
        return {"errore": f"{exc}"}
    finally:
        conn.close()
    return {"colonne": cols, "righe": [list(r) for r in rows[:MAX_SQL_ROWS]], "troncato": len(rows) > MAX_SQL_ROWS}


def run_tool(session: Session, name: str, args: dict) -> str:
    try:
        if name == "panoramica":
            out = tool_panoramica(session)
        elif name == "fatture":
            out = tool_fatture(session, **args)
        elif name == "analisi_spese":
            out = tool_analisi_spese(session, **args)
        elif name == "previsione_liquidita":
            out = tool_previsione(session, **args)
        elif name == "scadenze_documenti":
            out = tool_scadenze(session, **args)
        elif name == "esegui_sql":
            out = tool_sql(session, **args)
        else:
            out = {"errore": f"strumento sconosciuto: {name}"}
    except (TypeError, ValueError) as exc:
        out = {"errore": f"parametri non validi: {exc}"}
    return json.dumps(out, ensure_ascii=False, default=str)


# ---------- conversazione ----------

def _block_dict(block) -> dict:
    return block.to_dict() if hasattr(block, "to_dict") else dict(block)


def _after_fallback(blocks: list) -> list:
    """Se un modello ha rifiutato e un altro ha proseguito (blocco "fallback"), i blocchi di ragionamento e le chiamate
    agli strumenti precedenti al passaggio non vanno rimandati né eseguiti."""
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    if last < 0:
        return blocks
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(blocks) if i > last or (b.type not in drop and b.type != "fallback")]


def ask(session: Session, conv: ChatConversation, question: str, client=None) -> str:
    """Aggiunge la domanda alla conversazione, fa lavorare Claude con gli strumenti e restituisce la risposta."""
    if client is None:
        if not is_enabled():
            raise AIUnavailable("Assistente non configurato: manca la variabile ANTHROPIC_API_KEY")
        import anthropic

        client = anthropic.Anthropic(timeout=300, max_retries=2)
    import anthropic

    messages = json.loads(conv.api_messages or "[]")
    display = json.loads(conv.display or "[]")
    # la data va nel messaggio (non nel prompt di sistema) così il prefisso resta in cache
    messages.append({"role": "user", "content": f"[Oggi è {date.today():%d/%m/%Y}]\n{question}"})
    display.append({"role": "user", "text": question})
    system = SYSTEM.format(company=get_setting(session, "company_name") or "l'azienda", schema=schema_text())
    answer, tools_used, usage_in, usage_out = "", [], 0, 0
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.beta.messages.create(
                model=model_name(),
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                system=system,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.AuthenticationError as exc:
            raise AIUnavailable("Chiave API di Anthropic non valida: controlla ANTHROPIC_API_KEY") from exc
        except anthropic.RateLimitError as exc:
            raise AIExtractionError("Troppe richieste al servizio Anthropic: riprova tra un minuto") from exc
        except anthropic.APIStatusError as exc:
            raise AIExtractionError(f"Errore del servizio Anthropic ({exc.status_code}): riprova più tardi") from exc
        except anthropic.APIConnectionError as exc:
            raise AIExtractionError("Connessione al servizio Anthropic non riuscita") from exc
        usage_in += getattr(response.usage, "input_tokens", 0) or 0
        usage_out += getattr(response.usage, "output_tokens", 0) or 0
        blocks = _after_fallback(list(response.content))
        messages.append({"role": "assistant", "content": [_block_dict(b) for b in blocks]})
        if response.stop_reason == "refusal":
            answer = "Non posso rispondere a questa domanda."
            break
        tool_uses = [b for b in blocks if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            answer = "".join(b.text for b in blocks if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                answer += "\n\n_(risposta interrotta perché troppo lunga: chiedi di riassumere)_"
            break
        results = []
        for tu in tool_uses:
            tools_used.append(tu.name)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": run_tool(session, tu.name, dict(tu.input))})
        messages.append({"role": "user", "content": results})
    else:
        answer = "Non sono riuscito a completare l'analisi: prova a fare una domanda più specifica."
    usd = usage_in * 4 / 1_000_000 + usage_out * 20 / 1_000_000 if model_name().startswith("claude-opus-5-5") else None
    display.append({"role": "assistant", "text": answer or "(nessuna risposta)", "tools": sorted(set(tools_used)),
                    "cost_eur": round(usd * 0.92, 3) if usd is not None else None})
    conv.api_messages = json.dumps(messages, ensure_ascii=False, default=str)
    conv.display = json.dumps(display, ensure_ascii=False)
    if not conv.title:
        conv.title = question[:80]
    return answer


def conversations(session: Session) -> list[ChatConversation]:
    return list(session.scalars(select(ChatConversation).order_by(ChatConversation.updated_at.desc()).limit(30)))
