"""Cantieri: a quale cantiere appartiene ogni fattura, e l'EBITDA di ciascun cantiere.

Le fatture emesse si assegnano dal cliente; quelle ricevute dalle parole chiave del cantiere (via, città, cognome
del cliente...) trovate nella causale e nelle righe della fattura (dagli XML). Quello che le regole non capiscono
si può far leggere a Claude ("Assegna con AI") o assegnare a mano; le scelte manuali non vengono mai cambiate."""
import json
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Contract, Invoice, Project, Transaction
from app.services.fatturapa import CREDIT_NOTE_TYPES, SELF_INVOICE_TYPES
from app.services.invoice_match import name_matches

GENERAL = "generale"
LOCKED = ("manuale", GENERAL)  # scelte dell'utente: le regole e l'AI non le toccano
PERSONNEL = "Stipendi e collaboratori"


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9àèéìòù]+", " ", text.lower()) + " "


def keyword_hits(project: Project, text: str) -> int:
    t = _norm(text)
    return sum(1 for k in project.keyword_list if _norm(k) in t)


def _signed_taxable(inv: Invoice) -> int:
    base = inv.taxable_cents if inv.taxable_cents is not None else abs(inv.total_cents)
    return -base if inv.doc_type in CREDIT_NOTE_TYPES else base


def auto_assign(session: Session, invoices: list[Invoice] | None = None) -> int:
    """Regole: emesse dal cliente (e dalle parole chiave se il cliente ha più cantieri), ricevute dalle parole chiave.
    Assegna solo quando il cantiere è uno solo; non tocca le scelte manuali né quelle dell'AI."""
    projects = list(session.scalars(select(Project)))
    if not projects:
        return 0
    if invoices is None:
        invoices = list(session.scalars(select(Invoice).where(Invoice.project_source.in_(["", "auto"]))))
    changed = 0
    for inv in invoices:
        if inv.project_source not in ("", "auto"):
            continue
        text = f"{inv.description} {inv.counterparty if inv.direction == 'passiva' else ''}"
        if inv.direction == "attiva" and inv.doc_type not in SELF_INVOICE_TYPES:
            cands = [p for p in projects if p.client and name_matches(p.client, inv.counterparty)]
            if len(cands) > 1:
                scored = sorted(((keyword_hits(p, text), p) for p in cands), key=lambda x: -x[0])
                cands = [scored[0][1]] if scored[0][0] and (len(scored) == 1 or scored[1][0] < scored[0][0]) else []
        else:
            scored = [(keyword_hits(p, text), p) for p in projects]
            best = max((s for s, _ in scored), default=0)
            cands = [p for s, p in scored if s == best] if best else []
        target = cands[0] if len(cands) == 1 else None
        new_id, new_src = (target.id, "auto") if target else (None, "")
        if (inv.project_id, inv.project_source) != (new_id, new_src):
            inv.project_id, inv.project_source = new_id, new_src
            changed += 1
    session.flush()
    return changed


def client_keyword(name: str) -> str:
    """Parola che identifica il cliente nelle descrizioni: per le società la parola distintiva della ragione sociale,
    per le persone il cognome (ultima parola)."""
    from app.services.invoice_match import _LEGAL_FORM, _STOP, _tokens

    legal = {"societa", "responsabilita", "limitata", "semplificata", "srl", "srls", "spa", "snc", "sas", "coop",
             "cooperativa", "unipersonale", "ditta", "impresa"}
    words = [w for w in _tokens(name) if w not in _STOP and w not in legal and not w.isdigit()]
    if _LEGAL_FORM.search(name or "") or legal & set(_tokens(name)):
        return next((w for w in words if len(w) >= 3), "")
    return next((w for w in reversed(words) if len(w) >= 3), "")


def seed_from_clients(session: Session, year: int) -> int:
    """Un cantiere per ogni cliente con fatture nell'anno che non ne ha ancora uno (nome e parola chiave dal cliente),
    più i contratti attivi."""
    existing = list(session.scalars(select(Project)))
    created = 0

    def has(client: str) -> bool:
        return any(p.client and name_matches(p.client, client) for p in existing)

    for c in session.scalars(select(Contract)):
        if not has(c.client):
            kw = [client_keyword(c.client)]
            if c.title:
                kw += [w.strip() for w in re.split(r"[–,]", c.title) if len(w.strip()) >= 4 and "intern" not in w.lower()]
            p = Project(name=f"{c.client}" + (f" – {c.title}" if c.title else ""), client=c.client, keywords=", ".join(kw))
            session.add(p)
            existing.append(p)
            created += 1
    clients = session.scalars(select(Invoice.counterparty).where(
        Invoice.direction == "attiva", Invoice.date >= date(year, 1, 1)).distinct())
    for client in clients:
        if client and not has(client):
            p = Project(name=client, client=client, keywords=client_keyword(client))
            session.add(p)
            existing.append(p)
            created += 1
    session.flush()
    return created


# ---------- assegnazione con Claude ----------

AI_BATCH = 120
PROMPT = """Sei l'assistente amministrativo di un'impresa italiana di ristrutturazioni che lavora per cantieri.
Per ogni fattura qui sotto indica a quale cantiere appartiene, leggendo fornitore, causale e righe (indirizzi, nomi
dei clienti, lavori). Usa SOLO gli id dei cantieri elencati. Rispondi 0 se è una spesa generale dell'azienda (software,
telefono, commercialista, affitto, carburante, pubblicità...) e -1 se non riesci a capirlo con ragionevole certezza.

Cantieri:
{projects}

Fatture:
{invoices}"""


def ai_assign(session: Session, year: int, client=None) -> tuple[int, int]:
    """Fa leggere a Claude le fatture dell'anno ancora da assegnare. Restituisce (assegnate a un cantiere, generali)."""
    from app.services.ai_extract import AIExtractionError, AIUnavailable, is_enabled, model_name

    projects = list(session.scalars(select(Project).where(Project.active)))
    todo = [i for i in session.scalars(select(Invoice).where(
        Invoice.project_source == "", Invoice.date >= date(year, 1, 1)).order_by(Invoice.total_cents.desc()))]
    if not projects or not todo:
        return 0, 0
    if client is None:
        if not is_enabled():
            raise AIUnavailable("Assegnazione con AI non configurata: manca la variabile ANTHROPIC_API_KEY")
        import anthropic

        client = anthropic.Anthropic(timeout=600, max_retries=2)
    plist = "\n".join(f"id {p.id}: {p.name}" + (f" (cliente {p.client})" if p.client else "")
                      + (f" — parole chiave: {p.keywords}" if p.keywords else "") for p in projects)
    ids = {p.id for p in projects}
    schema = {"type": "object", "properties": {"assignments": {"type": "array", "items": {
        "type": "object", "properties": {"n": {"type": "integer"}, "project_id": {"type": "integer"}},
        "required": ["n", "project_id"], "additionalProperties": False}}},
        "required": ["assignments"], "additionalProperties": False}
    assigned = general = 0
    for start in range(0, len(todo), AI_BATCH):
        batch = todo[start:start + AI_BATCH]
        items = "\n".join(
            f"{n + 1}. {'emessa a' if i.direction == 'attiva' else 'ricevuta da'} {i.counterparty}, {i.date:%d/%m/%Y}, "
            f"{abs(i.total_cents) / 100:.2f} € — {(i.description or 'nessuna descrizione')[:500]}" for n, i in enumerate(batch))
        try:
            message = client.beta.messages.create(
                model=model_name(), max_tokens=16000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": PROMPT.format(projects=plist, invoices=items)}],
            )
        except Exception as exc:  # errori di rete/servizio: mostro un messaggio chiaro
            raise AIExtractionError(f"Errore del servizio Anthropic: {exc}") from exc
        if message.stop_reason == "refusal":
            raise AIExtractionError("Il modello non ha elaborato la richiesta")
        try:
            data = json.loads("".join(b.text for b in message.content if b.type == "text"))
        except ValueError as exc:
            raise AIExtractionError("Risposta non leggibile dal modello: riprova") from exc
        for a in data.get("assignments", []):
            n, pid = a.get("n"), a.get("project_id")
            if not isinstance(n, int) or not 1 <= n <= len(batch):
                continue
            inv = batch[n - 1]
            if pid in ids:
                inv.project_id, inv.project_source = pid, "ai"
                assigned += 1
            elif pid == 0:
                inv.project_id, inv.project_source = None, GENERAL
                general += 1
    session.flush()
    return assigned, general


# ---------- EBITDA per cantiere ----------

@dataclass
class ProjectRow:
    project: Project | None
    revenue: int = 0
    costs: int = 0
    n_in: int = 0
    n_out: int = 0

    @property
    def margin(self) -> int:
        return self.revenue - self.costs

    @property
    def margin_pct(self) -> float | None:
        return self.margin / self.revenue if self.revenue > 0 else None


@dataclass
class ProjectReport:
    year: int
    rows: list[ProjectRow] = field(default_factory=list)
    general_costs: int = 0  # spese generali (assegnate come tali)
    unassigned_costs: int = 0  # fatture ricevute non ancora assegnate
    unassigned_revenue: int = 0
    n_unassigned: int = 0
    personnel: int = 0
    without_description: int = 0  # fatture ricevute senza causale/righe (caricate solo dal report Excel)
    n_invoices: int = 0

    @property
    def projects_margin(self) -> int:
        return sum(r.margin for r in self.rows)

    @property
    def ebitda(self) -> int:
        return self.projects_margin + self.unassigned_revenue - self.general_costs - self.unassigned_costs - self.personnel

    @property
    def assigned_share(self) -> float | None:
        costs = sum(r.costs for r in self.rows) + self.general_costs + self.unassigned_costs
        return 1 - self.unassigned_costs / costs if costs else None


def report(session: Session, year: int) -> ProjectReport:
    """Stessi conti dei KPI (imponibile, note di credito sottratte, cespiti e acconti esclusi fuori), divisi per cantiere."""
    from app.services.kpi import COST_EXCLUDED

    rep = ProjectReport(year)
    by_id: dict[int, ProjectRow] = {}
    invs = session.scalars(select(Invoice).where(Invoice.date >= date(year, 1, 1), Invoice.date <= date(year, 12, 31)))
    for inv in invs:
        rep.n_invoices += 1
        amount = _signed_taxable(inv)
        is_revenue = inv.direction == "attiva" and inv.doc_type not in SELF_INVOICE_TYPES
        if not is_revenue and inv.cost_treatment in COST_EXCLUDED:
            continue
        if not is_revenue and not inv.description:
            rep.without_description += 1
        if inv.project_id:
            row = by_id.get(inv.project_id)
            if row is None:
                row = by_id[inv.project_id] = ProjectRow(inv.project)
            if is_revenue:
                row.revenue += amount
                row.n_in += 1
            else:
                row.costs += amount
                row.n_out += 1
        elif is_revenue:
            rep.unassigned_revenue += amount
        elif inv.project_source == GENERAL:
            rep.general_costs += amount
        else:
            rep.unassigned_costs += amount
            rep.n_unassigned += 1
    rep.personnel = sum(-a for (a,) in session.execute(select(Transaction.amount_cents).where(
        Transaction.date >= date(year, 1, 1), Transaction.date <= date(year, 12, 31),
        Transaction.category == PERSONNEL, Transaction.amount_cents < 0)))
    rep.rows = sorted(by_id.values(), key=lambda r: -r.revenue)
    return rep
