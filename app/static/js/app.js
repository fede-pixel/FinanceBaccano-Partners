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

  function initCharts() {
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
    if (window.Chart) initCharts();
    initDropzones();
    initConfirms();
  });
})();
