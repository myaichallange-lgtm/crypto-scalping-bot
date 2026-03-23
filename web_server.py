#!/usr/bin/env python3
"""
web_server.py - Browser dashboard for the Scalping Bot
Serves a live auto-refreshing HTML dashboard on http://0.0.0.0:8765

Run alongside main.py:
    python3 web_server.py
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))

from bot.config import Config
from bot.exchange import ExchangeClient
from bot.indicators import ohlcv_to_df, compute_indicators, generate_signal, compute_trend_bias

DATA_DIR = Path(__file__).parent / "data"
LOG_DIR  = Path(__file__).parent / "logs"
PORT     = 8765

_exchange: ExchangeClient | None = None
_start_time = datetime.now(timezone.utc)


async def get_exchange() -> ExchangeClient:
    global _exchange
    if _exchange is None:
        _exchange = ExchangeClient()
        await _exchange.connect()
    return _exchange


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_journal() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path  = DATA_DIR / f"journal_{today}.json"
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"daily_pnl": 0, "trade_count": 0, "wins": 0, "losses": 0, "win_rate": 0}


def tail_log(n: int = 20) -> list[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path  = LOG_DIR / f"bot_{today}.log"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
        clean = []
        for line in lines[-120:]:
            line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
            if line:
                clean.append(line)
        return clean[-n:]
    except Exception:
        return []


def get_uptime() -> str:
    delta = datetime.now(timezone.utc) - _start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── API endpoint ──────────────────────────────────────────────────────────────

async def api_status(request):
    try:
        ex      = await get_exchange()
        balance = await ex.get_usdt_balance()
        raw_pos = await ex.fetch_open_positions()

        market_data = []
        for symbol in Config.PAIRS:
            try:
                ohlcv_1m, ohlcv_5m = await asyncio.gather(
                    ex.fetch_ohlcv(symbol, "1m", limit=500),
                    ex.fetch_ohlcv(symbol, "5m", limit=200),
                )
                df_1m  = compute_indicators(ohlcv_to_df(ohlcv_1m))
                df_5m  = compute_indicators(ohlcv_to_df(ohlcv_5m))
                trend  = compute_trend_bias(df_5m)
                sig    = generate_signal(df_1m, df_5m)
                last   = df_1m.iloc[-1]
                market_data.append({
                    "symbol": symbol,
                    "price":  round(float(last["close"]), 4),
                    "trend":  trend,
                    "rsi":    round(float(last["rsi"]), 1),
                    "atr":    round(float(last["atr"]), 4),
                    "ema_fast": round(float(last["ema_fast"]), 4),
                    "ema_slow": round(float(last["ema_slow"]), 4),
                    "cross":  bool(df_1m["ema_cross_up"].iloc[-3:].any() or
                                   df_1m["ema_cross_down"].iloc[-3:].any()),
                    "vol":    bool(last["vol_spike"]),
                    "vwap":   round(float(last["vwap"]), 4),
                    "bb_upper": round(float(last["bb_upper"]), 4),
                    "bb_lower": round(float(last["bb_lower"]), 4),
                    "signal": sig.signal,
                    "reason": sig.reason,
                })
            except Exception as e:
                market_data.append({
                    "symbol": symbol, "price": 0, "trend": "neutral",
                    "rsi": 0, "atr": 0, "ema_fast": 0, "ema_slow": 0,
                    "cross": False, "vol": False, "vwap": 0,
                    "bb_upper": 0, "bb_lower": 0,
                    "signal": "error", "reason": str(e),
                })

        positions = []
        for p in raw_pos:
            positions.append({
                "symbol":    p.get("symbol", ""),
                "side":      p.get("side", ""),
                "entry":     float(p.get("entryPrice", 0)),
                "size":      float(p.get("contracts", 0)),
                "unr_pnl":   float(p.get("unrealizedProfit", 0) or 0),
                "liq_price": float(p.get("liquidationPrice", 0) or 0),
            })

        journal = load_journal()
        logs    = tail_log(25)
        initial = Config.INITIAL_BALANCE

        payload = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "uptime":      get_uptime(),
            "balance":     round(balance, 2),
            "initial":     initial,
            "balance_pct": round((balance - initial) / initial * 100, 2) if initial else 0,
            "daily_pnl":   round(journal.get("daily_pnl", 0), 4),
            "trades":      journal.get("trade_count", 0),
            "wins":        journal.get("wins", 0),
            "losses":      journal.get("losses", 0),
            "win_rate":    round(journal.get("win_rate", 0) * 100, 1),
            "daily_limit": Config.DAILY_DRAWDOWN_LIMIT,
            "leverage":    Config.LEVERAGE,
            "risk_pct":    Config.MAX_RISK_PER_TRADE,
            "market":      market_data,
            "positions":   positions,
            "logs":        logs,
            "testnet":     Config.TESTNET,
        }
        return web.Response(
            text=json.dumps(payload),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        return web.Response(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
            status=500,
        )


# ── HTML dashboard ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scalping Bot Dashboard</title>
<style>
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #d29922;
    --blue:    #388bfd;
    --purple:  #a371f7;
    --cyan:    #39d353;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 13px; min-height: 100vh;
  }
  #app { max-width: 1400px; margin: 0 auto; padding: 16px; }

  /* Header */
  .header {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 24px; margin-bottom: 16px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .header-title { display: flex; align-items: center; gap: 10px; }
  .header-title h1 { font-size: 18px; font-weight: 700; }
  .badge {
    font-size: 10px; font-weight: 700; padding: 2px 8px;
    border-radius: 20px; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .badge-testnet { background: #2d2d00; color: var(--yellow); border: 1px solid var(--yellow); }
  .badge-live    { background: #1a0000; color: var(--red);    border: 1px solid var(--red); }
  .header-meta { color: var(--muted); font-size: 11px; text-align: right; }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); margin-right: 5px;
    animation: pulse 1.5s infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.4; transform: scale(0.8); }
  }

  /* Stats row */
  .stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }
  .stat-label { color: var(--muted); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.8px; margin-bottom: 6px; }
  .stat-value { font-size: 20px; font-weight: 700; }
  .stat-sub   { font-size: 11px; color: var(--muted); margin-top: 3px; }

  /* Main grid */
  .main-grid { display: grid; grid-template-columns: 1fr 340px; gap: 16px; margin-bottom: 16px; }

  /* Cards */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
  }
  .card-header {
    padding: 10px 16px; border-bottom: 1px solid var(--border);
    font-weight: 600; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.6px; color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }

  /* Market table */
  table { width: 100%; border-collapse: collapse; }
  th {
    padding: 10px 14px; text-align: left; font-size: 10px;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px;
    border-bottom: 1px solid var(--border); font-weight: 600;
  }
  td { padding: 11px 14px; border-bottom: 1px solid #1e252e; font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }
  th.right, td.right { text-align: right; }
  th.center, td.center { text-align: center; }

  /* Tags */
  .tag {
    display: inline-block; padding: 2px 8px; border-radius: 20px;
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .tag-bull   { background: #0d2a18; color: var(--green); border: 1px solid #1f4d2f; }
  .tag-bear   { background: #2a0d0d; color: var(--red);   border: 1px solid #4d1f1f; }
  .tag-neutral{ background: #1e1e1e; color: var(--muted); border: 1px solid var(--border); }
  .tag-long   { background: #0d2a18; color: var(--green); border: 1px solid #1f4d2f; }
  .tag-short  { background: #2a0d0d; color: var(--red);   border: 1px solid #4d1f1f; }
  .tag-none   { background: #1e1e1e; color: var(--muted); border: 1px solid var(--border); }
  .tag-error  { background: #2a1a0d; color: var(--yellow); border: 1px solid var(--yellow); }

  /* Check / cross */
  .yes { color: var(--green); font-weight: 700; }
  .no  { color: var(--muted); }

  /* RSI color */
  .rsi-hot   { color: var(--red);    font-weight: 700; }
  .rsi-cold  { color: var(--cyan);   font-weight: 700; }
  .rsi-warm  { color: var(--yellow); }
  .rsi-cool  { color: var(--blue);  }
  .rsi-ok    { color: var(--text);  }

  /* P&L */
  .pos { color: var(--green); font-weight: 600; }
  .neg { color: var(--red);   font-weight: 600; }

  /* Log panel */
  .log-box {
    height: 280px; overflow-y: auto; padding: 10px 14px;
    font-family: 'Consolas', 'Courier New', monospace; font-size: 11px;
    line-height: 1.7;
  }
  .log-line { color: var(--muted); }
  .log-signal  { color: var(--green); }
  .log-win     { color: var(--green); font-weight: 600; }
  .log-loss    { color: var(--red);   font-weight: 600; }
  .log-error   { color: var(--red); }
  .log-warning { color: var(--yellow); }
  .log-trade   { color: var(--cyan); }

  /* Risk bar */
  .risk-row { display: flex; justify-content: space-between; align-items: center;
    padding: 8px 16px; border-bottom: 1px solid #1e252e; }
  .risk-row:last-child { border-bottom: none; }
  .risk-key   { color: var(--muted); font-size: 11px; }
  .risk-val   { font-weight: 600; font-size: 12px; }
  .progress-wrap { background: #1e252e; border-radius: 4px; height: 6px; width: 160px; overflow: hidden; }
  .progress-bar  { height: 100%; border-radius: 4px; transition: width 0.5s ease; }

  /* Positions */
  .empty-state {
    padding: 32px; text-align: center; color: var(--muted); font-size: 12px;
  }

  /* Bottom grid */
  .bottom-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

  /* Refresh indicator */
  .refresh-bar {
    position: fixed; bottom: 12px; right: 16px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 12px; font-size: 11px; color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }
  .spinner {
    width: 10px; height: 10px; border: 2px solid var(--border);
    border-top-color: var(--blue); border-radius: 50%;
    animation: spin 0.8s linear infinite; display: none;
  }
  .spinner.active { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 900px) {
    .stats { grid-template-columns: repeat(3, 1fr); }
    .main-grid { grid-template-columns: 1fr; }
    .bottom-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div id="app">

  <!-- Header -->
  <div class="header">
    <div class="header-title">
      <span style="font-size:22px">🤖</span>
      <h1>Scalping Bot</h1>
      <span id="badge" class="badge badge-testnet">Testnet</span>
    </div>
    <div class="header-meta">
      <div><span class="pulse"></span>Live · refreshes every 6s</div>
      <div style="margin-top:4px">Uptime: <span id="uptime">—</span></div>
      <div style="margin-top:2px">Last update: <span id="last-update">—</span></div>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat-card">
      <div class="stat-label">💰 Balance</div>
      <div class="stat-value" id="balance">—</div>
      <div class="stat-sub" id="balance-pct">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">📈 Daily P&L</div>
      <div class="stat-value" id="daily-pnl">—</div>
      <div class="stat-sub" id="daily-pnl-start">Started at $<span id="initial">—</span></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">🎯 Trades Today</div>
      <div class="stat-value" id="trades">—</div>
      <div class="stat-sub" id="wl">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">✅ Win Rate</div>
      <div class="stat-value" id="win-rate">—</div>
      <div class="stat-sub">last session</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">📂 Open Positions</div>
      <div class="stat-value" id="pos-count">—</div>
      <div class="stat-sub">max 3 allowed</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">⚙️ Leverage</div>
      <div class="stat-value" id="leverage">—</div>
      <div class="stat-sub" id="risk-pct">—</div>
    </div>
  </div>

  <!-- Main: market table + risk -->
  <div class="main-grid">
    <div class="card">
      <div class="card-header">📊 Live Market Scanner</div>
      <table id="market-table">
        <thead>
          <tr>
            <th>Pair</th>
            <th class="right">Price</th>
            <th class="center">Trend 5m</th>
            <th class="center">RSI(7)</th>
            <th class="right">ATR</th>
            <th class="right">VWAP</th>
            <th class="center">EMA Cross</th>
            <th class="center">Vol Spike</th>
            <th class="center">Signal</th>
          </tr>
        </thead>
        <tbody id="market-body">
          <tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px">Loading…</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-header">🛡️ Risk Controls</div>
      <div id="risk-panel"></div>
    </div>
  </div>

  <!-- Positions -->
  <div class="card" style="margin-bottom:16px">
    <div class="card-header">📂 Open Positions</div>
    <div id="positions-panel"></div>
  </div>

  <!-- Bottom: log + signals -->
  <div class="bottom-grid">
    <div class="card">
      <div class="card-header">📋 Bot Log (live)</div>
      <div class="log-box" id="log-box">
        <div class="log-line">Connecting…</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">💡 Signal Conditions</div>
      <div id="signal-detail" style="padding:14px"></div>
    </div>
  </div>

</div>

<!-- Refresh bar -->
<div class="refresh-bar">
  <div class="spinner" id="spinner"></div>
  <span id="countdown">Next refresh in 6s</span>
</div>

<script>
const API = '/api/status';
let countdown = 6;

function fmt(n, dec=2) { return Number(n).toFixed(dec); }
function fmtPrice(n) { return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:4}); }
function pnlClass(n) { return n >= 0 ? 'pos' : 'neg'; }
function pnlStr(n)   { return (n >= 0 ? '+' : '') + fmt(n, 4) + ' USDT'; }

function rsiClass(r) {
  if (r >= 70) return 'rsi-hot';
  if (r <= 30) return 'rsi-cold';
  if (r >= 60) return 'rsi-warm';
  if (r <= 40) return 'rsi-cool';
  return 'rsi-ok';
}

function trendTag(t) {
  return `<span class="tag tag-${t}">${t.toUpperCase()}</span>`;
}

function signalTag(s) {
  if (s === 'long')  return `<span class="tag tag-long">▲ LONG</span>`;
  if (s === 'short') return `<span class="tag tag-short">▼ SHORT</span>`;
  if (s === 'error') return `<span class="tag tag-error">ERR</span>`;
  return `<span class="tag tag-none">—</span>`;
}

function logClass(line) {
  if (line.includes('SIGNAL') || line.includes('opened'))  return 'log-signal';
  if (line.includes('WIN'))    return 'log-win';
  if (line.includes('LOSS'))   return 'log-loss';
  if (line.includes('ERROR'))  return 'log-error';
  if (line.includes('WARNING'))return 'log-warning';
  if (line.includes('closed') || line.includes('Trailing')) return 'log-trade';
  return 'log-line';
}

async function refresh() {
  document.getElementById('spinner').classList.add('active');
  try {
    const res  = await fetch(API);
    const data = await res.json();
    if (data.error) { console.error(data.error); return; }

    // Header
    document.getElementById('uptime').textContent = data.uptime;
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
    const badge = document.getElementById('badge');
    badge.textContent = data.testnet ? 'Testnet' : '🔴 LIVE';
    badge.className = 'badge ' + (data.testnet ? 'badge-testnet' : 'badge-live');

    // Stats
    document.getElementById('balance').textContent = '$' + Number(data.balance).toLocaleString('en-US', {minimumFractionDigits:2});
    const bpEl = document.getElementById('balance-pct');
    bpEl.textContent = (data.balance_pct >= 0 ? '+' : '') + fmt(data.balance_pct,2) + '% from start';
    bpEl.className = 'stat-sub ' + pnlClass(data.balance_pct);

    const pnlEl = document.getElementById('daily-pnl');
    pnlEl.textContent = pnlStr(data.daily_pnl);
    pnlEl.className = 'stat-value ' + pnlClass(data.daily_pnl);

    document.getElementById('initial').textContent = fmt(data.initial, 0);
    document.getElementById('trades').textContent  = data.trades;
    document.getElementById('wl').textContent      = data.wins + 'W / ' + data.losses + 'L';
    document.getElementById('win-rate').textContent = fmt(data.win_rate, 1) + '%';
    document.getElementById('pos-count').textContent = data.positions.length;
    document.getElementById('leverage').textContent   = data.leverage + '×';
    document.getElementById('risk-pct').textContent   = (data.risk_pct * 100).toFixed(0) + '% risk per trade';

    // Market table
    const tbody = document.getElementById('market-body');
    tbody.innerHTML = data.market.map(m => `
      <tr>
        <td><strong>${m.symbol}</strong></td>
        <td class="right"><strong>${fmtPrice(m.price)}</strong></td>
        <td class="center">${trendTag(m.trend)}</td>
        <td class="center"><span class="${rsiClass(m.rsi)}">${fmt(m.rsi,1)}</span></td>
        <td class="right">${fmt(m.atr,4)}</td>
        <td class="right">${fmtPrice(m.vwap)}</td>
        <td class="center"><span class="${m.cross ? 'yes' : 'no'}">${m.cross ? '✓' : '—'}</span></td>
        <td class="center"><span class="${m.vol ? 'yes' : 'no'}">${m.vol ? '✓' : '—'}</span></td>
        <td class="center">${signalTag(m.signal)}</td>
      </tr>
    `).join('');

    // Risk panel
    const dailyUsed = Math.abs(Math.min(data.daily_pnl, 0)) / (data.initial || 1);
    const dailyPct  = Math.min(dailyUsed / data.daily_limit * 100, 100);
    const barColor  = dailyPct > 70 ? '#f85149' : '#3fb950';
    document.getElementById('risk-panel').innerHTML = `
      <div class="risk-row"><span class="risk-key">Leverage</span><span class="risk-val">${data.leverage}×</span></div>
      <div class="risk-row"><span class="risk-key">Risk per trade</span><span class="risk-val">${(data.risk_pct*100).toFixed(0)}%</span></div>
      <div class="risk-row"><span class="risk-key">Max positions</span><span class="risk-val">3</span></div>
      <div class="risk-row"><span class="risk-key">Daily halt limit</span><span class="risk-val">−${(data.daily_limit*100).toFixed(0)}%</span></div>
      <div class="risk-row">
        <span class="risk-key">Daily drawdown</span>
        <span class="risk-val" style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
          <span>${fmt(dailyPct,1)}% used</span>
          <div class="progress-wrap">
            <div class="progress-bar" style="width:${dailyPct}%;background:${barColor}"></div>
          </div>
        </span>
      </div>
      <div class="risk-row"><span class="risk-key">Peak drawdown halt</span><span class="risk-val">−25%</span></div>
      <div class="risk-row"><span class="risk-key">Trailing stop</span><span class="risk-val" style="color:var(--green)">Active</span></div>
      <div class="risk-row"><span class="risk-key">SL multiplier</span><span class="risk-val">1.5× ATR</span></div>
      <div class="risk-row"><span class="risk-key">TP multiplier</span><span class="risk-val">2.5× ATR</span></div>
    `;

    // Positions
    const posPanel = document.getElementById('positions-panel');
    if (data.positions.length === 0) {
      posPanel.innerHTML = '<div class="empty-state">No open positions — bot is watching for signals</div>';
    } else {
      posPanel.innerHTML = `
        <table>
          <thead><tr>
            <th>Symbol</th><th>Side</th>
            <th class="right">Entry Price</th>
            <th class="right">Size</th>
            <th class="right">Liq. Price</th>
            <th class="right">Unr. P&L</th>
          </tr></thead>
          <tbody>
            ${data.positions.map(p => `
              <tr>
                <td><strong>${p.symbol}</strong></td>
                <td>${signalTag(p.side === 'long' ? 'long' : 'short')}</td>
                <td class="right">${fmtPrice(p.entry)}</td>
                <td class="right">${p.size}</td>
                <td class="right">${fmtPrice(p.liq_price)}</td>
                <td class="right"><span class="${pnlClass(p.unr_pnl)}">${pnlStr(p.unr_pnl)}</span></td>
              </tr>
            `).join('')}
          </tbody>
        </table>`;
    }

    // Log
    const logBox = document.getElementById('log-box');
    logBox.innerHTML = (data.logs.length
      ? data.logs.map(l => `<div class="${logClass(l)}">${escHtml(l)}</div>`).join('')
      : '<div class="log-line" style="color:var(--muted)">No log entries yet</div>');
    logBox.scrollTop = logBox.scrollHeight;

    // Signal detail
    const sigDetail = document.getElementById('signal-detail');
    sigDetail.innerHTML = data.market.map(m => `
      <div style="margin-bottom:14px">
        <div style="font-weight:600;margin-bottom:6px">${m.symbol} ${signalTag(m.signal)}</div>
        <div style="color:var(--muted);font-size:11px;line-height:1.8">
          ${m.reason || 'Awaiting confluence…'}<br>
          EMA9 <span style="color:var(--cyan)">${fmtPrice(m.ema_fast)}</span> /
          EMA21 <span style="color:var(--purple)">${fmtPrice(m.ema_slow)}</span><br>
          BB upper <span>${fmtPrice(m.bb_upper)}</span> /
          BB lower <span>${fmtPrice(m.bb_lower)}</span>
        </div>
      </div>
    `).join('<hr style="border:none;border-top:1px solid var(--border);margin:8px 0">');

  } catch(e) {
    console.error('Refresh error:', e);
  } finally {
    document.getElementById('spinner').classList.remove('active');
  }
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Countdown timer
setInterval(() => {
  countdown--;
  if (countdown <= 0) {
    countdown = 6;
    refresh();
  }
  document.getElementById('countdown').textContent = `Next refresh in ${countdown}s`;
}, 1000);

// Initial load
refresh();
</script>
</body>
</html>
"""


async def root(request):
    return web.Response(text=HTML, content_type="text/html")


async def make_app():
    app = web.Application()
    app.router.add_get("/",           root)
    app.router.add_get("/api/status", api_status)
    return app


if __name__ == "__main__":
    async def main():
        app = await make_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        print(f"\n🌐  Dashboard running at http://0.0.0.0:{PORT}")
        print(f"    Press Ctrl+C to stop\n")
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
