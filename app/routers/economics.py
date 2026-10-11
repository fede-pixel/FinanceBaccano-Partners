"""Dashboard KPI economici/finanziari e capacità di investimento."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import investment, kpi
from app.web import redirect, render

router = APIRouter()


@router.get("/kpi")
def kpi_page(request: Request, mesi: int = 12, iva: str = "", provvisorio: str = "", session: Session = Depends(get_session)):
    from datetime import date, timedelta

    from app.services.util import get_setting, set_setting

    mesi = mesi if mesi in (6, 12, 24) else 12
    if iva in ("mensile", "trimestrale"):
        set_setting(session, "vat_frequency", iva)  # si ricorda la scelta
        session.commit()
    iva = get_setting(session, "vat_frequency") or "trimestrale"
    rep = kpi.build(session, mesi)
    vat = kpi.vat_report(session, date.today().year, iva)
    from app.services.util import parse_date

    if parse_date(provvisorio):
        set_setting(session, "provisional_date", parse_date(provvisorio).isoformat())
        session.commit()
    saved = parse_date(get_setting(session, "provisional_date"))
    cutoff = saved or (date.today().replace(day=1) - timedelta(days=1))
    prov = kpi.provisional_view(session, cutoff)
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
    charts["vat"] = {"kind": "bar", "labels": [p.label for p in vat.periods], "title": "IVA a debito e a credito", "series": [
        {"label": "IVA a debito (vendite)", "values": [p.debit / 100 for p in vat.periods], "color": "--series-actual"},
        {"label": "IVA a credito (acquisti)", "values": [p.credit / 100 for p in vat.periods], "color": "--series-forecast"}]}
    charts["vat_balance"] = {"kind": "bar", "labels": [p.label for p in vat.periods], "title": "Posizione IVA", "series": [
        {"label": "Saldo del periodo (+ debito / − credito)", "values": [p.balance / 100 for p in vat.periods], "color": "--series-actual"}]}
    return render(request, "kpi.html", rep=rep, charts=charts, mesi=mesi, vat=vat, today=date.today(), prov=prov)


@router.get("/investimenti")
def investment_page(request: Request, mesi: int = 6, scenario: str = "base", session: Session = Depends(get_session)):
    mesi = mesi if mesi in (3, 6, 12) else 6
    scenario = scenario if scenario in investment.SCENARIOS else "base"
    pp = investment.profit_plan(session, scenario)
    rem = investment.remainder(session)
    colors = ["--series-actual", "--series-forecast", "--series-3", "--series-4"]
    chart_rem = {
        "kind": "bar", "labelFormat": "month", "labels": [m.key for m in rem.months], "stacked": True,
        "title": "Capacità di investimento mese per mese", "series": [
            {"label": label, "values": [m.by_area[i] / 100 for m in rem.months], "color": colors[i % 4], "stack": "inv"}
            for i, (_, label) in enumerate(investment.AREAS)]}
    plan = pp.cash
    chart_profit = {
        "kind": "bar", "labelFormat": "month", "labels": [m.key for m in pp.months], "stacked": True,
        "title": "Incassi previsti: costi e utile", "series": [
            {"label": f"Costi stimati ({pp.cost_ratio * 100:.0f}%)", "values": [m.costs / 100 for m in pp.months],
             "color": "--series-forecast", "stack": "in"},
            {"label": "Utile da investire", "values": [m.profit / 100 for m in pp.months], "color": "--series-actual", "stack": "in"}],
    }
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
        "kind": "bar", "horizontal": True, "labels": [a.label for a in pp.allocations], "title": "Ripartizione",
        "series": [{"label": "Budget", "values": [a.amount / 100 for a in pp.allocations], "color": "--series-actual"}],
    }
    return render(
        request, "investment.html", pp=pp, plan=plan, mesi=mesi, scenario=scenario, scenarios=investment.SCENARIOS,
        cfg=investment.settings(session), areas=investment.AREAS,
        chart_days=chart_days, chart_months=chart_months, chart_alloc=chart_alloc, chart_profit=chart_profit,
        fixed_total=sum(v for _, v in investment.fixed_costs(session)),
        rem=rem, chart_rem=chart_rem, legend_rem=[(s["label"], s["color"], "bar") for s in chart_rem["series"]],
        cfg_reserve_months=f"{investment.settings(session)['inv_reserve_months']:g}".replace(".", ","),
    )


@router.post("/investimenti/impostazioni")
async def investment_settings(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    investment.save_settings(session, {k: str(v) for k, v in form.items()})
    session.commit()
    back = f"/investimenti?mesi={form.get('mesi', 6)}&scenario={form.get('scenario', 'base')}"
    return redirect(back, msg="Impostazioni salvate")


@router.get("/verifica")
def data_check_page(request: Request, anno: int = 0, session: Session = Depends(get_session)):
    from datetime import date

    from app.services import data_check

    anno = anno or date.today().year
    checks = data_check.run_checks(session)
    order = {"err": 0, "warn": 1, "info": 2, "ok": 3}
    checks.sort(key=lambda c: order[c.status])
    return render(request, "data_check.html", checks=checks, tot=data_check.totals(session, anno), anno=anno,
                  n_err=sum(c.status == "err" for c in checks), n_warn=sum(c.status == "warn" for c in checks))
