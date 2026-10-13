"use strict";
(() => {
  const REFRESH_MS = 15000;
  const $ = (id) => document.getElementById(id);

  // ---- theme -------------------------------------------------------------
  const root = document.documentElement;
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  } catch (_) { /* storage unavailable */ }
  $("theme").addEventListener("click", () => {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const current = root.dataset.theme || (dark ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (_) { /* ignore */ }
  });

  // ---- helpers (all dynamic text goes through textContent) ----------------
  const h = (tag, cls, text, attrs) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = text;
    if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  };
  const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const bytes = (n) => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${nf.format(n)} ${units[i]}`;
  };
  const rate = (n) => `${bytes(n)}/s`;
  const duration = (s) => {
    const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${hr}h` : hr ? `${hr}h ${m}m` : `${m}m`;
  };
  const ago = (ts) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
  };
  const level = (pct) => (pct >= 90 ? "bad" : pct >= 75 ? "warn" : "");

  function stat(label, value, unit, sub, pct, inverse) {
    const card = h("div", "card");
    card.append(h("div", "label", label));
    const v = h("div", "value num", value);
    if (unit) v.append(" ", h("small", "", unit));
    card.append(v, h("div", "sub num", sub || ""));
    if (pct !== null && pct !== undefined) {
      const m = h("div", "meter", null, { role: "presentation" });
      const bar = h("i", inverse ? "" : level(pct));
      bar.style.setProperty("width", `${Math.max(0, Math.min(100, pct))}%`);
      m.append(bar);
      card.append(m);
    }
    return card;
  }
  const NA = "n/a";

  function renderHost(host) {
    const out = [];
    out.push(stat("Uptime", host.uptime_s != null ? duration(host.uptime_s) : NA, "", "since last boot"));
    const cpu = host.cpu || {};
    out.push(stat("CPU", cpu.percent != null ? nf.format(cpu.percent) : NA, cpu.percent != null ? "%" : "",
      cpu.load ? `load ${cpu.load.map((x) => x.toFixed(2)).join(" ")}` : `${cpu.cores || "?"} cores`, cpu.percent));
    const m = host.memory;
    out.push(m ? stat("Memory", bytes(m.used), "", `of ${bytes(m.total)}`, (100 * m.used) / m.total)
      : stat("Memory", NA, "", "unreadable"));
    const d = host.storage;
    out.push(d ? stat("Storage", bytes(d.used), "", `${bytes(d.free)} free of ${bytes(d.total)}`, (100 * d.used) / d.total)
      : stat("Storage", NA, "", "unreadable"));
    const b = host.battery;
    if (b) {
      const state = b.charging ? "charging" : String(b.status).replace(/_/g, " ");
      const temp = b.temperature_c != null ? ` · ${nf.format(b.temperature_c)} °C` : "";
      out.push(stat("Battery", b.percent != null ? nf.format(b.percent) : NA, "%", state + temp,
        b.percent, true));
    } else {
      out.push(stat("Battery", NA, "", "no reader available"));
    }
    const n = host.network;
    if (!n) out.push(stat("Network", NA, "", "not readable on this host"));
    else if (n.rx_bps == null) out.push(stat("Network", "…", "", "measuring"));
    else out.push(stat("Network", rate(n.rx_bps), "down", `${rate(n.tx_bps)} up`));
    $("host").replaceChildren(...out);
  }

  const barClass = (r) => (r === null ? "" : r >= 0.995 ? "ok" : r >= 0.9 ? "warn" : "bad");
  function barRow(label, values, pct, spanLabel) {
    const row = h("div", "bar-row");
    const bar = h("div", "bar", null, { role: "img", "aria-label": `${spanLabel} uptime ${pct != null ? pct + "%" : "no data"}` });
    values.forEach((r, i) => {
      const seg = h("i", barClass(r), null, { title: r === null ? "no data" : `${nf.format(r * 100)}% up` });
      seg.dataset.i = i;
      bar.append(seg);
    });
    row.append(h("span", "", label), bar, h("span", "pct num", pct != null ? `${nf.format(pct)}%` : NA));
    return row;
  }

  function renderServices(list) {
    if (!list.length) { $("svc").replaceChildren(h("p", "empty", "No services configured.")); return; }
    $("svc").replaceChildren(...list.map((s) => {
      const card = h("article", "card");
      const head = h("div", "svc-head");
      head.append(h("span", "dot", null, { "data-state": s.state }));
      const name = h("span", "svc-name");
      if (s.link && /^https?:/.test(s.link)) {
        name.append(h("a", "", s.name, { href: s.link, rel: "noopener noreferrer", target: "_blank" }));
      } else { name.textContent = s.name; }
      const word = { up: "Operational", down: "Down", unknown: "No recent data" }[s.state];
      const badge = h("span", "badge num");
      badge.append(h("b", "", word));
      if (s.latency_ms != null) badge.append(` · ${Math.round(s.latency_ms)} ms`);
      else if (s.error) badge.append(` · ${s.error}`);
      head.append(name, badge);
      card.append(head);
      if (s.description) card.append(h("p", "desc", s.description));
      const bars = h("div", "bars");
      bars.append(barRow("24 h", s.bars_24h, s.uptime_24h, "24 hour"), barRow("7 d", s.bars_7d, s.uptime_7d, "7 day"));
      card.append(bars);
      if (s.metrics && s.metrics.length) {
        const chips = h("div", "chips");
        for (const m of s.metrics) {
          const chip = h("span", "chip num", `${m.label}: `);
          const val = typeof m.value === "number" ? nf.format(m.value) : String(m.value);
          chip.append(h("b", "", m.unit ? `${val} ${m.unit}` : val));
          chips.append(chip);
        }
        card.append(chips);
      }
      return card;
    }));
  }

  const BANNER = {
    up: "All systems operational", degraded: "Some services are down",
    down: "All services are down", unknown: "Waiting for data",
  };

  async function refresh() {
    try {
      const res = await fetch("api/status", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      document.body.classList.remove("stale");
      const banner = $("banner");
      banner.dataset.state = data.summary.state;
      banner.textContent = BANNER[data.summary.state] || BANNER.unknown;
      renderHost(data.host || {});
      renderServices(data.services || []);
      $("updated").textContent = `Updated ${ago(data.generated_at)}`;
    } catch (_) {
      document.body.classList.add("stale");
      $("banner").textContent = "Cannot reach the status server";
      $("banner").dataset.state = "down";
    }
  }
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
