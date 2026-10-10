"""Categorizzazione automatica dei movimenti in base a descrizione e controparte.

Funziona a regole "parola chiave -> categoria": ci sono regole di base per le spese aziendali
più comuni e le regole che nascono quando correggi a mano una categoria (valgono di più).
I movimenti categorizzati a mano non vengono mai ricalcolati."""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, CategoryRule, Transaction
from app.services import merchant

RULES_VERSION = "3"  # aumentarlo quando cambia la logica: all'avvio le categorie automatiche vengono ricalcolate
INTERNAL = "Giroconti"  # trasferimenti tra conti propri: esclusi dall'analisi
OTHER = "Altro"
INCOME = "Entrate"
SUBCONTRACT = "Subappalti e conto terzi"
GENERIC = "Fornitori"  # categoria generica: per i bonifici vince la regola dei subappalti

# Bonifici in uscita: "Bonifico SEPA a favore di…", "Bon. istantaneo", "SEPA Credit Transfer", Revolut "To Mario Rossi"
_TRANSFER = re.compile(
    r"\bbon\.|\b(bonifico|bonifici|bonif|sepa credit transfer|credit transfer|transfer to|"
    r"disposizione di pagamento|trasferimento a favore)\b|^\s*to\s+\S",
    re.I,
)

# (categoria, parole chiave). L'ordine conta: la prima regola che corrisponde vince.
DEFAULT_RULES: list[tuple[str, list[str]]] = [
    (INTERNAL, ["giroconto", "giro conto", "trasferimento tra conti", "trasferimento interno", "top-up", "top up",
                "ricarica carta", "ricarica prepagata", "exchanged to", "money added from", "to eur pocket"]),
    ("Tasse e contributi", ["f24", "agenzia entrate", "agenzia delle entrate", "inps", "inail", "tributi", "imposta sostitutiva",
                            "iva periodica", "ires", "irap", "pagopa", "camera di commercio", "diritto annuale"]),
    ("Stipendi e collaboratori", ["stipendio", "stipendi", "emolumento", "emolumenti", "salario", "cedolino", "retribuzione",
                                  "compenso amministratore", "tfr", "rimborso spese dipendente"]),
    ("Consulenze e professionisti", ["commercialist", "notai", "avvocat", "consulenz", "consulent", "studio legale",
                                     "studio associato", "parcella", "onorario"]),
    (SUBCONTRACT, ["subappalt", "conto terzi", "c.terzi", "manodopera", "stato avanzamento lavori", "prestazione occasionale"]),
    ("Pubblicità e marketing", ["facebk", "facebook", "meta ads", "meta platforms", "instagram", "google ads", "googleads",
                                "linkedin", "tiktok", "pinterest", "mailchimp", "semrush", "hubspot", "sponsor"]),
    ("Software e abbonamenti", ["google", "gsuite", "workspace", "microsoft", "office 365", "adobe", "aws", "amazon web services",
                                "notion", "slack", "zoom", "canva", "openai", "chatgpt", "anthropic", "claude.ai", "github",
                                "atlassian", "dropbox", "figma", "apple.com", "icloud", "shopify", "wix", "squarespace",
                                "aruba", "register.it", "godaddy", "render.com", "vercel", "stripe fee", "fatture in cloud",
                                "spotify", "netflix", "subscription", "abbonamento"]),
    ("Utenze e telefonia", ["enel", "a2a", "hera", "iren", "edison", "eni plenitude", "sorgenia", "acea", "tim ", "telecom",
                            "vodafone", "windtre", "wind tre", "iliad", "fastweb", "ho mobile", "kena", "very mobile", "bolletta",
                            "utenze", "utenza", "energia elettrica", "luce", "gas", "acquedotto"]),
    ("Affitto e sede", ["affitto", "locazione", "canone locazione", "condominio", "coworking", "regus", "spazio ufficio"]),
    ("Assicurazioni", ["assicuraz", "polizza", "unipol", "generali", "allianz", "axa", "zurich", "reale mutua", "cattolica",
                       "groupama", "prima.it", "verti"]),
    ("Carburante e trasporti", ["telepass", "autostrade", "autostrada", "pedaggio", "eni station", "enilive", "q8", "esso",
                                "tamoil", "agip", "ip ", "api ", "shell", "carburante", "benzina", "trenitalia", "italo",
                                "uber", "free now", "freenow", "taxi", "itabus", "flixbus", "parcheggio", "easypark",
                                "ryanair", "easyjet", "ita airways", "wizz", "vueling", "lufthansa", "noleggio", "hertz",
                                "europcar", "avis", "sixt", "car2go", "enjoy", "share now"]),
    ("Viaggi e alloggi", ["hotel", "booking.com", "airbnb", "expedia", "trivago", "b&b", "albergo"]),
    ("Ristoranti e bar", ["ristorante", "trattoria", "osteria", "pizzeria", "bar ", "caffe", "caffè", "pasticceria",
                          "mcdonald", "burger king", "autogrill", "deliveroo", "just eat", "justeat", "glovo", "starbucks",
                          "sushi", "bistrot", "enoteca"]),
    ("Acquisti e forniture", ["amazon", "amzn", "mediaworld", "unieuro", "euronics", "ikea", "leroy merlin", "brico",
                              "esselunga", "coop", "conad", "carrefour", "lidl", "eurospin", "cancelleria", "ebay"]),
    ("Banca e commissioni", ["commissione", "commissioni", "canone conto", "spese tenuta", "imposta di bollo", "bollo",
                             "interessi passivi", "competenze", "fee", "spese bonifico", "canone mensile"]),
    ("Prelievi contanti", ["prelievo", "atm", "bancomat", "cash withdrawal"]),
    (GENERIC, ["fattura", "fatt.", "fatt ", "ft.", "saldo fattura", "acconto fattura"]),
]
CATEGORIES = [c for c, _ in DEFAULT_RULES] + [OTHER]


def is_transfer(description: str, counterparty: str = "") -> bool:
    return bool(_TRANSFER.search(description or "") or _TRANSFER.search(counterparty or ""))


def norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9àèéìòù&.]+", " ", (text or "").lower())).strip() + " "


def merchant_name(tx: Transaction) -> str:
    """Nome dell'esercente/controparte ricavato dalla descrizione (vedi services/merchant.py)."""
    return merchant.merchant_name(tx.description or "", tx.counterparty or "")


def merchant_key(tx: Transaction) -> str:
    """Chiave del gruppo di spese: stessa chiave = stesso esercente. Usata anche per creare le regole."""
    return merchant.merchant_key(tx.description or "", tx.counterparty or "")


def seed_rules(session: Session) -> None:
    """Aggiunge le regole di base mancanti (anche quelle introdotte da aggiornamenti dell'app).
    Non tocca le tue regole né quelle di base che hai ridefinito."""
    existing = set(session.scalars(select(CategoryRule.pattern)))
    added = False
    for prio, (category, words) in enumerate(DEFAULT_RULES):
        for w in words:
            p = w.strip().lower()
            if p not in existing:
                session.add(CategoryRule(pattern=p, category=category, priority=100 + prio, source="base"))
                existing.add(p)
                added = True
    if added:
        session.commit()


@dataclass
class Categorizer:
    # (pattern, categoria, parole) in ordine di valutazione; con "parole" basta che ci siano tutte, in qualsiasi punto
    rules: list[tuple[str, str, frozenset[str] | None]]
    own_name: frozenset[str] = frozenset()  # parole della ragione sociale: un bonifico a noi stessi è un giroconto

    @classmethod
    def load(cls, session: Session) -> "Categorizer":
        from app.services.util import get_setting

        own = frozenset(w for w in norm(get_setting(session, "company_name")).split() if len(w) > 2)
        rules = []
        for r in session.scalars(select(CategoryRule).order_by(CategoryRule.priority, CategoryRule.id)):
            p = r.pattern.strip().lower()
            if r.source in ("tua", "ai"):
                rules.append((p, r.category, frozenset(norm(p).split())))
            else:
                # la parola chiave deve iniziare una parola del testo ("esso" non scatta dentro "presso"),
                # le più corte devono coincidere con una parola intera ("bar" non scatta in "barbieri")
                rules.append((f" {p} " if len(p) <= 3 else f" {p}", r.category, None))
        return cls(rules, own)

    def category_for(self, description: str, counterparty: str, amount_cents: int) -> str:
        # testo originale + nome dell'esercente riconosciuto (es. "AMZN Mktp" -> "Amazon")
        text = norm(f"{counterparty} {description} {merchant.merchant_name(description, counterparty)}")
        words = set(text.split())
        found = None
        for pattern, category, needed in self.rules:
            if (needed <= words) if needed else (pattern in text):
                found = category
                break
        if found and amount_cents > 0 and found != INTERNAL:
            return INCOME
        if amount_cents > 0:
            return found or INCOME
        if (found is None or found == GENERIC) and is_transfer(description, counterparty):
            # Regola base: i bonifici a persone e ditte (Mario Rossi, Edilizia Bianchi…) sono subappalti e lavori
            # conto terzi. Le categorie specifiche (tasse, stipendi, consulenze, utenze…) e le tue regole vincono.
            if self.own_name and self.own_name <= words:
                return INTERNAL
            return SUBCONTRACT
        return found or OTHER


def apply(session: Session, txs: list[Transaction] | None = None, cat: Categorizer | None = None) -> int:
    """Assegna la categoria ai movimenti non categorizzati a mano. Restituisce quanti sono cambiati."""
    cat = cat or Categorizer.load(session)
    if txs is None:
        txs = list(session.scalars(select(Transaction).where(Transaction.category_source != "manuale")))
    changed = 0
    for tx in txs:
        if tx.category_source == "manuale":
            continue
        new = cat.category_for(tx.description, tx.counterparty, tx.amount_cents)
        if new != tx.category or tx.category_source != "auto":
            tx.category, tx.category_source = new, "auto"
            changed += 1
    return changed


def learn(session: Session, tx: Transaction, category: str) -> tuple[str, int]:
    """Correzione manuale: fissa la categoria del movimento e crea una regola per quelli simili.
    Restituisce (parola chiave usata, movimenti aggiornati)."""
    tx.category, tx.category_source = category, "manuale"
    key = merchant_key(tx)
    updated = 0
    if key and category:
        rule = session.scalar(select(CategoryRule).where(CategoryRule.pattern == key))
        if rule:
            rule.category, rule.priority, rule.source = category, 10, "tua"
        else:
            session.add(CategoryRule(pattern=key, category=category, priority=10, source="tua"))
        session.flush()
        updated = apply(session)
    session.commit()
    return key, updated


# ---------- analisi ----------

@dataclass
class CategoryTotal:
    category: str
    total: int  # centesimi, positivo = speso
    count: int
    share: float = 0.0
    merchants: list[tuple[str, int, int]] = field(default_factory=list)  # (nome, totale, n°)


@dataclass
class SpendingReport:
    start: date
    end: date
    total: int
    income: int
    internal: int
    categories: list[CategoryTotal]
    top_merchants: list[tuple[str, str, int, int]]  # (nome, categoria, totale, n°)
    uncategorized_share: float


def spending_report(session: Session, start: date, end: date, account_ids: list[int] | None = None) -> SpendingReport:
    q = select(Transaction).join(Account).where(Transaction.date >= start, Transaction.date <= end, Account.currency == "EUR")
    if account_ids:
        q = q.where(Transaction.account_id.in_(account_ids))
    txs = list(session.scalars(q))
    if any(t.category_source == "" for t in txs):
        apply(session, [t for t in txs if t.category_source == ""])
        session.commit()
    by_cat: dict[str, list[Transaction]] = defaultdict(list)
    income = internal = 0
    for t in txs:
        if t.category == INTERNAL:
            internal += abs(t.amount_cents)
        elif t.amount_cents > 0:
            income += t.amount_cents
        else:
            by_cat[t.category or OTHER].append(t)
    total = sum(-t.amount_cents for ts in by_cat.values() for t in ts)
    cats = []
    merchants_all: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for category, ts in by_cat.items():
        m: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for t in ts:
            name = merchant_name(t) or "—"
            m[name][0] += -t.amount_cents
            m[name][1] += 1
            merchants_all[(name, category)][0] += -t.amount_cents
            merchants_all[(name, category)][1] += 1
        ct = CategoryTotal(category, sum(-t.amount_cents for t in ts), len(ts))
        ct.share = ct.total / total if total else 0
        ct.merchants = sorted(((n, v[0], v[1]) for n, v in m.items()), key=lambda x: -x[1])[:5]
        cats.append(ct)
    cats.sort(key=lambda c: -c.total)
    top = sorted(((n, c, v[0], v[1]) for (n, c), v in merchants_all.items()), key=lambda x: -x[2])[:10]
    other = next((c.total for c in cats if c.category == OTHER), 0)
    return SpendingReport(start, end, total, income, internal, cats, top, other / total if total else 0)


@dataclass
class Cluster:
    key: str
    name: str
    total: int
    count: int
    example: str


def uncategorized_clusters(session: Session, start: date | None = None, end: date | None = None, limit: int = 20) -> list[Cluster]:
    """Gruppi di uscite finite in "Altro" (assegnate in automatico), per esercente, dai più costosi."""
    q = select(Transaction).where(Transaction.category == OTHER, Transaction.category_source != "manuale", Transaction.amount_cents < 0)
    if start:
        q = q.where(Transaction.date >= start)
    if end:
        q = q.where(Transaction.date <= end)
    groups: dict[str, Cluster] = {}
    for t in session.scalars(q):
        key = merchant_key(t)
        if not key:
            continue
        c = groups.get(key)
        if c is None:
            c = groups[key] = Cluster(key, merchant_name(t), 0, 0, (t.description or t.counterparty)[:90])
        c.total += -t.amount_cents
        c.count += 1
    return sorted(groups.values(), key=lambda c: -c.total)[:limit]


def assign_cluster(session: Session, key: str, category: str, source: str = "tua") -> int:
    """Assegna una categoria a tutto un gruppo di esercente creando (o aggiornando) la regola."""
    key = " ".join(key.lower().split())
    rule = session.scalar(select(CategoryRule).where(CategoryRule.pattern == key))
    priority = 10 if source == "tua" else 50  # le tue regole valgono più di quelle proposte dall'AI
    if rule:
        if rule.source == "tua" and source == "ai":
            return 0  # non sovrascrivo una tua scelta
        rule.category, rule.priority, rule.source = category, priority, source
    else:
        session.add(CategoryRule(pattern=key, category=category, priority=priority, source=source))
    session.flush()
    return apply(session)
