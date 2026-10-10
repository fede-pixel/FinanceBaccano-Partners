/* Grafici e piccole interazioni. Nessuna dipendenza esterna oltre a Chart.js (incluso in locale). */
(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const eur = new Intl.NumberFormat("it-IT", { style: "currency", currency: "EUR" });
  const eurShort = new Intl.NumberFormat("it-IT", { notation: "compact", maximumFractionDigits: 1 });
  const dayFmt = new Intl.DateTimeFormat("it-IT", { weekday: "short", day: "2-digit", month: "short" });

  function baseOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: css("--surface"),
          titleColor: css("--ink"),
          bodyColor: css("--ink-2"),
          borderColor: css("--axis"),
          borderWidth: 1,
          padding: 10,
          callbacks: {
            title: (items) => dayFmt.format(new Date(items[0].label + "T12:00:00")),
            label: (ctx) => (ctx.raw === null ? null : ` ${ctx.dataset.label}: ${eur.format(ctx.raw)}`),
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: css("--axis") },
          ticks: {
            color: css("--muted"),
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 12,
            callback: function (v) {
              const d = new Date(this.getLabelForValue(v) + "T12:00:00");
              return d.getDate() + "/" + (d.getMonth() + 1);
            },
          },
        },
        y: {
          grid: { color: css("--grid") },
          border: { display: false },
          ticks: { color: css("--muted"), callback: (v) => eurShort.format(v) + " €" },
        },
      },
    };
  }

  function forecastChart(canvas, data) {
    const today = data.labels.indexOf(data.today);
    const datasets = [
      {
        label: "Saldo reale",
        data: data.actual,
        borderColor: css("--series-actual"),
        backgroundColor: css("--series-actual"),
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.15,
        spanGaps: false,
      },
      {
        label: "Saldo previsto",
        data: data.projected,
        borderColor: css("--series-forecast"),
        backgroundColor: css("--series-forecast"),
        borderWidth: 2,
        borderDash: [6, 4],
        pointRadius: 0,
        pointHoverRadius: 4,
        stepped: "before",
      },
    ];
    if (data.threshold) {
      datasets.push({
        label: "Soglia minima",
        data: data.labels.map(() => data.threshold),
        borderColor: css("--critical"),
        borderWidth: 1,
        borderDash: [2, 3],
        pointRadius: 0,
        pointHoverRadius: 0,
      });
    }
    const opts = baseOptions();
    const todayLine = {
      id: "todayLine",
      afterDraw(chart) {
        if (today < 0) return;
        const x = chart.scales.x.getPixelForValue(today);
        const { top, bottom } = chart.chartArea;
        const ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = css("--axis");
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, bottom);
        ctx.stroke();
        ctx.fillStyle = css("--muted");
        ctx.font = "11px system-ui";
        ctx.fillText("oggi", x + 4, top + 10);
        ctx.restore();
      },
    };
    return new Chart(canvas, { type: "line", data: { labels: data.labels, datasets }, options: opts, plugins: [todayLine] });
  }

  function flowsChart(canvas, data) {
    const opts = baseOptions();
    opts.scales.x.stacked = true;
    opts.scales.y.stacked = true;
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          { label: "Entrate", data: data.inflow, backgroundColor: css("--series-actual"), borderRadius: 3, maxBarThickness: 14 },
          { label: "Uscite", data: data.outflow, backgroundColor: css("--series-forecast"), borderRadius: 3, maxBarThickness: 14 },
        ],
      },
      options: opts,
    });
  }

  function spendingChart(canvas, data) {
    const opts = baseOptions();
    opts.indexAxis = "y";
    opts.interaction = { mode: "nearest", axis: "y", intersect: false };
    opts.plugins.tooltip.callbacks = {
      title: (items) => items[0].label,
      label: (ctx) => ` ${eur.format(ctx.raw)} · ${data.shares[ctx.dataIndex]}% · ${data.counts[ctx.dataIndex]} movimenti`,
    };
    opts.scales = {
      x: { grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => eurShort.format(v) + " €" } },
      y: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--ink-2"), autoSkip: false } },
    };
    return new Chart(canvas, {
      type: "bar",
      data: { labels: data.labels, datasets: [{ label: "Uscite", data: data.values, backgroundColor: css("--series-actual"), borderRadius: 4, barThickness: 18 }] },
      options: opts,
    });
  }

  function initCharts() {
    document.querySelectorAll("[data-spending]").forEach((el) => {
      const data = JSON.parse(document.getElementById(el.dataset.spending).textContent);
      let chart = spendingChart(el.querySelector("canvas"), data);
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        chart.destroy();
        chart = spendingChart(el.querySelector("canvas"), data);
      });
    });
    document.querySelectorAll("[data-forecast]").forEach((el) => {
      const data = JSON.parse(document.getElementById(el.dataset.forecast).textContent);
      const charts = [];
      const draw = () => {
        charts.splice(0).forEach((c) => c.destroy());
        const line = el.querySelector("canvas.line");
        const bars = el.querySelector("canvas.bars");
        if (line) charts.push(forecastChart(line, data));
        if (bars) charts.push(flowsChart(bars, data));
      };
      draw();
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
    });
  }

  // Grafico generico configurato da JSON: {kind, labels, labelFormat, series:[{label, values, color, dash}], valueFormat, reference}
  const monthFmt = new Intl.DateTimeFormat("it-IT", { month: "short", year: "2-digit" });
  const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1).replace(".", ",") + "%");
  function genericChart(canvas, cfg) {
    const fmtValue = cfg.valueFormat === "pct" ? pct : (v) => eur.format(v);
    const fmtTick = cfg.valueFormat === "pct" ? (v) => Math.round(v * 100) + "%" : (v) => eurShort.format(v) + " €";
    const fmtLabel = (l) => {
      if (cfg.labelFormat === "month") return monthFmt.format(new Date(l + "-15T12:00:00"));
      if (cfg.labelFormat === "day") { const d = new Date(l + "T12:00:00"); return d.getDate() + "/" + (d.getMonth() + 1); }
      return l;
    };
    const opts = baseOptions();
    opts.plugins.tooltip.callbacks = {
      title: (items) => (cfg.labelFormat === "day" ? dayFmt.format(new Date(items[0].label + "T12:00:00")) : fmtLabel(items[0].label)),
      label: (ctx) => (ctx.raw === null ? null : ` ${ctx.dataset.label}: ${fmtValue(ctx.raw)}`),
    };
    opts.scales.x.ticks.callback = function (v) { return fmtLabel(this.getLabelForValue(v)); };
    opts.scales.y.ticks.callback = fmtTick;
    if (cfg.horizontal) {
      opts.indexAxis = "y";
      opts.interaction = { mode: "nearest", axis: "y", intersect: false };
      opts.scales = {
        x: { grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: fmtTick } },
        y: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--ink-2"), autoSkip: false } },
      };
    }
    const datasets = cfg.series.map((s) => {
      const color = css(s.color || "--series-actual");
      return cfg.kind === "line"
        ? { label: s.label, data: s.values, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: cfg.labels.length > 40 ? 0 : 3,
            pointHoverRadius: 5, tension: 0.15, borderDash: s.dash ? [6, 4] : [], spanGaps: true }
        : { label: s.label, data: s.values, backgroundColor: color, borderRadius: 4, maxBarThickness: cfg.horizontal ? 22 : 26,
            borderColor: css("--surface"), borderWidth: cfg.stacked ? { top: 2, bottom: 0, left: 1, right: 1 } : { top: 0, bottom: 0, left: 1, right: 1 },
            stack: s.stack, borderSkipped: cfg.stacked ? "bottom" : "start" };
    });
    if (cfg.stacked) { opts.scales.x.stacked = true; opts.scales.y.stacked = true; }
    if (cfg.reference) {
      datasets.push({ type: "line", label: cfg.reference.label, data: cfg.labels.map(() => cfg.reference.value),
        borderColor: css(cfg.reference.color || "--critical"), borderWidth: 1, borderDash: [2, 3], pointRadius: 0, pointHoverRadius: 0 });
    }
    return new Chart(canvas, { type: cfg.kind === "line" ? "line" : "bar", data: { labels: cfg.labels, datasets }, options: opts });
  }

  function initGenericCharts() {
    document.querySelectorAll("[data-chart]").forEach((el) => {
      const cfg = JSON.parse(document.getElementById(el.dataset.chart).textContent);
      let chart = genericChart(el.querySelector("canvas"), cfg);
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        chart.destroy();
        chart = genericChart(el.querySelector("canvas"), cfg);
      });
    });
  }

  function initDropzones() {
    document.querySelectorAll(".dropzone").forEach((zone) => {
      const input = zone.querySelector("input[type=file]");
      const label = zone.querySelector(".dz-label");
      ["dragenter", "dragover"].forEach((ev) =>
        zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); })
      );
      ["dragleave", "drop"].forEach((ev) =>
        zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); })
      );
      zone.addEventListener("drop", (e) => {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event("change"));
      });
      input.addEventListener("change", () => {
        if (label) label.textContent = [...input.files].map((f) => f.name).join(", ") || label.dataset.empty;
      });
    });
  }

  function initConfirms() {
    document.querySelectorAll("form[data-confirm]").forEach((f) =>
      f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); })
    );
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (window.Chart) { initCharts(); initGenericCharts(); }
    initDropzones();
    initConfirms();
  });
})();
