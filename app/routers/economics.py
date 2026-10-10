"""Dashboard KPI economici/finanziari e capacità di investimento."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import investment, kpi
from app.web import redirect, render

router = APIRouter()


@router.get("/kpi")
def kpi_page(request: Request, mesi: int = 12, session: Session = Depends(get_session)):
    mesi = mesi if mesi in (6, 12, 24) else 12
    rep = kpi.build(session, mesi)
    labels = [m.key for m in rep.months]
    eur = lambda vals: [v / 100 for v in vals]  # noqa: E731
    charts = {
        "economics": {"kind": "bar", "labelFormat": "month", "labels": labels, "title": "Ricavi e costi", "series": [
            {"label": "Ricavi", "values": eur(m.revenue for m in rep.months), "color": "--series-actual"},
            {"label": "Costi fornitori", "values": eur(m.external_costs for m in rep.months), "color": "--series-forecast"},
            {"label": "Personale", "values": eur(m.personnel for m in rep.months), "color": "--series-3"},
        ]},
        "ebitda": {"kind": "bar", "labelFormat": "month", "labels": labels, "title": "EBITDA mensile", "series": [
            {"label": "EBITDA", "values": eur(m.ebitda for m in rep.months), "color": "--series-actual"}]},
        "margin": {"kind": "line", "labelFormat": "month", "labels": labels, "valueFormat": "pct", "title": "Margine EBITDA", "series": [
            {"label": "Margine EBITDA", "values": [m.margin for m in rep.months], "color": "--series-actual"}]},
        "cashflow": {"kind": "bar", "labelFormat": "month", "labels": labels, "title": "Entrate e uscite di cassa", "stacked": True,
                     "series": [
            {"label": "Incassi di fatture", "values": eur(m.cash_in_invoices for m in rep.months), "color": "--series-actual", "stack": "in"},
            {"label": "Altre entrate", "values": eur(m.cash_in - m.cash_in_invoices for m in rep.months), "color": "--series-3", "stack": "in"},
            {"label": "Pagamenti di fatture", "values": eur(m.cash_out_invoices for m in rep.months), "color": "--series-forecast", "stack": "out"},
            {"label": "Altre uscite", "values": eur(m.cash_out - m.cash_out_invoices for m in rep.months), "color": "--series-4", "stack": "out"}]},
        "balance": {"kind": "line", "labelFormat": "month", "labels": labels, "title": "Liquidità a fine mese", "series": [
            {"label": "Liquidità", "values": [None if m.end_balance is None else m.end_balance / 100 for m in rep.months],
             "color": "--series-actual"}]},
    }
    return render(request, "kpi.html", rep=rep, charts=charts, mesi=mesi)


@router.get("/investimenti")
def investment_page(request: Request, mesi: int = 6, scenario: str = "base", session: Session = Depends(get_session)):
    mesi = mesi if mesi in (3, 6, 12) else 6
    scenario = scenario if scenario in investment.SCENARIOS else "base"
    plan = investment.build(session, mesi, scenario)
    chart_days = {
        "kind": "line", "labelFormat": "day", "labels": [d.date.isoformat() for d in plan.days],
        "title": "Liquidità prevista", "series": [{"label": "Liquidità prevista", "values": [d.balance / 100 for d in plan.days],
                                                   "color": "--series-actual"}],
        "reference": {"label": "Riserva di sicurezza", "value": plan.reserve / 100, "color": "--critical"},
    }
    chart_months = {
        "kind": "bar", "labelFormat": "month", "labels": [k for k, _, _ in plan.months], "title": "Entrate e uscite previste",
        "series": [{"label": "Entrate previste", "values": [i / 100 for _, i, _ in plan.months], "color": "--series-actual"},
                   {"label": "Uscite previste", "values": [o / 100 for _, _, o in plan.months], "color": "--series-forecast"}],
    }
    chart_alloc = {
        "kind": "bar", "horizontal": True, "labels": [a.label for a in plan.allocations], "title": "Ripartizione",
        "series": [{"label": "Budget", "values": [a.amount / 100 for a in plan.allocations], "color": "--series-actual"}],
    }
    return render(
        request, "investment.html", plan=plan, mesi=mesi, scenario=scenario, scenarios=investment.SCENARIOS,
        cfg=investment.settings(session), areas=investment.AREAS,
        chart_days=chart_days, chart_months=chart_months, chart_alloc=chart_alloc,
    )


@router.post("/investimenti/impostazioni")
async def investment_settings(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    investment.save_settings(session, {k: str(v) for k, v in form.items()})
    session.commit()
    back = f"/investimenti?mesi={form.get('mesi', 6)}&scenario={form.get('scenario', 'base')}"
    return redirect(back, msg="Impostazioni salvate")
