"""Dal testo grezzo del movimento al nome dell'esercente/controparte.

Le descrizioni bancarie sono piene di parti che non dicono nulla sulla spesa: tipo di operazione,
numero di carta, data e ora, mandati SDD, codici di riferimento, circuiti di pagamento, città.
Qui le togliamo e riconosciamo i marchi più comuni, così che "AMZN Mktp IT*1A2B3", "AMAZON EU SARL"
e "Amazon.it" diventino tutti "Amazon" e finiscano nello stesso gruppo."""
import re

# Marchi noti: (nome canonico, espressione). L'ordine conta: i più specifici prima.
BRANDS: list[tuple[str, str]] = [
    ("Amazon Web Services", r"\baws\b|amazon web services"),
    ("Amazon Prime", r"amazon prime|prime video|amzn prime"),
    ("Amazon", r"\bamzn\b|\bamazon\b"),
    ("Google Ads", r"google\s*\*?\s*ads|googleads|adwords"),
    ("Google Workspace", r"google\s*\*?\s*(g ?suite|workspace)|gsuite"),
    ("Google Cloud", r"google\s*\*?\s*cloud"),
    ("Google", r"\bgoogle\b"),
    ("Meta Ads", r"facebk|\bfacebook\b|\bfb\b ads|meta platforms|meta ads|instagram"),
    ("Microsoft", r"microsoft|\bmsft\b|office ?365|\bazure\b"),
    ("Apple", r"apple\.com|itunes|\bapple\b"),
    ("Adobe", r"\badobe\b"),
    ("Notion", r"\bnotion\b"),
    ("Slack", r"\bslack\b"),
    ("Zoom", r"\bzoom\.?us\b|\bzoom\b"),
    ("Canva", r"\bcanva\b"),
    ("OpenAI", r"openai|chatgpt"),
    ("Anthropic", r"anthropic|claude\.ai"),
    ("GitHub", r"github"),
    ("Dropbox", r"dropbox"),
    ("Figma", r"\bfigma\b"),
    ("LinkedIn", r"linkedin"),
    ("Spotify", r"spotify"),
    ("Netflix", r"netflix"),
    ("Shopify", r"shopify"),
    ("Aruba", r"\baruba\b"),
    ("Render", r"render\.com"),
    ("Revolut", r"\brevolut\b"),
    ("Telepass", r"telepass"),
    ("Autostrade", r"autostrad"),
    ("Trenitalia", r"trenitalia"),
    ("Italo", r"italo ?treno|\bitalo\b|\bntv\b"),
    ("Uber", r"\buber\b"),
    ("FreeNow", r"free ?now"),
    ("Ryanair", r"ryanair"),
    ("easyJet", r"easyjet"),
    ("ITA Airways", r"ita airways"),
    ("Booking.com", r"booking\.com|\bbooking\b"),
    ("Airbnb", r"airbnb"),
    ("Eni", r"\beni\b|enilive|eni station"),
    ("Q8", r"\bq8\b"),
    ("Esso", r"\besso\b"),
    ("Tamoil", r"tamoil"),
    ("Enel", r"\benel\b"),
    ("TIM", r"\btim\b|telecom italia"),
    ("Vodafone", r"vodafone"),
    ("WindTre", r"wind ?tre|windtre"),
    ("Iliad", r"\biliad\b"),
    ("Fastweb", r"fastweb"),
    ("Esselunga", r"esselunga"),
    ("Coop", r"\bcoop\b"),
    ("Conad", r"\bconad\b"),
    ("Carrefour", r"carrefour"),
    ("Lidl", r"\blidl\b"),
    ("IKEA", r"\bikea\b"),
    ("MediaWorld", r"media ?world"),
    ("Autogrill", r"autogrill"),
    ("McDonald's", r"mc ?donald"),
    ("Starbucks", r"starbucks"),
    ("Deliveroo", r"deliveroo"),
    ("Just Eat", r"just ?eat"),
    ("Glovo", r"\bglovo\b"),
    ("Agenzia delle Entrate", r"agenzia (delle )?entrate|\bf24\b|delega unificata"),
    ("INPS", r"\binps\b"),
    ("INAIL", r"\binail\b"),
]
_BRANDS = [(name, re.compile(p, re.I)) for name, p in BRANDS]

# Circuiti/intermediari che precedono il vero esercente: "PAYPAL *NETFLIX", "SUMUP *BAR ROMA"
_PROCESSOR = re.compile(r"\b(paypal|pp|sumup|sum up|sq|zettle|izettle|nexi|satispay|stripe|ppro|cbi)\s*\*\s*", re.I)

# Parti tipiche delle descrizioni bancarie italiane e delle app (Revolut, Vivid) prive di significato
_BOILERPLATE = [
    r"pagamento\s+(pos|carta|contactless|apple ?pay|google ?pay|mobile)",
    r"pagamenti?\s+(diversi|su pos)",
    r"operazione\s+(carta|pos|n\.?\s*\S+)",
    r"acquisto\s+(pos|carta|online|con carta)",
    r"\b(carta|card)\s*(n\.?|nr\.?|num\.?|no\.?)?\s*[\*x\d][\*x\d\s]{3,}",
    r"\*{2,}\s*\d+",
    r"\b(del|il|data)\s+\d{1,2}[/.\-]\d{1,2}([/.\-]\d{2,4})?",
    r"\bore\s+\d{1,2}[:.]\d{2}([:.]\d{2})?",
    r"\d{1,2}[:.]\d{2}([:.]\d{2})?",
    r"addebito\s+(diretto\s+)?(sdd|sepa)(\s+(core|b2b))?",
    r"\b(mandato|mand\.?|cid|cod\.?\s*id\.?|id\s*creditore)\s*[:.]?\s*\S+",
    r"\b(bonifico|bon\.?|bonif\.?)(\s+sepa)?(\s+istantaneo)?(\s+(a favore di|a vostro favore|a favore|da|disposto a|ordinante|per))?",
    r"\bvs\.?\s+disposizione",
    r"\b(disposizione|disp\.?)\s+(di|a)\b",
    r"\b(trn|cro|ref|rif|riferimento|id operazione|ordine|order|transaction id|tx)\b\s*[:.]?\s*[a-z0-9\-/]*\d[a-z0-9\-/]*",
    r"\bmcc\s*\d+",
    r"\b[a-z]{2}\d{2}[a-z0-9]{11,30}\b",  # IBAN
    r"\b(card payment|card purchase|payment to|transfer to|transfer from|top-up|exchanged to)\b",
    r"^(to|from|a|da|beneficiario|ordinante|benef\.?)\s+",
    r"\b(causale|descrizione|motivo)\s*[:.]",
    r"\b(contactless|online|e-commerce|ecommerce|internet|web)\b",
    r"\b(presso|c/o|pos|atm|esercente|merchant)\b",
    r"\bwww\.|\.(com|it|eu|net|io)\b",
    r"\b(eur|euro|usd|gbp|chf)\b",
]
_BOILERPLATE_RE = [re.compile(p, re.I) for p in _BOILERPLATE]

_LEGAL = re.compile(r"\b(s\.?r\.?l\.?s?|s\.?p\.?a\.?|s\.?a\.?s\.?|s\.?n\.?c\.?|sarl|s\.?a\.?|ltd|limited|inc|llc|gmbh|b\.?v\.?|ag|plc|soc\.? coop\.?|unipersonale)\b\.?", re.I)
_COUNTRY = re.compile(r"\b(ita|it|irl|ie|lux|lu|nld|nl|deu|de|fra|fr|esp|es|usa|us|gbr|gb|che|ch|aut|at|bel|be)\b", re.I)
_CITIES = re.compile(
    r"\b(roma|rome|milano|milan|napoli|torino|firenze|bologna|genova|venezia|verona|bari|palermo|catania|padova|"
    r"trieste|brescia|bergamo|modena|parma|monterotondo|fiumicino|guidonia|tivoli|frascati|latina|dublin|dublino|"
    r"luxembourg|lussemburgo|amsterdam|london|londra|berlin|berlino|paris|parigi|madrid|barcelona|san francisco|seattle)\b",
    re.I,
)
_NUMBERS = re.compile(r"\b[a-z]*\d[\w\-/.]*\b", re.I)  # codici e numeri (anche alfanumerici con cifre)


def brand_of(text: str) -> str | None:
    for name, rx in _BRANDS:
        if rx.search(text):
            return name
    return None


def clean_text(description: str, counterparty: str = "") -> str:
    """Testo ripulito (minuscolo), utile anche per le regole."""
    text = f"{counterparty} {description}" if counterparty and counterparty.lower() not in description.lower() else description or counterparty
    text = _PROCESSOR.sub(" ", text or "")
    for rx in _BOILERPLATE_RE:
        text = rx.sub(" ", text)
    text = _LEGAL.sub(" ", text)
    text = _NUMBERS.sub(" ", text)
    text = re.sub(r"[^\w&' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def merchant_name(description: str, counterparty: str = "") -> str:
    """Nome leggibile dell'esercente/controparte (es. "Amazon", "Ferramenta Bianchi")."""
    raw = f"{counterparty} {description}"
    brand = brand_of(raw)
    if brand:
        return brand
    # la controparte, se c'è, è già il nome giusto: la pulisco poco
    source = counterparty or description
    text = clean_text(source)
    words = [w for w in text.split() if len(w) > 1]
    # città e sigle di paese in coda non distinguono l'esercente
    while len(words) > 1 and (_CITIES.fullmatch(words[-1]) or _COUNTRY.fullmatch(words[-1])):
        words.pop()
    words = [w for w in words if not (_COUNTRY.fullmatch(w) and len(words) > 1)]
    name = " ".join(words[:3])
    return name.title() if name else (description or counterparty or "").strip()[:30]


def merchant_key(description: str, counterparty: str = "") -> str:
    return merchant_name(description, counterparty).lower()
