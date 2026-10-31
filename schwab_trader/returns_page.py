"""One page with the plan's backtest returns: every week of every year (a heat map plus a
table per year), every year, and every crash, next to the S&P 500 and Nasdaq 100.

Reads what `python -m schwab_trader.crash_research --current` wrote (summary.json
"current_plan" and plan_weekly.csv) and writes results/crashes/plan_returns.html, a single
self-contained file (data inline, no outside requests but Google Fonts).

    python -m schwab_trader.returns_page
"""
from __future__ import annotations

import json

import pandas as pd

from .crash_research import OUT

PAGE = OUT / "plan_returns.html"


def page_data() -> dict:
    sm = json.loads((OUT / "summary.json").read_text())
    cp = sm["current_plan"]
    w = pd.read_csv(OUT / "plan_weekly.csv", index_col=0, parse_dates=True)
    weeks = [[d.date().isoformat(), round(r.plan, 5), round(r.SPY, 5), round(r.QQQ, 5)] for d, r in w.iterrows()]
    yearly = []
    for y, r in cp["yearly"].items():
        p = w.loc[w.index.year == int(y), "plan"]
        yearly.append({"year": y, "plan": r["plan"], "spy": r["SPY"], "qqq": r["QQQ"], "weeks": int(len(p)),
                       "up": int((p >= 1e-6).sum()), "down": int((p <= -1e-6).sum()), "cash": int((p.abs() < 1e-6).sum()),
                       "best": float(p.max()), "worst": float(p.min())})
    old = [{"index": name, **{k: c[k] for k in ("peak", "trough", "recovered", "drop", "with_shield")}}
           for key, name in (("SPX", "S&P 500"), ("NDX", "Nasdaq 100")) for c in sm["crashes"][key] if c["peak"] < cp["start"]]
    yrs = (pd.Timestamp(cp["end"]) - pd.Timestamp(cp["start"])).days / 365.25
    cash = float((w.plan.abs() < 1e-6).mean())
    return {"label": cp["label"], "cash": cash, "start": cp["start"], "end": cp["end"], "growth": cp["growth"],
            "rate": {k: g ** (1 / yrs) - 1 for k, g in cp["growth"].items()}, "max_drawdown": cp["max_drawdown"],
            "weeks": weeks, "week_stats": cp["weeks"], "yearly": yearly, "crashes": cp["crashes"],
            "old": sorted(old, key=lambda c: c["peak"])}


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plan Returns by Week</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* Layout: a ledger. Summary figures, then the week-by-year heat map, then the tables it summarizes. */
:root {
  --bg: #f6f7f5; --surface: #ffffff; --ink: #121614; --ink-2: #4c5450; --muted: #858c88; --line: #dfe3e0;
  --accent: #1f5f8b;
  --gain-1: #c9def5; --gain-2: #6aa2e2; --gain-3: #1c5cab;
  --loss-1: #f4c4c3; --loss-2: #e2706f; --loss-3: #ad3030;
  --flat: #e9ebe8; --crash: #121614;
  --display: "Archivo", "Arial Narrow", system-ui, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1211; --surface: #171b19; --ink: #edf0ee; --ink-2: #b9c0bc; --muted: #868d89; --line: #2a302d;
    --accent: #79b0e6;
    --gain-1: #1d3d63; --gain-2: #2f74c9; --gain-3: #8cbcf2;
    --loss-1: #5a2424; --loss-2: #b94545; --loss-3: #f09898;
    --flat: #2a2f2c; --crash: #edf0ee;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1211; --surface: #171b19; --ink: #edf0ee; --ink-2: #b9c0bc; --muted: #868d89; --line: #2a302d;
  --accent: #79b0e6;
  --gain-1: #1d3d63; --gain-2: #2f74c9; --gain-3: #8cbcf2;
  --loss-1: #5a2424; --loss-2: #b94545; --loss-3: #f09898;
  --flat: #2a2f2c; --crash: #edf0ee;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); }
main { max-width: 1120px; margin: 0 auto; padding: 32px 20px 64px; display: grid; gap: 36px; }
main > *, section > * { min-width: 0; }   /* wide tables scroll inside their card, never the page */
h1, h2 { font-family: var(--display); text-wrap: balance; margin: 0; letter-spacing: -0.01em; }
h1 { font-size: 34px; font-weight: 800; line-height: 1.1; }
h2 { font-size: 21px; font-weight: 700; }
p { margin: 0; max-width: 68ch; color: var(--ink-2); }
.intro { display: grid; gap: 10px; }
.note { font-size: 13px; color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.stat { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; display: grid; gap: 2px; }
.stat b { font-family: var(--display); font-size: 28px; font-weight: 700; }
.stat span { font-size: 13px; color: var(--ink-2); }
section { display: grid; gap: 14px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
.scroll { overflow-x: auto; }
.heat { display: grid; grid-template-columns: 52px repeat(53, minmax(11px, 1fr)); gap: 2px; min-width: 700px; align-items: center; }
.heat .yr { font-size: 12px; color: var(--ink-2); font-variant-numeric: tabular-nums; background: none; border: 0; padding: 0;
            text-align: left; cursor: pointer; font-family: var(--body); }
.heat .yr:hover, .heat .yr[aria-pressed="true"] { color: var(--accent); font-weight: 600; }
.heat .yr:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cell { height: 18px; border-radius: 2px; background: var(--flat); }
.cell.crash { box-shadow: inset 0 0 0 1.5px var(--crash); }
.cell.empty { background: none; }
.months { display: grid; grid-template-columns: 52px repeat(53, minmax(11px, 1fr)); gap: 2px; min-width: 700px;
          font-size: 11px; color: var(--muted); margin-top: 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; font-size: 12px; color: var(--ink-2); margin-top: 12px; }
.legend i { display: inline-block; width: 14px; height: 14px; border-radius: 2px; vertical-align: -3px; margin-right: 5px; }
.legend .ring { background: var(--flat); box-shadow: inset 0 0 0 1.5px var(--crash); }
.tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--line);
       border-radius: 6px; padding: 8px 10px; font-size: 12px; box-shadow: 0 6px 18px rgba(0,0,0,.14); display: none;
       font-variant-numeric: tabular-nums; z-index: 5; min-width: 170px; }
.tip strong { display: block; margin-bottom: 4px; font-size: 12px; }
.tip div { display: flex; justify-content: space-between; gap: 14px; }
.years { display: flex; flex-wrap: wrap; gap: 6px; }
.years button { font: 500 13px var(--body); color: var(--ink-2); background: var(--surface); border: 1px solid var(--line);
                border-radius: 999px; padding: 4px 11px; cursor: pointer; font-variant-numeric: tabular-nums; }
.years button:hover { border-color: var(--accent); color: var(--ink); }
.years button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--surface); }
.years button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 10px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
tfoot td { font-weight: 600; border-bottom: 0; }
td.pos { color: var(--gain-3); } td.neg { color: var(--loss-3); }
.chip { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 6px; vertical-align: 0; }
.sub { font-size: 12px; color: var(--muted); font-weight: 400; }
@media (max-width: 480px) { main { padding: 22px 16px 48px; } h1 { font-size: 27px; } .stat b { font-size: 24px; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<main>
  <header class="intro">
    <h1>Plan Returns by Week</h1>
    <p id="lede"></p>
    <p class="note">Backtest, after trading costs. The rules were picked on Feb 2011 to Oct 2021; only Oct 2021 on is a true out-of-sample test. Past results, not a forecast.</p>
  </header>

  <div class="stats" id="stats"></div>

  <section>
    <h2>Every week of every year</h2>
    <p>Each square is one week of the plan, Monday to Friday. Blue weeks made money, red weeks lost it, gray weeks were flat (most of them all in cash while the crash shield was on, as in 2022). Outlined weeks fall inside an S&amp;P 500 drop of 10% or more (top to bottom). Hover a square for that week; pick a year to list its weeks below.</p>
    <div class="card">
      <div class="scroll"><div class="heat" id="heat" aria-hidden="true"></div><div class="months" id="months" aria-hidden="true"></div></div>
      <div class="legend" id="legend"></div>
    </div>
  </section>

  <section>
    <h2 id="weekTitle">Weeks</h2>
    <div class="years" id="years" role="group" aria-label="Year"></div>
    <div class="card scroll"><table id="weekTable"></table></div>
  </section>

  <section>
    <h2>Every year</h2>
    <div class="card scroll"><table id="yearTable"></table></div>
  </section>

  <section>
    <h2>Every crash</h2>
    <p>Each S&amp;P 500 fall of 10% or more since 2011, from its top to its bottom, and where the plan stood once the S&amp;P got back to its old high.</p>
    <div class="card scroll"><table id="crashTable"></table></div>
    <p>Before 2011 there is no stock data for the plan, so these falls of 15% or more show the index itself and the index with the crash shield alone (cash while the shield is on).</p>
    <div class="card scroll"><table id="oldTable"></table></div>
  </section>
</main>
<div class="tip" id="tip" role="status"></div>
<script>
const D = __DATA__;
const pct = (x, d = 1) => { const v = +(x * 100).toFixed(d); return (v < 0 ? "−" : "+") + Math.abs(v).toFixed(d) + "%"; };
const day = s => new Date(s + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "2-digit", year: "numeric" });
const el = (tag, attrs = {}, text) => { const e = document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
const bins = [[-Infinity, -0.04, "loss-3"], [-0.04, -0.015, "loss-2"], [-0.015, -0.0025, "loss-1"], [-0.0025, 0.0025, "flat"],
              [0.0025, 0.015, "gain-1"], [0.015, 0.04, "gain-2"], [0.04, Infinity, "gain-3"]];
const color = x => "var(--" + bins.find(b => x >= b[0] && x < b[1])[2] + ")";
const sign = x => x >= 0.0005 ? "pos" : x <= -0.0005 ? "neg" : "";

document.getElementById("lede").textContent =
  `The daily plan as it runs now (${D.label}), backtested week by week from ${day(D.start)} to ${day(D.end)}, next to the S&P 500 (SPY) and the Nasdaq 100 (QQQ).`;

const ws = D.week_stats;
const stats = [
  [D.growth.plan.toFixed(1) + "x", `account growth (S&P ${D.growth.SPY.toFixed(1)}x, Nasdaq 100 ${D.growth.QQQ.toFixed(1)}x)`],
  [pct(D.rate.plan), `a year (S&P ${pct(D.rate.SPY)}, Nasdaq 100 ${pct(D.rate.QQQ)})`],
  [pct(D.max_drawdown, 0), "worst drop from a high"],
  [Math.round(ws.up * 100) + "%", `of ${ws.n} weeks up, ${Math.round(D.cash * 100)}% all in cash; ahead of the S&P in ${Math.round(ws.beat_spy * 100)}%`],
];
const st = document.getElementById("stats");
for (const [b, s] of stats) { const d = el("div", { class: "stat" }); d.append(el("b", {}, b), el("span", {}, s)); st.append(d); }

// weeks by year, numbered from the first week ending in that year
const byYear = {};
for (const w of D.weeks) (byYear[w[0].slice(0, 4)] ||= []).push(w);
const years = Object.keys(byYear).sort();
const inCrash = d => D.crashes.some(c => d >= c.peak && d <= addDays(c.trough, 6));   // weeks end on Fridays
function addDays(s, n) { const t = new Date(s + "T12:00:00"); t.setDate(t.getDate() + n); return t.toISOString().slice(0, 10); }

const heat = document.getElementById("heat"), tip = document.getElementById("tip");
const yearButtons = {};
for (const y of years) {
  const b = el("button", { class: "yr", type: "button", "aria-pressed": "false", tabindex: "-1" }, y);
  b.addEventListener("click", () => pick(y));
  yearButtons[y] = b;
  heat.append(b);
  const list = byYear[y];
  // a part year starts its row at the week of the year it actually starts in
  const first = new Date(list[0][0] + "T12:00:00"), jan1 = new Date(+y, 0, 1, 12);
  const offset = Math.max(0, Math.floor((first - jan1) / 864e5 / 7));
  for (let i = 0; i < offset; i++) heat.append(el("div", { class: "cell empty" }));
  list.forEach(w => {
    const c = el("div", { class: "cell" + (inCrash(w[0]) ? " crash" : "") });
    c.style.background = color(w[1]);
    c.addEventListener("pointermove", e => show(e, w));
    c.addEventListener("pointerleave", () => tip.style.display = "none");
    c.addEventListener("click", () => pick(y));
    heat.append(c);
  });
  for (let i = offset + list.length; i < 53; i++) heat.append(el("div", { class: "cell empty" }));
}
const months = document.getElementById("months");
months.append(el("span"));
const mpos = { 1: "Jan", 9: "Mar", 18: "May", 27: "Jul", 36: "Sep", 44: "Nov" };
for (let i = 1; i <= 53; i++) months.append(el("span", {}, mpos[i] || ""));

function show(e, w) {
  tip.replaceChildren(el("strong", {}, "Week ending " + day(w[0])));
  for (const [k, v] of [["Plan", w[1]], ["S&P 500", w[2]], ["Nasdaq 100", w[3]]]) {
    const r = el("div"); r.append(el("span", {}, k), el("b", {}, pct(v))); tip.append(r);
  }
  tip.style.display = "block";
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8), yy = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + "px"; tip.style.top = yy + "px";
}

const lg = document.getElementById("legend");
const labels = ["−4% or worse", "−4% to −1.5%", "−1.5% to −0.25%", "about flat", "+0.25% to +1.5%", "+1.5% to +4%", "+4% or better"];
bins.forEach((b, i) => { const s = el("span"); const sw = el("i"); sw.style.background = "var(--" + b[2] + ")"; s.append(sw, labels[i]); lg.append(s); });
{ const s = el("span"); s.append(el("i", { class: "ring" }), "inside an S&P 500 fall of 10%+"); lg.append(s); }

// year picker + weekly table
const yb = document.getElementById("years");
const pills = {};
for (const y of years) { const b = el("button", { type: "button", "aria-pressed": "false" }, y); b.addEventListener("click", () => pick(y)); pills[y] = b; yb.append(b); }
function row(tr, cells) { for (const [v, cls] of cells) tr.append(el("td", cls ? { class: cls } : {}, v)); return tr; }
function head(t, cols) { const h = el("thead"), r = el("tr"); for (const c of cols) r.append(el("th", {}, c)); h.append(r); t.append(h); }
function pick(y) {
  for (const k of years) { pills[k].setAttribute("aria-pressed", k === y); yearButtons[k].setAttribute("aria-pressed", k === y); }
  const t = document.getElementById("weekTable"); t.replaceChildren();
  const list = byYear[y], yr = D.yearly.find(r => r.year === y);
  document.getElementById("weekTitle").textContent = `Every week of ${y}` + (y === D.end.slice(0, 4) ? " so far" : "");
  head(t, ["Week ending", "Plan", "S&P 500", "Nasdaq 100", "Plan minus S&P"]);
  const b = el("tbody");
  for (const w of list) {
    const tr = el("tr"); const d = el("td"); const ch = el("span", { class: "chip" }); ch.style.background = color(w[1]);
    d.append(ch, day(w[0])); tr.append(d);
    row(tr, [[pct(w[1]), sign(w[1])], [pct(w[2]), sign(w[2])], [pct(w[3]), sign(w[3])], [pct(w[1] - w[2]), sign(w[1] - w[2])]]);
    b.append(tr);
  }
  t.append(b);
  if (yr) {
    const f = el("tfoot"), tr = el("tr");
    tr.append(el("td", {}, `${y}: ${yr.up} weeks up, ${yr.down} down` + (yr.cash ? `, ${yr.cash} in cash` : "")));
    row(tr, [[pct(yr.plan), sign(yr.plan)], [pct(yr.spy), sign(yr.spy)], [pct(yr.qqq), sign(yr.qqq)], [pct(yr.plan - yr.spy), sign(yr.plan - yr.spy)]]);
    f.append(tr); t.append(f);
  }
}
pick(years[years.length - 1]);

// every year
{
  const t = document.getElementById("yearTable");
  head(t, ["Year", "Plan", "S&P 500", "Nasdaq 100", "Weeks up", "Down", "In cash", "Best week", "Worst week"]);
  const b = el("tbody");
  for (const r of D.yearly) {
    const tr = el("tr");
    tr.append(el("td", {}, r.year === D.end.slice(0, 4) ? `${r.year} (to ${day(D.end).slice(0, 6)})` : r.year));
    row(tr, [[pct(r.plan), sign(r.plan)], [pct(r.spy), sign(r.spy)], [pct(r.qqq), sign(r.qqq)], [String(r.up)], [String(r.down)],
             [String(r.cash)],
             [pct(r.best), "pos"], [pct(r.worst), sign(r.worst)]]);
    b.append(tr);
  }
  t.append(b);
}

// every crash since 2011
{
  const t = document.getElementById("crashTable");
  head(t, ["S&P 500 top to bottom", "S&P 500", "Nasdaq 100", "Plan", "Plan's worst drop", "Plan by the S&P's recovery"]);
  const b = el("tbody");
  for (const c of D.crashes) {
    const tr = el("tr");
    tr.append(el("td", {}, `${day(c.peak)} – ${day(c.trough)}`));
    const rec = el("td", { class: sign(c.plan_to_recovery) }, pct(c.plan_to_recovery, 0));
    rec.append(el("span", { class: "sub" }, c.recovered ? ` by ${day(c.recovered)}` : " (S&P not back yet)"));
    row(tr, [[pct(c.spy, 0), sign(c.spy)], [pct(c.qqq, 0), sign(c.qqq)], [pct(c.plan, 0), sign(c.plan)], [pct(c.plan_worst, 0), sign(c.plan_worst)]]);
    tr.append(rec);
    b.append(tr);
  }
  t.append(b);
}
// before 2011: index only
{
  const t = document.getElementById("oldTable");
  head(t, ["Index, top to bottom", "Index", "With the crash shield", "Back to its high"]);
  const b = el("tbody");
  for (const c of D.old) {
    const tr = el("tr");
    tr.append(el("td", {}, `${c.index}: ${day(c.peak)} – ${day(c.trough)}`));
    row(tr, [[pct(c.drop, 0), sign(c.drop)], [pct(c.with_shield, 0), sign(c.with_shield)], [c.recovered ? day(c.recovered) : "not yet"]]);
    b.append(tr);
  }
  t.append(b);
}
</script>
</body>
</html>
"""


def main():
    data = json.dumps(page_data(), separators=(",", ":"), default=str)
    PAGE.write_text(TEMPLATE.replace("__DATA__", data.replace("</", "<\\/")))
    print(PAGE)


if __name__ == "__main__":
    main()
