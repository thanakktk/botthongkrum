/* ===================================================================
   FTMO Trading Monitor — front-end logic
   Polls /api/status (3s) for state and /api/quote (1s) for the live chart.
   Pure rendering — the dashboard is read-only except the kill-switch POST.
   =================================================================== */
let chartSym = '', lastPositions = [];
const money = n => n == null ? '—' : n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const ago = s => s == null ? '—' : (s < 90 ? Math.round(s) + 's' : Math.round(s / 60) + 'm') + ' ago';
const ksClass = m => m === 'running' ? 'bg-g' : (m === 'halt_new' ? 'bg-y' : 'bg-r');
const stClass = s => s === 'active' ? 'g' : (s === 'probation' ? 'y' : 'r');
const esc = s => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/* ---------- summary tiles (the glanceable top strip) ---------- */
function tile(lab, val, meta, cls) {
  return `<div class="tile ${cls || ''}"><div class="lab">${lab}</div>
    <div class="val">${val}</div><div class="meta">${meta || ''}</div></div>`;
}
function summaryTiles(d, hb) {
  const eq = hb.equity, mb = d.baseline ? d.baseline.midnight_balance : null;
  const pnl = (eq != null && mb != null) ? eq - mb : null;
  const dDist = (eq != null && hb.daily_floor != null) ? eq - hb.daily_floor : null;
  const oDist = (eq != null && hb.overall_floor != null) ? eq - hb.overall_floor : null;
  const ks = d.kill_switch || {};
  const ksCls = ks.mode === 'running' ? 'good' : (ks.mode === 'halt_new' ? 'warn' : 'bad');
  return [
    tile('Equity', '$' + money(eq),
      pnl != null ? `<span class="${pnl >= 0 ? 'g' : 'r'}">${pnl >= 0 ? '▲' : '▼'} ${pnl >= 0 ? '+' : ''}$${money(pnl)} today</span>` : '',
      pnl == null ? '' : (pnl >= 0 ? 'good' : 'bad')),
    tile('Balance', '$' + money(hb.balance), 'last closed P&L', ''),
    tile('Daily floor room', dDist == null ? '—' : (dDist >= 0 ? '+' : '') + '$' + money(dDist),
      'floor $' + money(hb.daily_floor), dDist == null ? '' : (dDist > 0 ? 'good' : 'bad')),
    tile('Overall floor room', oDist == null ? '—' : (oDist >= 0 ? '+' : '') + '$' + money(oDist),
      'floor $' + money(hb.overall_floor), oDist == null ? '' : (oDist > 0 ? 'good' : 'bad')),
    tile('Open positions', (d.positions ? d.positions.length : 0),
      'risk @SL $' + money(hb.open_risk_to_sl), ''),
    tile('Kill switch',
      `<span class="pill ${ksClass(ks.mode)}" style="font-size:15px;padding:3px 12px">${(ks.mode || '?').toUpperCase()}</span>`,
      ks.reason || 'normal operation', ksCls),
  ].join('');
}

/* ---------- equity curve (SVG) ---------- */
function equityChart(curve) {
  if (!curve || curve.length < 2) return '<div class="muted">collecting equity data…</div>';
  const w = 1000, h = 150, pad = 10, eqs = curve.map(p => p.equity);
  let mn = Math.min(...eqs), mx = Math.max(...eqs); if (mn === mx) { mn -= 1; mx += 1; }
  const X = i => pad + (i / (curve.length - 1)) * (w - 2 * pad);
  const Y = v => pad + (1 - (v - mn) / (mx - mn)) * (h - 2 * pad);
  const pts = curve.map((p, i) => X(i).toFixed(1) + ',' + Y(p.equity).toFixed(1)).join(' ');
  const area = `${pad},${h - pad} ${pts} ${w - pad},${h - pad}`;
  const first = eqs[0], last = eqs[eqs.length - 1], up = last >= first;
  const col = up ? 'var(--grn)' : 'var(--red)';
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" class="chart-svg">
    <defs><linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${col}" stop-opacity=".25"/><stop offset="100%" stop-color="${col}" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${area}" fill="url(#eg)"/>
    <polyline fill="none" stroke="${col}" stroke-width="2" points="${pts}"/></svg>
   <div class="kv"><span class="muted">min $${money(mn)} · max $${money(mx)} · ${curve.length} pts</span>
     <b class="${up ? 'g' : 'r'}">$${money(last)} (${up ? '+' : ''}${money(last - first)})</b></div>`;
}

/* ---------- live candlestick chart — TradingView Lightweight Charts ----------
   Renders REAL MetaTrader broker candles (from /api/quote) with TradingView's
   official charting lib. Key fix for the old "flat" chart: the price scale
   auto-fits the CANDLES only (see autoscaleInfoProvider) — SL/TP/entry are drawn
   as price-lines + a numeric legend, so far-away targets never squash the bars. */
let lwChart = null, lwSeries = null, lwLines = [], lwBidLine = null;
let lwLows = [], lwHighs = [], lwSymLoaded = '', lwLevelSig = '';

function initLwChart() {
  if (lwChart || !window.LightweightCharts) return;
  const el = document.getElementById('lwchart');
  if (!el) return;
  const LC = window.LightweightCharts;
  lwChart = LC.createChart(el, {
    autoSize: true,
    layout: { background: { type: 'solid', color: '#070b12' }, textColor: '#8493ad', fontFamily: 'inherit' },
    grid: { vertLines: { color: 'rgba(255,255,255,.035)' }, horzLines: { color: 'rgba(255,255,255,.05)' } },
    rightPriceScale: { borderColor: '#212c42', scaleMargins: { top: 0.12, bottom: 0.12 } },
    timeScale: { borderColor: '#212c42', timeVisible: true, secondsVisible: false, rightOffset: 4, barSpacing: 7 },
    crosshair: {
      mode: LC.CrosshairMode.Normal,
      vertLine: { color: 'rgba(77,159,255,.4)', labelBackgroundColor: '#1a2336' },
      horzLine: { color: 'rgba(77,159,255,.4)', labelBackgroundColor: '#1a2336' },
    },
  });
  lwSeries = lwChart.addCandlestickSeries({
    upColor: '#2ec27e', downColor: '#f25268',
    wickUpColor: '#2ec27e', wickDownColor: '#f25268',
    borderUpColor: '#2ec27e', borderDownColor: '#f25268',
    priceLineVisible: false, lastValueVisible: true,
    priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    // candle-only autoscale => SL/TP/entry never compress the candles ("flat" fix)
    autoscaleInfoProvider: () => {
      if (!lwLows.length) return null;
      const min = Math.min(...lwLows), max = Math.max(...lwHighs);
      const pad = (max - min) * 0.12 || (max * 0.0008) || 1;
      return { priceRange: { minValue: min - pad, maxValue: max + pad } };
    },
  });
}

function setLevelLines(lv) {
  if (!lwSeries) return;
  lwLines.forEach(l => lwSeries.removePriceLine(l));
  lwLines = [];
  if (!lv) return;
  const LS = window.LightweightCharts.LineStyle;
  const mk = (price, color, title) => {
    if (price == null) return;
    lwLines.push(lwSeries.createPriceLine({
      price, color, lineWidth: 1, lineStyle: LS.Dashed, axisLabelVisible: true, title,
    }));
  };
  mk(lv.entry, '#4d9fff', 'ENTRY');
  mk(lv.sl, '#f25268', 'SL');
  mk(lv.tp1, '#e0a93b', 'TP1');
  mk(lv.tp2, '#2ec27e', 'TP2');
}

// Open-position levels for the symbol CURRENTLY on the chart (not whichever
// symbol updated last) — fixes XAUUSD showing BTCUSD's entry/SL/TP.
function levelsFor(sym) {
  const p = (lastPositions || []).find(x => x.symbol === sym);
  if (!p) return null;
  return { entry: p.open_price, sl: p.sl,
           tp1: p.tp1 != null ? p.tp1 : p.tp, tp2: p.tp2, side: p.side };
}

function setBidLine(bid) {
  if (!lwSeries || bid == null) return;
  if (lwBidLine) lwSeries.removePriceLine(lwBidLine);
  lwBidLine = lwSeries.createPriceLine({
    price: bid, color: '#e6edf6', lineWidth: 1,
    lineStyle: window.LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: '',
  });
}

function priceLegend(lv, bid, last) {
  const px = bid != null ? bid : last;
  const chip = (label, val, color) => val == null ? '' :
    `<span class="plg" style="--c:${color}"><i></i>${label} <b>${money(val)}</b></span>`;
  let out = chip('Price', px, '#e6edf6');
  if (lv) {
    out += chip('Entry', lv.entry, '#4d9fff') + chip('SL', lv.sl, '#f25268')
      + chip('TP1', lv.tp1, '#e0a93b') + chip('TP2', lv.tp2, '#2ec27e');
    if (lv.entry != null && px != null) {        // live, direction-aware move vs entry
      const d = (px - lv.entry) * (lv.side === 'sell' ? -1 : 1);
      out += `<span class="plg ${d >= 0 ? 'g' : 'r'}" style="--c:${d >= 0 ? '#2ec27e' : '#f25268'}">`
        + `<i></i>${d >= 0 ? '▲ +' : '▼ '}${money(d)}</span>`;
    }
  }
  return out;
}

async function updatePrice() {
  initLwChart();
  const statusEl = document.getElementById('price-status');
  let q;
  try { q = await (await fetch('/api/quote?symbol=' + encodeURIComponent(chartSym) + '&tf=M1')).json(); }
  catch (e) { return; }
  if (!q || !q.ok) {
    if (statusEl) { statusEl.textContent = (q && q.error) ? ('MT5: ' + q.error) : 'connecting to MT5 for live price…'; statusEl.style.display = ''; }
    return;
  }
  if (!lwSeries) return;
  // ISO -> epoch seconds; keep strictly-ascending unique times (lib requirement)
  const data = [];
  let prevT = -1;
  for (const b of (q.bars || [])) {
    if (!b || b.t == null) continue;
    const t = Math.floor(Date.parse(b.t) / 1000);
    if (!Number.isFinite(t)) continue;
    const row = { time: t, open: b.o, high: b.h, low: b.l, close: b.c };
    if (t > prevT) { data.push(row); prevT = t; }
    else if (data.length) { data[data.length - 1] = row; }   // same minute => live update
  }
  if (data.length < 2) return;
  lwLows = data.map(b => b.low); lwHighs = data.map(b => b.high);
  if (q.bid != null) { lwLows.push(q.bid); lwHighs.push(q.bid); }
  lwSeries.setData(data);

  const lv = levelsFor(q.symbol);     // levels for the SYMBOL ON SCREEN, not latest-updated
  const sig = lv ? JSON.stringify([lv.entry, lv.sl, lv.tp1, lv.tp2, lv.side]) : '';
  if (lwSymLoaded !== q.symbol) { lwSymLoaded = q.symbol; lwLevelSig = ' '; lwChart.timeScale().fitContent(); }
  if (sig !== lwLevelSig) { lwLevelSig = sig; setLevelLines(lv); }
  setBidLine(q.bid);

  document.getElementById('price-title').innerHTML =
    `📈 ${q.symbol || ''} — LIVE ${q.tf || 'M1'} <span class="g stream">● streaming</span>`;
  document.getElementById('price-legend').innerHTML =
    priceLegend(lv, q.bid, data[data.length - 1].close);
  if (statusEl) statusEl.style.display = 'none';
}

function setSym(s) {            // live-chart symbol toggle (XAU <-> BTC)
  chartSym = s; lwSymLoaded = ''; lwLevelSig = '';
  if (lwSeries) {
    lwLines.forEach(l => lwSeries.removePriceLine(l)); lwLines = [];
    if (lwBidLine) { lwSeries.removePriceLine(lwBidLine); lwBidLine = null; }
  }
  updatePrice();
  document.querySelectorAll('.symbtn').forEach(b => b.classList.toggle('on', b.textContent === s));
}

/* ---------- per-strategy intelligence card ---------- */
function intelCard(s) {
  const buy = s.direction === 'buy', sell = s.direction === 'sell';
  const col = buy ? 'var(--grn)' : (sell ? 'var(--red)' : 'var(--mut)');
  const dir = buy ? '▲ BUY' : (sell ? '▼ SELL' : '—');
  const wr = s.trades ? Math.round(s.wins / s.trades * 100) : null;
  const active = s.roster === 'active';
  const rtag = active ? `<span class="rtag active">★ ACTIVE</span>`
    : `<span class="rtag shadow" title="benched: failed out-of-sample; the League monitors it and re-promotes if its edge returns">👻 SHADOW</span>`;
  const conds = (s.checklist || []).map(c => `<div class="cond ${c.passed ? '' : 'off'}">
    <span>${c.passed ? '🟢' : '⚪'} ${esc(c.name)}</span>
    <span class="pts">${c.passed ? c.points : 0}/${c.points}</span></div>`).join('');
  let stat;
  if (active) { stat = s.trades ? `${s.trades} trades · WR ${wr}% · ` + (s.pnl >= 0 ? '+' : '') + '$' + money(s.pnl) : 'no live trades'; }
  else if (s.sh_n) {
    const sw = Math.round(s.sh_wr * 100), ar = (s.sh_avgR >= 0 ? '+' : '') + s.sh_avgR.toFixed(2);
    stat = `<span style="color:${s.sh_avgR > 0 ? 'var(--grn)' : 'var(--mut)'}">👻 ${s.sh_n} paper · WR ${sw}% · avgR ${ar}</span>`;
  } else { stat = '👻 shadow · gathering paper-trades…'; }
  return `<div class="scard ${s.roster || ''}"><div class="top">
      <b>${rtag} ${esc(s.name)}</b>
      <span class="badge" style="background:${col}22;color:${col}">${dir} ${s.pct}%</span></div>
    <div class="sub muted" style="margin:4px 0">${s.family} · ${stat}</div>
    <div class="bar"><div style="width:${s.pct}%;background:${col}"></div></div>
    ${conds}</div>`;
}

/* ---------- kill-switch control ---------- */
async function postKill(mode, reason) {
  const msg = mode === 'running' ? 'Reset kill switch to RUNNING?'
    : (mode === 'close_all_halt' ? 'CLOSE ALL positions and HALT? The bot will flatten everything.' : 'Halt new entries?');
  if (!confirm(msg)) return;
  try {
    const r = await fetch('/api/kill_switch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, reason })
    });
    const j = await r.json(); if (!j.ok) alert('Failed: ' + (j.error || '?'));
  } catch (e) { alert('Request failed: ' + e); }
  tick();
}

/* ---------- main poll + render ---------- */
async function tick() {
  let d; try { d = await (await fetch('/api/status')).json(); } catch (e) { return; }
  document.getElementById('clock').textContent = new Date(d.now).toLocaleString();
  document.getElementById('session').innerHTML = '🕑 ' + (d.session || '—');
  const dot = document.getElementById('dot'), hb = d.heartbeat;
  dot.className = 'dot ' + (d.alive ? 'live' : 'stale');
  document.getElementById('hdr').innerHTML = d.profile
    ? `${d.profile.login} · ${d.profile.variant}/${d.profile.path}/${d.profile.phase} · `
      + (d.alive ? `<span class="g">LIVE</span>` : `<span class="r">STALE</span>`)
      + ` <span class="muted">(hb ${ago(hb.age_secs)})</span>` : '';

  document.getElementById('summary').innerHTML = summaryTiles(d, hb);

  const dsf = hb.daily_soft_floor;
  const cards = [];

  cards.push(`<div class="card"><h2>⚖️ Account &amp; Floors</h2>
    <div class="kv"><span>Equity</span><b>$${money(hb.equity)}</b></div>
    <div class="kv"><span>Balance</span><b>$${money(hb.balance)}</b></div>
    <div class="kv"><span class="muted">Open risk @ SL</span><b>$${money(hb.open_risk_to_sl)}</b></div>
    <hr>
    <div class="kv"><span>Daily soft (halt)</span><b class="y">$${money(dsf)}</b></div>
    <div class="kv"><span>Daily floor</span><b class="r">$${money(hb.daily_floor)}</b></div>
    <div class="kv"><span>Overall floor</span><b class="r">$${money(hb.overall_floor)}</b></div>
    <div class="kv"><span class="muted">Verdict</span><b>${hb.action || '—'} / ${hb.reason || '—'}</b></div></div>`);

  const ks = d.kill_switch || {};
  cards.push(`<div class="card"><h2>🛑 Kill Switch</h2>
    <div style="margin:4px 0 10px"><span class="pill ${ksClass(ks.mode)}" style="font-size:18px;padding:5px 16px">${(ks.mode || '?').toUpperCase()}</span></div>
    <div class="kv"><span class="muted">Reason</span><b>${ks.reason || '—'}</b></div>
    <div class="kv"><span class="muted">Source</span><b>${ks.source || '—'}</b></div>
    <div class="kv"><span class="muted">Tripped</span><b>${ks.tripped_at ? new Date(ks.tripped_at).toLocaleString() : '—'}</b></div>
    <div class="ctl">
      <button class="ok" onclick="postKill('running','manual_reset')">Reset (run)</button>
      <button class="warn" onclick="postKill('halt_new','dashboard_halt')">Halt new</button>
      <button class="danger" onclick="postKill('close_all_halt','dashboard_kill')">Close all &amp; halt</button>
    </div>
    <hr><h2 style="margin-top:4px">📅 Day Baseline (${d.baseline.cet_date})</h2>
    <div class="kv"><span>Midnight balance</span><b>$${money(d.baseline.midnight_balance)}</b>
       <span class="muted">${d.baseline.source || ''}</span></div></div>`);

  lastPositions = d.positions || [];   // live-chart overlay matches by symbol (see levelsFor)

  // live-chart symbol toggle (don't override a user pick once set)
  const syms = (d.intel_by_symbol || []).map(g => g.symbol);
  if (!chartSym || !syms.includes(chartSym)) chartSym = (d.chart && d.chart.symbol) || syms[0] || '';
  document.getElementById('symbar').innerHTML = syms.length > 1
    ? '<span class="muted" style="margin-right:6px">live chart:</span>' + syms.map(s =>
        `<button class="symbtn ${s === chartSym ? 'on' : ''}" onclick="setSym('${s}')">${s}</button>`).join('')
    : '';

  // 🧠 Strategy Intelligence — one section PER traded symbol (XAU / BTC separate)
  const intelSecs = (d.intel_by_symbol || []).map(g => {
    const rows = g.rows || [];
    const nAct = rows.filter(s => s.roster === 'active').length;
    return `<div class="symsec"><h3 class="symhdr">💱 ${g.symbol}
        <span class="sub">· ${g.tf || 'H1'}</span>
        <span class="rtag active">★ ${nAct}</span>
        <span class="rtag shadow">👻 ${rows.length - nAct}</span></h3>
      <div class="igrid">${rows.map(intelCard).join('') || '<div class="muted">no price data yet</div>'}</div></div>`;
  }).join('');
  cards.push(`<div class="card full"><h2>🧠 Strategy Intelligence — readiness per technique, by symbol
      <span class="sub muted">— only ★ ACTIVE techniques fire orders; 👻 SHADOW are benched &amp; monitored</span></h2>
    ${intelSecs || '<div class="muted">no price data yet — waiting for MT5 bars</div>'}</div>`);

  const perf = (d.perf_by_symbol || []).map(p => `<tr><td><b>${p.symbol}</b></td>
    <td>${p.trades}</td><td>${p.wins}</td>
    <td>${p.trades ? Math.round(p.win_rate * 100) + '%' : '—'}</td>
    <td class="${p.pnl >= 0 ? 'g' : 'r'}">${p.pnl >= 0 ? '+' : ''}$${money(p.pnl)}</td></tr>`).join('');
  cards.push(`<div class="card"><h2>💱 Performance by Symbol</h2>
    <div class="tw"><table><thead><tr><th>Symbol</th><th>Trades</th><th>W</th><th>Win%</th>
      <th>Net P&amp;L</th></tr></thead><tbody>
      ${perf || '<tr><td colspan=5 class="muted">no closed trades yet</td></tr>'}</tbody></table></div></div>`);

  cards.push(`<div class="card full"><h2>📈 Equity Curve</h2>${equityChart(d.equity_curve)}</div>`);

  const nw = (d.news || []).map(e => `<tr><td class="muted">${new Date(e.event_time).toLocaleString()}</td>
    <td>${e.currency}</td><td class="r">${e.impact}</td><td>${e.title}</td>
    <td>${e.forecast ?? '—'}</td><td>${e.previous ?? '—'}</td></tr>`).join('');
  cards.push(`<div class="card full"><h2>📰 Upcoming News — XAUUSD focus (USD high-impact)</h2>
    <div class="tw"><table><thead><tr><th>When</th><th>Ccy</th><th>Impact</th><th>Event</th>
      <th>Forecast</th><th>Previous</th></tr></thead><tbody>
      ${nw || '<tr><td colspan=6 class="muted">no upcoming high-impact USD events (feed = current week)</td></tr>'}</tbody></table></div></div>`);

  const pos = d.positions.map(p => `<tr><td>${p.ticket}</td><td>${p.symbol}</td>
    <td class="${p.side === 'buy' ? 'g' : 'r'}">${p.side}</td><td>${p.volume}</td>
    <td>${money(p.open_price)}</td><td>${p.sl ?? '—'}</td><td>${p.tp1 ?? '—'}</td><td>${p.tp2 ?? '—'}</td>
    <td><span class="pill ${p.mgmt === 'tp1_hit' ? 'bg-g' : 'bg-y'}">${p.mgmt || '—'}</span></td></tr>`).join('');
  cards.push(`<div class="card full"><h2>📂 Open Positions (${d.positions.length})</h2>
    <div class="tw"><table><thead><tr><th>Ticket</th><th>Symbol</th><th>Side</th><th>Vol</th>
      <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>Manage</th></tr></thead><tbody>
      ${pos || '<tr><td colspan=9 class="muted">none open</td></tr>'}</tbody></table></div></div>`);

  const tr = (d.trades || []).map(t => `<tr><td class="muted">${t.closed_at ? new Date(t.closed_at).toLocaleString() : '—'}</td>
    <td>${t.symbol}</td><td class="${t.side === 'buy' ? 'g' : 'r'}">${t.side}</td><td>${t.volume}</td>
    <td>${money(t.open_price)}</td><td class="${t.pnl >= 0 ? 'g' : 'r'}">${money(t.pnl)}</td>
    <td>${t.reason || '—'}</td><td class="muted">${t.strategy || '—'}</td></tr>`).join('');
  cards.push(`<div class="card full"><h2>📜 Trade History — closed (latest 20)</h2>
    <div class="tw"><table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Vol</th>
      <th>Entry</th><th>P&amp;L</th><th>Reason</th><th>Strategy</th></tr></thead><tbody>
      ${tr || '<tr><td colspan=8 class="muted">no closed trades yet</td></tr>'}</tbody></table></div></div>`);

  const rsn = (d.reasoning || []).map(o => `<div class="reason">
    <div><b>${new Date(o.created_at).toLocaleString()}</b> ·
      <span class="${o.side === 'buy' ? 'g' : 'r'}">${(o.side || '').toUpperCase()} ${o.symbol}</span>
      ${o.volume} · <span class="pill ${o.regime === 'trend' ? 'bg-g' : 'bg-y'}">${o.regime || '?'}</span>
      · <span class="muted">${o.status}</span></div>
    <div class="why">${esc(o.rationale)}</div></div>`).join('');
  cards.push(`<div class="card full"><h2>🔍 Trade Reasoning — why each order was taken</h2>
    ${rsn || '<div class="muted">no reasoned orders yet — confluence requires ≥2 techniques across ≥2 families</div>'}</div>`);

  const lg = d.league.map(r => `<tr><td>${r.strategy_id}</td>
    <td><span class="pill ${r.regime === 'trend' ? 'bg-g' : 'bg-y'}">${r.regime}</span></td>
    <td>${r.trades}</td><td>${r.wins}</td>
    <td class="${r.gross_pnl >= 0 ? 'g' : 'r'}">${money(r.gross_pnl)}</td>
    <td class="${stClass(r.status)}">${r.status}</td></tr>`).join('');
  cards.push(`<div class="card"><h2>🏆 Strategy League</h2>
    <div class="tw"><table><thead><tr><th>Strategy</th><th>Regime</th><th>Trades</th><th>W</th>
      <th>PnL</th><th>Status</th></tr></thead><tbody>
      ${lg || '<tr><td colspan=6 class="muted">no standings yet</td></tr>'}</tbody></table></div></div>`);

  const ord = d.orders.map(o => `<tr><td class="muted">${new Date(o.created_at).toLocaleTimeString()}</td>
    <td>${o.symbol}</td><td class="${o.side === 'buy' ? 'g' : 'r'}">${o.side}</td>
    <td>${o.volume}</td><td>${o.regime || '—'}</td><td>${o.status}</td></tr>`).join('');
  cards.push(`<div class="card"><h2>🧾 Recent Orders</h2>
    <div class="tw"><table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Vol</th>
      <th>Regime</th><th>Status</th></tr></thead><tbody>
      ${ord || '<tr><td colspan=6 class="muted">none</td></tr>'}</tbody></table></div></div>`);

  const cat = (d.catalog || []).map(s => {
    const a = s.roster === 'active';
    return `<tr style="${a ? '' : 'opacity:.5'}"><td>${a ? '★ ' : ''}${s.name}</td>
    <td><span class="pill ${s.regime === 'trend' ? 'bg-g' : 'bg-y'}">${s.family}</span></td>
    <td>${s.regime}</td><td><span class="rtag ${a ? 'active' : 'shadow'}">${a ? '★ ACTIVE' : '👻 SHADOW'}</span></td>
    <td class="muted">${s.fits}</td></tr>`;
  }).join('');
  cards.push(`<div class="card full"><h2>📖 Strategy Playbook — 13 techniques · 4 active, 9 shadow</h2>
    <div class="tw"><table><thead><tr><th>Technique</th><th>Family</th><th>Best regime</th>
      <th>Roster</th><th>Fits</th></tr></thead><tbody>${cat}</tbody></table></div></div>`);

  const colorEv = e => e === 'kill' ? 'r' : (e === 'veto' || e === 'order' ? 'b' : (e === 'rollover' ? 'y' : ''));
  const au = d.audit.map(a => `<tr><td class="muted">${new Date(a.ts).toLocaleTimeString()}</td>
    <td class="${colorEv(a.event_type)}">${a.event_type}</td><td>${a.decision || ''}</td>
    <td class="muted">${a.reason || ''}</td></tr>`).join('');
  cards.push(`<div class="card full"><h2>📋 Audit Log (latest 30)</h2>
    <div class="tw"><table><thead><tr><th>Time</th><th>Event</th><th>Decision</th><th>Reason</th>
      </tr></thead><tbody>${au}</tbody></table></div></div>`);

  document.getElementById('grid').innerHTML = cards.join('');
}

tick(); setInterval(tick, 3000);
updatePrice(); setInterval(updatePrice, 1000);   // MetaTrader-style tick chart
