"""Live operations dashboard for Sam.

The visual showpiece. Reads from the same SQLite DB Sam writes to, so it works
across processes (Sam, supplier bots, dashboard all separate). Streams updates
via long-polling on a JSON endpoint — no websockets needed for a 1-day build,
and the page stays simple enough to debug in a tab.

Three panels:
  * Active calls with live transcripts (inbound from customers + outbound to
    suppliers). When Sam triggers source_parts, the parallel outbound calls
    light up and stream in real time.
  * Recent jobs with severity badges.
  * Parts sourcing requests with per-supplier offers side-by-side.

Run with::

    uv run python -m sam.dashboard

Then open http://localhost:7861.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel

# Load .env BEFORE importing modules that read env vars at import time.
load_dotenv(override=True)

from . import db  # noqa: E402
from .outbound_caller import source_parts  # noqa: E402


app = FastAPI(title="Sam — Rivera Plumbing Dispatch")

INDEX_HTML = (Path(__file__).parent / "dashboard.html").read_text() if (
    Path(__file__).parent / "dashboard.html"
).exists() else None


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


def _enrich_call(call: dict) -> dict:
    """Hydrate JSON transcript + add duration."""
    try:
        transcript = json.loads(call.get("transcript", "[]"))
    except json.JSONDecodeError:
        transcript = []
    out = dict(call)
    out["transcript"] = transcript
    started = call.get("started_at") or 0
    ended = call.get("ended_at")
    out["duration_seconds"] = round((ended or time.time()) - started, 1)
    out["live"] = ended is None
    return out


class SourceRequest(BaseModel):
    part_query: str
    quantity: int = 1
    max_price_dollars: float | None = None


@app.post("/api/source")
async def trigger_source(req: SourceRequest) -> JSONResponse:
    """Dispatcher console: kick off a parts sourcing run from the dashboard.

    Lets demo viewers see the bots-calling-bots flow without needing a live
    voice call. Returns immediately with the request_id; the dashboard's
    polling loop picks up the calls as they happen.
    """
    async def _run() -> None:
        try:
            await source_parts(
                part_query=req.part_query,
                quantity=req.quantity,
                max_price_dollars=req.max_price_dollars,
            )
        except Exception:
            logger.error(f"source_parts failed:\n{traceback.format_exc()}")

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": "Sourcing kicked off — watch the panel."})


@app.get("/api/state")
async def state() -> JSONResponse:
    """Single endpoint the front-end polls. Returns everything the UI needs."""
    calls = [_enrich_call(c) for c in db.recent_calls(limit=15)]
    jobs = db.recent_jobs(limit=10)
    parts = db.recent_parts_requests(limit=10)

    # For each parts request, attach the suppliers' calls so they render together.
    parts_with_calls: list[dict] = []
    for req in parts:
        related = [c for c in calls if c.get("parts_request_id") == req["id"]]
        related.sort(key=lambda c: c.get("supplier_name") or c.get("counterpart"))
        parts_with_calls.append({**req, "calls": related})

    return JSONResponse(
        {
            "now": time.time(),
            "kpis": {
                "active_calls": sum(1 for c in calls if c["live"]),
                "jobs_today": len(jobs),
                "parts_requests": len(parts),
            },
            "calls": calls,
            "jobs": jobs,
            "parts_requests": parts_with_calls,
        }
    )


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Sam — Rivera Plumbing Dispatch</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
  :root {
    --bg: #0b1020;
    --panel: #131a32;
    --panel-2: #1a2348;
    --ink: #e6ecff;
    --ink-dim: #8a95b8;
    --accent: #7c5cff;
    --good: #2dd4a4;
    --warn: #f5a524;
    --bad: #ff6b6b;
    --line: #232c52;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  header {
    padding: 16px 24px;
    border-bottom: 1px solid var(--line);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: .2px; }
  header .sub { color: var(--ink-dim); font-size: 12px; }
  .kpis { display: flex; gap: 24px; }
  .kpi { text-align: right; }
  .kpi .v { font-size: 22px; font-weight: 700; }
  .kpi .l { font-size: 11px; color: var(--ink-dim); text-transform: uppercase; letter-spacing: 1px; }
  main { display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; padding: 16px 24px; }
  .col { display: flex; flex-direction: column; gap: 12px; }
  .panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .panel h2 {
    margin: 0 0 10px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1.4px;
    color: var(--ink-dim);
    font-weight: 600;
  }
  .empty { color: var(--ink-dim); font-style: italic; padding: 8px 0; }
  .call, .job, .req { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }
  .call .head, .job .head, .req .head {
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
  }
  .who { font-weight: 600; }
  .live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--good); margin-right: 6px; animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .badge { font-size: 10px; padding: 2px 7px; border-radius: 10px; text-transform: uppercase; letter-spacing: .8px; font-weight: 700; }
  .badge.in { background: #1e3554; color: #74b3ff; }
  .badge.out { background: #3a2b5b; color: #c7a8ff; }
  .badge.emergency { background: #4a1d24; color: #ff8a8a; }
  .badge.urgent { background: #4a3a1a; color: #ffd078; }
  .badge.routine { background: #1f3a31; color: #7be0b4; }
  .badge.live { background: #14352b; color: var(--good); }
  .badge.recommended { background: #2d2257; color: var(--accent); }
  .meta { color: var(--ink-dim); font-size: 11px; margin-top: 2px; }
  .transcript { margin-top: 8px; max-height: 220px; overflow-y: auto; padding-right: 4px; }
  .turn { padding: 4px 0; font-size: 13px; }
  .turn .r { display: inline-block; min-width: 70px; color: var(--ink-dim); font-size: 11px; text-transform: uppercase; letter-spacing: .8px; }
  .turn.sam .r, .turn.caller .r { color: var(--accent); }
  .turn.customer .r { color: #74b3ff; }
  .turn.supplier .r { color: #c7a8ff; }
  .req .offers { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin-top: 10px; }
  .offer { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; }
  .offer h3 { margin: 0 0 4px; font-size: 12px; font-weight: 600; display: flex; justify-content: space-between; }
  .offer .price { font-size: 16px; font-weight: 700; }
  .offer .price.in { color: var(--good); }
  .offer .price.out { color: var(--ink-dim); text-decoration: line-through; }
  .offer .turns { margin-top: 6px; font-size: 11px; color: var(--ink-dim); max-height: 110px; overflow-y: auto; }
  .offer .winner { font-size: 10px; color: var(--accent); font-weight: 700; }
  .offer.winner { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(124,92,255,.15); }
  footer { padding: 16px 24px; color: var(--ink-dim); font-size: 11px; border-top: 1px solid var(--line); }
  .source-row { display: grid; grid-template-columns: 2fr 60px 80px 110px; gap: 6px; margin-bottom: 10px; }
  .source-row input { background: var(--panel-2); border: 1px solid var(--line); color: var(--ink); padding: 6px 8px; border-radius: 6px; font: inherit; }
  .source-row button { background: var(--accent); color: white; border: 0; border-radius: 6px; padding: 6px 10px; font-weight: 600; cursor: pointer; }
  .source-row button:hover { filter: brightness(1.1); }
</style>
</head>
<body>
<header>
  <div>
    <h1>Sam — Rivera Plumbing Dispatch</h1>
    <div class="sub">live ops · auto-refresh 1s</div>
  </div>
  <div class="kpis">
    <div class="kpi"><div class="v" id="kpi-calls">0</div><div class="l">active calls</div></div>
    <div class="kpi"><div class="v" id="kpi-jobs">0</div><div class="l">jobs today</div></div>
    <div class="kpi"><div class="v" id="kpi-parts">0</div><div class="l">parts requests</div></div>
  </div>
</header>
<main>
  <div class="col">
    <section class="panel">
      <h2>Calls</h2>
      <div id="calls"></div>
    </section>
  </div>
  <div class="col">
    <section class="panel">
      <h2>Parts sourcing</h2>
      <div class="source-row">
        <input id="src-part" placeholder="part name (e.g. Rinnai RU199i)" />
        <input id="src-qty" type="number" min="1" value="1" />
        <input id="src-max" type="number" placeholder="max $" />
        <button id="src-btn">Source now</button>
      </div>
      <div id="parts"></div>
    </section>
    <section class="panel">
      <h2>Jobs</h2>
      <div id="jobs"></div>
    </section>
  </div>
</main>
<footer>Sam is a hackathon project. Nemotron + Pipecat + Gradium + Twilio. Tested with Cekura.</footer>
<script>
const esc = s => (s||'').replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dur = s => s == null ? '' : (s < 60 ? `${s.toFixed(0)}s` : `${Math.floor(s/60)}m ${Math.floor(s%60)}s`);
const dollars = c => c == null ? '—' : '$' + (c/100).toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:0});
const fmtTurn = t => `<div class="turn ${t.role}"><span class="r">${esc(t.role)}</span> ${esc(t.text)}</div>`;

function renderCall(c) {
  const dirBadge = `<span class="badge ${c.direction==='inbound'?'in':'out'}">${c.direction}</span>`;
  const liveBadge = c.live ? `<span class="badge live"><span class="live-dot"></span>live</span>` : '';
  const turns = (c.transcript||[]).map(fmtTurn).join('');
  return `
    <div class="call">
      <div class="head">
        <div>
          ${dirBadge} ${liveBadge}
          <span class="who"> ${esc(c.counterpart)}</span>
        </div>
        <div class="meta">${dur(c.duration_seconds)}</div>
      </div>
      ${c.summary ? `<div class="meta">${esc(c.summary)}</div>` : ''}
      <div class="transcript">${turns || '<div class="empty">no transcript yet</div>'}</div>
    </div>`;
}

function renderJob(j) {
  const sev = j.severity || 'routine';
  return `
    <div class="job">
      <div class="head">
        <div>
          <span class="badge ${sev}">${sev}</span>
          <span class="who"> ${esc(j.customer_name || j.customer_phone || 'unknown caller')}</span>
        </div>
        <div class="meta">RV-${String(j.id).padStart(5,'0')}</div>
      </div>
      <div class="meta">${esc(j.description)}</div>
      <div class="meta">scheduled: ${esc(j.scheduled_at || 'tbd')} · tech: ${esc(j.tech_name || 'unassigned')}</div>
    </div>`;
}

function renderReq(r) {
  const offers = (r.calls||[]).map(c => {
    const isWin = c.counterpart_id === r.best_supplier_id;
    const wonClass = isWin ? 'winner' : '';
    const winnerTag = isWin ? '<span class="winner">RECOMMENDED</span>' : '';
    const inStockClass = c.summary && c.summary.includes('in stock') ? 'in' : 'out';
    return `
      <div class="offer ${wonClass}">
        <h3>${esc(c.counterpart.replace('supplier:',''))} ${winnerTag}</h3>
        <div class="meta">${esc(c.summary || (c.live ? 'on the line…' : 'no answer'))}</div>
        <div class="turns">${(c.transcript||[]).map(fmtTurn).join('')}</div>
      </div>`;
  }).join('');
  return `
    <div class="req">
      <div class="head">
        <div><span class="who">${esc(r.part_name || r.sku)}</span> <span class="meta">× ${r.quantity}</span></div>
        <div>
          ${r.best_supplier_name ? `<span class="badge recommended">${esc(r.best_supplier_name)} · ${dollars(r.best_price_cents)}</span>` : '<span class="badge live">sourcing…</span>'}
        </div>
      </div>
      ${r.max_price_cents ? `<div class="meta">budget: ${dollars(r.max_price_cents)}</div>` : ''}
      <div class="offers">${offers || '<div class="empty">no supplier responses yet</div>'}</div>
    </div>`;
}

async function tick() {
  try {
    const r = await fetch('/api/state', {cache: 'no-store'});
    const s = await r.json();
    document.getElementById('kpi-calls').textContent = s.kpis.active_calls;
    document.getElementById('kpi-jobs').textContent = s.kpis.jobs_today;
    document.getElementById('kpi-parts').textContent = s.kpis.parts_requests;
    document.getElementById('calls').innerHTML = s.calls.length ? s.calls.map(renderCall).join('') : '<div class="empty">no calls yet — call Sam to start</div>';
    document.getElementById('jobs').innerHTML = s.jobs.length ? s.jobs.map(renderJob).join('') : '<div class="empty">no jobs yet</div>';
    document.getElementById('parts').innerHTML = s.parts_requests.length ? s.parts_requests.map(renderReq).join('') : '<div class="empty">no parts requests yet</div>';
  } catch (e) { console.error(e); }
}
document.getElementById('src-btn').addEventListener('click', async () => {
  const part_query = document.getElementById('src-part').value.trim();
  if (!part_query) return;
  const quantity = parseInt(document.getElementById('src-qty').value || '1', 10);
  const max = document.getElementById('src-max').value.trim();
  const body = {part_query, quantity};
  if (max) body.max_price_dollars = parseFloat(max);
  await fetch('/api/source', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  document.getElementById('src-part').value = '';
});
setInterval(tick, 1000);
tick();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7861, log_level="info")
