"""Analisi delle spese per categoria."""
import json
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, CategoryRule
from app.services import categorize
from app.services.util import add_months, month_bounds, month_label, parse_month
from app.web import redirect, render

router = APIRouter()

PERIODS = {
    "mese": "Mese in corso",
    "mese_prec": "Mese scorso",
    "3m": "Ultimi 3 mesi",
    "6m": "Ultimi 6 mesi",
    "12m": "Ultimi 12 mesi",
    "anno": "Anno in corso",
}


def period_bounds(periodo: str, mese: str, today: date) -> tuple[date, date, str]:
    ym = parse_month(mese)
    if ym:
        s, e = month_bounds(*ym)
        return s, e, month_label(*ym)
    first = today.replace(day=1)
    if periodo == "mese_prec":
        prev = add_months(first, -1)
        s, e = month_bounds(prev.year, prev.month)
        return s, e, month_label(prev.year, prev.month)
    if periodo in ("3m", "6m", "12m"):
        n = int(periodo[:-1])
        return add_months(first, -(n - 1)), today, PERIODS[periodo]
    if periodo == "anno":
        return date(today.year, 1, 1), today, f"Anno {today.year}"
    return first, today, month_label(today.year, today.month)


@router.get("/analisi")
def analysis(request: Request, periodo: str = "3m", mese: str = "", conto: int = 0, session: Session = Depends(get_session)):
    today = date.today()
    start, end, title = period_bounds(periodo, mese, today)
    report = categorize.spending_report(session, start, end, [conto] if conto else None)
    chart = json.dumps({
        "labels": [c.category for c in report.categories],
        "values": [round(c.total / 100, 2) for c in report.categories],
        "shares": [round(c.share * 100, 1) for c in report.categories],
        "counts": [c.count for c in report.categories],
    })
    accounts = list(session.scalars(select(Account).where(Account.active).order_by(Account.id)))
    my_rules = list(session.scalars(
        select(CategoryRule).where(CategoryRule.source.in_(["tua", "ai"])).order_by(CategoryRule.source.desc(), CategoryRule.category, CategoryRule.pattern)
    ))
    clusters = categorize.uncategorized_clusters(session, start, end)
    from app.services.ai_extract import is_enabled as ai_enabled
    base_rules = session.scalar(select(func.count()).select_from(CategoryRule).where(CategoryRule.source == "base"))
    return render(
        request, "analysis.html", report=report, chart=chart, title=title, periodo=periodo, mese=mese, conto=conto,
        periods=PERIODS, accounts=accounts, my_rules=my_rules, base_rules=base_rules,
        categories=categorize.CATEGORIES, da=start.isoformat(), a=end.isoformat(),
        clusters=clusters, ai_enabled=ai_enabled(), back=str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
    )


def _back(back: str) -> str:
    return back if back.startswith("/analisi") else "/analisi"


@router.post("/analisi/gruppo")
def assign_group(key: str = Form(...), category: str = Form(...), back: str = Form("/analisi"), session: Session = Depends(get_session)):
    category = category.strip()
    if not category:
        return redirect(_back(back), err="Scegli una categoria")
    n = categorize.assign_cluster(session, key, category)
    session.commit()
    return redirect(_back(back), msg=f"«{key}» → {category}: aggiornati {n} movimenti (vale anche per i prossimi)")


@router.post("/analisi/ai-classifica")
def ai_classify(back: str = Form("/analisi"), session: Session = Depends(get_session)):
    from app.services import ai_categorize
    from app.services.ai_extract import AIExtractionError, AIUnavailable

    clusters = categorize.uncategorized_clusters(session, limit=ai_categorize.MAX_CLUSTERS)
    if not clusters:
        return redirect(_back(back), msg="Non ci sono gruppi di spesa da classificare")
    try:
        suggestions = ai_categorize.suggest(session, clusters)
    except (AIUnavailable, AIExtractionError) as exc:
        return redirect(_back(back), err=str(exc))
    n = ai_categorize.apply_suggestions(session, suggestions)
    return redirect(
        _back(back),
        msg=f"Claude ha classificato {len(suggestions)} gruppi su {len(clusters)} (gli altri restano in \"Altro\"): aggiornati {n} movimenti. "
            "Puoi correggere o eliminare le regole proposte in fondo alla pagina.",
    )


@router.post("/analisi/regole")
def add_rule(pattern: str = Form(...), category: str = Form(...), session: Session = Depends(get_session)):
    pattern, category = " ".join(pattern.lower().split()), category.strip()
    if len(pattern) < 2 or not category:
        return redirect("/analisi", err="Indica una parola chiave (almeno 2 lettere) e una categoria")
    rule = session.scalar(select(CategoryRule).where(CategoryRule.pattern == pattern))
    if rule:
        rule.category, rule.priority, rule.source = category, 10, "tua"
    else:
        session.add(CategoryRule(pattern=pattern, category=category, priority=10, source="tua"))
    session.flush()
    n = categorize.apply(session)
    session.commit()
    return redirect("/analisi", msg=f"Regola «{pattern}» → {category} salvata: aggiornati {n} movimenti")


@router.post("/analisi/regole/{rule_id}/elimina")
def delete_rule(rule_id: int, session: Session = Depends(get_session)):
    rule = session.get(CategoryRule, rule_id)
    if rule:
        session.delete(rule)
        session.flush()
        categorize.apply(session)
        session.commit()
    return redirect("/analisi", msg="Regola eliminata")


@router.post("/analisi/ricalcola")
def recompute(session: Session = Depends(get_session)):
    n = categorize.apply(session)
    session.commit()
    return redirect("/analisi", msg=f"Categorie ricalcolate: {n} movimenti aggiornati (quelli corretti a mano restano invariati)")
