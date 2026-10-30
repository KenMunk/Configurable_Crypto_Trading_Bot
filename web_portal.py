"""The live trader's web portal: a read-only page for watching it from any browser on your network.

The engine's dashboard server (engine.py) hands these paths here:

    GET /portal                    the page: status, every deployment, a value chart, trades, orders, events
    GET /api/live                  the trader's snapshot (live_trader.LiveTrader.snapshot)
    GET /api/live/trades?limit=N   the latest closed trades
    GET /api/live/orders?limit=N   the latest orders
    GET /api/live/events?limit=N   the latest log events
    GET /api/live/equity?deployment=ID&limit=N
                                   a deployment's real value and its replay's value at each candle close

Nothing here changes anything: there are no buttons that trade, halt or clear. The page is self-contained (no
scripts or fonts from the internet), so it works on a node with no outside access for the viewer's browser to
reach. Set DASHBOARD_PASSWORD in .env to require a password (see engine.py).
"""

import json
from urllib.parse import parse_qs, urlsplit

MAX_ROWS = 500
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
PAGE_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
               "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _limit(query, default=50):
    try:
        return max(1, min(int(query.get("limit", [default])[0]), MAX_ROWS))
    except ValueError:
        return default


def route(path, trader):
    """(status, content type, body bytes, extra headers) for a portal path, or None if it isn't one."""
    parts = urlsplit(path)
    query = parse_qs(parts.query)
    if parts.path in ("/portal", "/portal/"):
        return 200, "text/html; charset=utf-8", PAGE.encode("utf-8"), {"Content-Security-Policy": PAGE_POLICY}
    if not parts.path.startswith("/api/live"):
        return None
    if trader is None:
        return _json(404, {"error": "The live trader is not running"})
    if parts.path == "/api/live":
        return _json(200, trader.snapshot())
    if parts.path == "/api/live/trades":
        return _json(200, trader.ledger.recent_trades(_limit(query)))
    if parts.path == "/api/live/orders":
        return _json(200, trader.ledger.recent_orders(_limit(query)))
    if parts.path == "/api/live/events":
        return _json(200, trader.ledger.events(_limit(query)))
    if parts.path == "/api/live/equity":
        deployment = (query.get("deployment") or [""])[0]
        if not deployment:
            return _json(400, {"error": "Name the deployment: /api/live/equity?deployment=<id>"})
        return _json(200, trader.ledger.equity_history(deployment, _limit(query, MAX_ROWS)))
    return _json(404, {"error": "Unknown live endpoint"})


def _json(status, payload):
    return status, "application/json", json.dumps(payload, indent=1, default=float).encode("utf-8"), {}


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Trader</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1f2328; --muted: #656d76; --line: #d8dee4;
  --good: #1a7f37; --bad: #cf222e; --warn: #9a6700; --accent: #0969da; --replay: #8250df;
  --chip-bg: #eef1f4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --panel: #161b22; --text: #e6edf3; --muted: #8d96a0; --line: #30363d;
    --good: #3fb950; --bad: #f85149; --warn: #d29922; --accent: #4493f8; --replay: #a371f7;
    --chip-bg: #21262d;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; padding: 14px 16px;
         background: var(--panel); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 2; }
h1 { font-size: 17px; margin: 0; }
h2 { font-size: 14px; margin: 0 0 10px; }
main { max-width: 1400px; margin: 0 auto; padding: 16px; display: grid; gap: 16px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { padding: 2px 9px; border-radius: 999px; background: var(--chip-bg); font-size: 12px; white-space: nowrap; }
.chip.good { color: var(--good); } .chip.bad { color: var(--bad); font-weight: 600; } .chip.warn { color: var(--warn); }
.updated { margin-left: auto; color: var(--muted); font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); gap: 12px; }
.tile, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.tile .label { color: var(--muted); font-size: 12px; }
.tile .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .sub { font-size: 12px; color: var(--muted); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
td.num, th.num { text-align: right; }
tr.pick { cursor: pointer; } tr.pick:hover { background: var(--chip-bg); }
tr.selected { outline: 2px solid var(--accent); outline-offset: -2px; }
td.note { white-space: normal; color: var(--muted); font-size: 12px; min-width: 220px; }
.pos { color: var(--good); } .neg { color: var(--bad); } .muted { color: var(--muted); }
.badge { font-size: 11px; padding: 1px 7px; border-radius: 4px; background: var(--chip-bg); }
.badge.live { color: var(--bad); font-weight: 600; } .badge.ACTIVE { color: var(--good); }
.badge.NEEDS_REVIEW, .badge.BLOCKED, .badge.HALTED { color: var(--bad); font-weight: 600; }
.badge.WINDING_DOWN { color: var(--warn); }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr)); gap: 16px; }
main > *, .grid2 > * { min-width: 0; }  /* wide tables scroll inside their panel, not the page */
svg { width: 100%; height: 240px; display: block; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-top: 6px; }
.swatch { display: inline-block; width: 14px; height: 3px; vertical-align: middle; margin-right: 5px; }
.level-ERROR { color: var(--bad); } .level-WARNING { color: var(--warn); }
.warning { font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); padding: 14px 16px 24px;
           max-width: 1400px; margin: 0 auto; }
.empty { color: var(--muted); padding: 10px 0; }
#offline { display: none; background: var(--bad); color: #fff; padding: 8px 16px; font-weight: 600; }
@media (max-width: 600px) { .grid2 { grid-template-columns: 1fr; } .updated { margin-left: 0; } }
</style>
</head>
<body>
<div id="offline"></div>
<header>
  <h1>Live Trader</h1>
  <div class="chips" id="chips"></div>
  <div class="updated" id="updated">Connecting…</div>
</header>
<main>
  <section class="tiles" id="tiles"></section>
  <section class="panel">
    <h2>Deployments</h2>
    <div class="scroll"><table id="deployments"></table></div>
  </section>
  <section class="panel">
    <h2 id="chart-title">Value</h2>
    <svg id="chart" role="img" aria-label="Deployment value over time"></svg>
    <div class="legend">
      <span><span class="swatch" style="background:var(--accent)"></span>Account value</span>
      <span><span class="swatch" style="background:var(--replay)"></span>Model replay value</span>
    </div>
  </section>
  <section class="grid2">
    <div class="panel"><h2>Recent trades</h2><div class="scroll"><table id="trades"></table></div></div>
    <div class="panel"><h2>Recent orders</h2><div class="scroll"><table id="orders"></table></div></div>
  </section>
  <section class="panel"><h2>Events</h2><div class="scroll"><table id="events"></table></div></section>
</main>
<div class="warning"><strong>Risk warning:</strong> this is a speculative automated trader. It buys and sells
cryptocurrency on its own, and you can lose your entire investment. Simulated and paper results do not predict
future returns. This is not financial advice. This page only watches; it cannot place or stop trades.
<span id="datadir"></span></div>
<script>
"use strict";
const REFRESH_MS = 10000;
let selected = null;
try { selected = localStorage.getItem("liveTrader.selected"); } catch (e) {}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const money = (v) => v == null ? "–" : Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const price = (v) => v == null ? "–" : Number(v).toLocaleString(undefined, {maximumSignificantDigits: 7});
const pct = (v) => v == null ? "–" : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%</span>`;
const signed = (v) => v == null ? "–" : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${money(v)}</span>`;
const when = (t) => t ? new Date(t * 1000).toLocaleString() : "–";
const duration = (s) => { s = Math.max(0, Math.round(s)); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; };
const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`; };

async function get(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function table(el, head, rows, empty) {
  if (!rows.length) { el.innerHTML = `<tr><td class="empty">${esc(empty)}</td></tr>`; return; }
  el.innerHTML = "<thead><tr>" + head.map(([label, cls]) => `<th class="${cls || ""}">${esc(label)}</th>`).join("") +
    "</tr></thead><tbody>" + rows.join("") + "</tbody>";
}

function render(snap) {
  const chips = [];
  const reason = snap.key ? esc(snap.key.reason) : "";
  chips.push(snap.mode === "live"
    ? `<span class="chip bad" title="${reason}">LIVE TRADING: real orders</span>`
    : `<span class="chip good" title="${reason}">Paper trading${snap.key ? ` (${snap.key.forced_paper ? "asked for" : "view-only key"})` : ""}</span>`);
  if (snap.halt_file) chips.push(`<span class="chip warn">HALT file: no new entries</span>`);
  for (const [mode, halt] of Object.entries(snap.halts || {})) if (halt) chips.push(`<span class="chip bad">${esc(mode)} halted: ${esc(halt.reason)}</span>`);
  if (snap.fees) chips.push(`<span class="chip">Fees ${(snap.fees.taker * 100).toFixed(2)}% taker / ${(snap.fees.maker * 100).toFixed(2)}% maker</span>`);
  const rec = snap.recovery || {};
  if (rec.outage && Date.now() / 1000 - rec.outage.resumed < 7 * 86400)
    chips.push(`<span class="chip warn" title="Every model was replayed to catch up">Resumed after an outage: offline ${duration(rec.outage.seconds)}, back ${esc(when(rec.outage.resumed))}</span>`);
  if (rec.moved_from) chips.push(`<span class="chip">Moved here from ${esc(rec.moved_from)}</span>`);
  if (snap.state !== "starting")
    chips.push(rec.last_backup ? `<span class="chip" title="${esc(rec.last_backup.path)}">Backed up ${ago(rec.last_backup.time)}</span>` : `<span class="chip warn">No backup yet</span>`);
  const stale = Date.now() / 1000 - snap.time > 180;
  if (snap.state === "starting") chips.unshift(`<span class="chip warn">Starting: loading models and prices…</span>`);
  document.getElementById("chips").innerHTML = chips.join("");
  document.getElementById("updated").innerHTML = snap.time ? `Trader updated ${ago(snap.time)}` + (stale ? ` <span class="neg">(not updating)</span>` : "") : "Starting…";

  if (snap.data_dir) document.getElementById("datadir").textContent = ` Records and backups on the trader's machine: ${snap.data_dir} (reports/ holds them as spreadsheets).`;
  const rows = snap.deployments || [];
  const tiles = [];
  for (const mode of ["live", "paper"]) {
    const group = rows.filter((r) => r.mode === mode && r.equity != null);
    if (!group.length) continue;
    const value = group.reduce((a, r) => a + r.equity, 0), capital = group.reduce((a, r) => a + r.capital, 0);
    tiles.push(`<div class="tile"><div class="label">${mode === "live" ? "Live" : "Paper"} value</div><div class="value">${money(value)}</div><div class="sub">${pct(value / capital - 1)} on ${money(capital)} capital</div></div>`);
  }
  const held = rows.filter((r) => r.position).length;
  const review = rows.filter((r) => ["NEEDS_REVIEW", "BLOCKED"].includes(r.status)).length;
  const outOfSync = rows.filter((r) => r.in_sync === false).length;
  tiles.push(`<div class="tile"><div class="label">Deployments</div><div class="value">${rows.length}</div><div class="sub">${(snap.currencies || []).length} currencies · ${held} holding</div></div>`);
  tiles.push(`<div class="tile"><div class="label">Need attention</div><div class="value ${review ? "neg" : ""}">${review}</div><div class="sub">${outOfSync} differ from their model</div></div>`);
  document.getElementById("tiles").innerHTML = tiles.join("");

  if (!selected || !rows.some((r) => r.id === selected)) selected = (rows.find((r) => r.product_id) || {}).id || null;
  table(document.getElementById("deployments"),
    [["Currency"], ["Deployment"], ["Mode"], ["Status"], ["Price", "num"], ["Value", "num"], ["Return", "num"],
     ["Account"], ["Model"], ["Nearest stop", "num"], ["Notes"]],
    rows.map((r) => {
      if (r.status === "BLOCKED") return `<tr><td>–</td><td>${esc(r.id)}</td><td></td><td><span class="badge BLOCKED">BLOCKED</span></td><td colspan="6"></td><td class="note">${esc(r.note)}</td></tr>`;
      const stops = (r.replay && r.replay.stops) || [];
      const nearest = stops.length ? stops.reduce((a, s) => s.price > a.price ? s : a) : null;
      const model = r.replay == null ? `<span class="muted">waiting</span>` : r.replay.in_position ? "in" : "out";
      const sync = r.in_sync === false ? ` <span class="neg" title="The account and the model's replay differ">≠</span>` : "";
      const notes = [...(r.notes || []), ...(r.review_note ? [r.review_note] : [])].map(esc).join("<br>");
      return `<tr class="pick ${r.id === selected ? "selected" : ""}" data-id="${esc(r.id)}">` +
        `<td>${esc(r.product_id)}</td><td>${esc(r.id)}</td><td><span class="badge ${esc(r.mode)}">${esc(r.mode)}</span></td>` +
        `<td><span class="badge ${esc(r.status)}">${esc(r.status.replace("_", " "))}</span></td>` +
        `<td class="num">${price(r.price)}</td><td class="num">${money(r.equity)}</td><td class="num">${pct(r.return)}</td>` +
        `<td>${r.position ? `held since ${esc(when(r.position.since))}` : "cash"}</td><td>${model}${sync}</td>` +
        `<td class="num">${nearest ? `${price(nearest.price)} <span class="muted">${esc(nearest.reason)}</span>` : "–"}</td>` +
        `<td class="note">${notes}</td></tr>`;
    }), "No deployments yet. The trader may still be starting.");
  for (const tr of document.querySelectorAll("#deployments tr.pick")) {
    tr.addEventListener("click", () => {
      selected = tr.dataset.id;
      try { localStorage.setItem("liveTrader.selected", selected); } catch (e) {}
      render(snap); loadChart();
    });
  }
}

function drawChart(points) {
  const svg = document.getElementById("chart");
  const W = svg.clientWidth || 800, H = 240, L = 64, R = 10, T = 10, B = 26;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (points.length < 2) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" fill="var(--muted)" font-size="13">No candle closes recorded yet</text>`;
    return;
  }
  const xs = points.map((p) => p.time);
  const ys = points.flatMap((p) => [p.equity, p.replay_equity]).filter((v) => v != null);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1e-9) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
  const X = (t) => L + (t - x0) / (x1 - x0 || 1) * (W - L - R);
  const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  const path = (key) => points.filter((p) => p[key] != null).map((p, i) => `${i ? "L" : "M"}${X(p.time).toFixed(1)},${Y(p[key]).toFixed(1)}`).join("");
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4, y = Y(v);
    grid += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="var(--line)"/><text x="${L - 6}" y="${y + 4}" text-anchor="end" fill="var(--muted)" font-size="11">${money(v)}</text>`;
  }
  const label = (t) => new Date(t * 1000).toLocaleDateString(undefined, {month: "short", day: "numeric"});
  grid += `<text x="${L}" y="${H - 6}" fill="var(--muted)" font-size="11">${label(x0)}</text><text x="${W - R}" y="${H - 6}" text-anchor="end" fill="var(--muted)" font-size="11">${label(x1)}</text>`;
  svg.innerHTML = grid +
    `<path d="${path("replay_equity")}" fill="none" stroke="var(--replay)" stroke-width="1.6" stroke-dasharray="5 3"/>` +
    `<path d="${path("equity")}" fill="none" stroke="var(--accent)" stroke-width="2"/>`;
}

async function loadChart() {
  document.getElementById("chart-title").textContent = selected ? `Value: ${selected}` : "Value";
  if (!selected) { drawChart([]); return; }
  try { drawChart((await get(`/api/live/equity?deployment=${encodeURIComponent(selected)}&limit=500`)).reverse()); }
  catch (e) { drawChart([]); }
}

function renderHistory(trades, orders, events) {
  table(document.getElementById("trades"),
    [["Closed"], ["Deployment"], ["Mode"], ["Bought", "num"], ["Sold", "num"], ["Profit", "num"], ["Exit"]],
    trades.map((t) => `<tr><td>${esc(when(t.exit_time))}</td><td>${esc(t.deployment_id)}</td><td>${esc(t.mode)}</td>` +
      `<td class="num">${price(t.entry_price)}</td><td class="num">${price(t.exit_price)}</td><td class="num">${signed(t.pnl)}</td><td>${esc(t.exit_reason)}</td></tr>`),
    "No closed trades yet.");
  table(document.getElementById("orders"),
    [["Placed"], ["Deployment"], ["Side"], ["Mode"], ["Status"], ["Filled", "num"], ["Price", "num"]],
    orders.map((o) => `<tr><td>${esc(when(o.created_at))}</td><td>${esc(o.deployment_id)}</td><td>${esc(o.side)}</td><td>${esc(o.mode)}</td>` +
      `<td>${esc(o.status)}</td><td class="num">${o.filled_value ? money(o.filled_value) : "–"}</td><td class="num">${price(o.average_price)}</td></tr>`),
    "No orders yet.");
  table(document.getElementById("events"),
    [["Time"], ["Level"], ["Deployment"], ["Message"]],
    events.map((e) => `<tr><td>${esc(when(e.time))}</td><td class="level-${esc(e.level)}">${esc(e.level)}</td><td>${esc(e.deployment_id || "")}</td><td class="note" style="color:inherit">${esc(e.message)}</td></tr>`),
    "No events yet.");
}

async function refresh() {
  const banner = document.getElementById("offline");
  try {
    const [snap, trades, orders, events] = await Promise.all([
      get("/api/live"), get("/api/live/trades?limit=25"), get("/api/live/orders?limit=25"), get("/api/live/events?limit=60")]);
    banner.style.display = "none";
    render(snap);
    renderHistory(trades, orders, events);
    loadChart();
  } catch (e) {
    banner.textContent = `Can't reach the trader (${e.message}). Retrying every ${REFRESH_MS / 1000}s.`;
    banner.style.display = "block";
  }
}
refresh();
setInterval(refresh, REFRESH_MS);
window.addEventListener("resize", loadChart);
</script>
</body>
</html>
"""
