#!/usr/bin/env python3
"""
dashboard.py - Live terminal dashboard for the Scalping Bot
Runs independently alongside main.py — read-only, never places orders.

Usage:
    python3 dashboard.py
"""

import asyncio
import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from rich.columns import Columns
from rich.align import Align

sys.path.insert(0, str(Path(__file__).parent))

from bot.config import Config
from bot.exchange import ExchangeClient
from bot.indicators import ohlcv_to_df, compute_indicators, generate_signal, compute_trend_bias

REFRESH_SECONDS = 6
DATA_DIR  = Path(__file__).parent / "data"
LOG_DIR   = Path(__file__).parent / "logs"
console   = Console()

# ── Colour helpers ────────────────────────────────────────────────────────────

def colour_pnl(val: float) -> Text:
    s = f"{val:+.4f} USDT"
    return Text(s, style="bold green" if val >= 0 else "bold red")

def colour_side(side: str) -> Text:
    return Text(side.upper(), style="bold green" if side.lower() == "long" else "bold red")

def colour_trend(trend: str) -> Text:
    colours = {"bull": "green", "bear": "red", "neutral": "dim white"}
    return Text(trend.upper(), style=colours.get(trend, "white"))

def colour_rsi(rsi: float) -> Text:
    if rsi >= 70:
        return Text(f"{rsi:.1f}", style="bold red")
    elif rsi <= 30:
        return Text(f"{rsi:.1f}", style="bold green")
    elif rsi >= 60:
        return Text(f"{rsi:.1f}", style="yellow")
    elif rsi <= 40:
        return Text(f"{rsi:.1f}", style="cyan")
    return Text(f"{rsi:.1f}", style="white")

def colour_signal(sig: str) -> Text:
    if sig == "long":
        return Text("▲ LONG", style="bold green")
    elif sig == "short":
        return Text("▼ SHORT", style="bold red")
    return Text("—  none", style="dim")

# ── Data loaders ──────────────────────────────────────────────────────────────

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

def tail_log(n: int = 12) -> list[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path  = LOG_DIR / f"bot_{today}.log"
    if not path.exists():
        return ["No log file yet"]
    try:
        with open(path) as f:
            lines = f.readlines()
        # Strip ANSI and log-level noise, keep last n meaningful lines
        clean = []
        for line in lines[-60:]:
            line = line.strip()
            # Remove ANSI escape codes
            import re
            line = re.sub(r'\x1b\[[0-9;]*m', '', line)
            if line:
                clean.append(line)
        return clean[-n:]
    except Exception:
        return ["Could not read log"]

# ── Panel builders ────────────────────────────────────────────────────────────

def make_header(balance: float, journal: dict, uptime: str) -> Panel:
    initial    = Config.INITIAL_BALANCE
    daily_pnl  = journal.get("daily_pnl", 0)
    trades     = journal.get("trade_count", 0)
    wins       = journal.get("wins", 0)
    losses     = journal.get("losses", 0)
    win_rate   = journal.get("win_rate", 0)
    daily_pct  = (daily_pnl / initial * 100) if initial else 0
    balance_pct = ((balance - initial) / initial * 100) if initial else 0

    t = Table.grid(padding=(0, 3))
    t.add_column(justify="center")
    t.add_column(justify="center")
    t.add_column(justify="center")
    t.add_column(justify="center")
    t.add_column(justify="center")
    t.add_column(justify="center")

    t.add_row(
        Text("BALANCE", style="bold dim"),
        Text("DAILY P&L", style="bold dim"),
        Text("TRADES", style="bold dim"),
        Text("WIN RATE", style="bold dim"),
        Text("W / L", style="bold dim"),
        Text("UPTIME", style="bold dim"),
    )
    t.add_row(
        Text(f"${balance:,.2f}  ({balance_pct:+.1f}%)", style="bold cyan"),
        colour_pnl(daily_pnl) if daily_pnl != 0 else Text(f"{daily_pct:+.2f}%", style="dim"),
        Text(str(trades), style="bold white"),
        Text(f"{win_rate:.0%}", style="bold yellow"),
        Text(f"{wins}W / {losses}L", style="white"),
        Text(uptime, style="dim"),
    )
    return Panel(Align.center(t), title="[bold cyan]🤖  Scalping Bot  ·  Binance Futures Testnet[/]",
                 border_style="cyan", padding=(0, 1))


def make_market_table(market_data: list[dict]) -> Panel:
    t = Table(box=box.SIMPLE_HEAD, show_header=True,
              header_style="bold magenta", expand=True)

    for col, justify in [
        ("Pair", "left"), ("Price", "right"), ("Trend 5m", "center"),
        ("RSI(7)", "center"), ("ATR", "right"), ("EMA cross", "center"),
        ("Vol spike", "center"), ("VWAP", "right"), ("Signal", "center"),
    ]:
        t.add_column(col, justify=justify)

    for row in market_data:
        ema_cross = "✓" if row["cross"] else "—"
        vol       = "✓" if row["vol"]   else "—"
        t.add_row(
            Text(row["symbol"], style="bold white"),
            Text(f"${row['price']:,.4f}"),
            colour_trend(row["trend"]),
            colour_rsi(row["rsi"]),
            Text(f"{row['atr']:.4f}"),
            Text(ema_cross, style="green" if row["cross"] else "dim"),
            Text(vol,       style="green" if row["vol"]   else "dim"),
            Text(f"${row['vwap']:,.4f}"),
            colour_signal(row["signal"]),
        )

    return Panel(t, title="[bold magenta]📊  Live Market Scanner[/]",
                 border_style="magenta", padding=(0, 1))


def make_positions_panel(positions: list[dict]) -> Panel:
    if not positions:
        content = Align.center(Text("No open positions", style="dim"), vertical="middle")
    else:
        t = Table(box=box.SIMPLE_HEAD, header_style="bold yellow", expand=True)
        for col in ["Symbol", "Side", "Entry", "Stop Loss", "Take Profit", "Size", "Unr. PnL"]:
            t.add_column(col, justify="right" if col not in ("Symbol", "Side") else "left")
        for p in positions:
            unr_pnl = p.get("unrealizedProfit", 0) or 0
            t.add_row(
                Text(p.get("symbol", ""), style="bold white"),
                colour_side(p.get("side", "long")),
                f"${float(p.get('entryPrice', 0)):,.4f}",
                "—",
                "—",
                str(p.get("contracts", "?")),
                colour_pnl(float(unr_pnl)),
            )
        content = t

    return Panel(content, title="[bold yellow]📂  Open Positions[/]",
                 border_style="yellow", padding=(0, 1))


def make_log_panel(lines: list[str]) -> Panel:
    text = Text()
    keyword_styles = {
        "SIGNAL":  "bold green",
        "WIN":     "bold green",
        "LOSS":    "bold red",
        "ERROR":   "bold red",
        "WARNING": "yellow",
        "opened":  "cyan",
        "closed":  "magenta",
        "Trailing": "yellow",
        "halted":  "bold red",
    }
    for line in lines:
        # Trim long lines
        trimmed = line[-120:] if len(line) > 120 else line
        style = "dim"
        for kw, st in keyword_styles.items():
            if kw in line:
                style = st
                break
        text.append(trimmed + "\n", style=style)

    return Panel(text, title="[bold blue]📋  Bot Log (live)[/]",
                 border_style="blue", padding=(0, 1))


def make_risk_panel(journal: dict, balance: float) -> Panel:
    initial        = Config.INITIAL_BALANCE
    daily_pnl      = journal.get("daily_pnl", 0)
    daily_limit    = Config.DAILY_DRAWDOWN_LIMIT
    daily_used_pct = abs(min(daily_pnl, 0)) / initial if initial else 0
    dd_bar_len     = 20
    dd_filled      = int(daily_used_pct / daily_limit * dd_bar_len) if daily_limit else 0
    dd_filled      = min(dd_filled, dd_bar_len)
    dd_bar_colour  = "red" if daily_used_pct > 0.7 * daily_limit else "green"

    bar = f"[{dd_bar_colour}]{'█' * dd_filled}{'░' * (dd_bar_len - dd_filled)}[/]"

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left", style="dim")
    t.add_column(justify="left")

    t.add_row("Risk / trade",   f"{Config.MAX_RISK_PER_TRADE:.0%}")
    t.add_row("Leverage",       f"{Config.LEVERAGE}×")
    t.add_row("Max positions",  str(Config.MAX_OPEN_POSITIONS))
    t.add_row("Daily halt at",  f"−{daily_limit:.0%}")
    t.add_row("Daily DD used",  f"{bar}  {daily_used_pct:.1%} / {daily_limit:.0%}")
    t.add_row("Pairs",          ", ".join(Config.PAIRS))

    return Panel(t, title="[bold red]🛡️  Risk Controls[/]",
                 border_style="red", padding=(0, 1))


# ── Main dashboard loop ───────────────────────────────────────────────────────

async def run_dashboard():
    ex = ExchangeClient()
    await ex.connect()

    start_time = datetime.now(timezone.utc)

    def get_uptime() -> str:
        delta = datetime.now(timezone.utc) - start_time
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=5),
        Layout(name="market",    size=13),
        Layout(name="bottom",    ratio=1),
    )
    layout["bottom"].split_row(
        Layout(name="positions", ratio=3),
        Layout(name="right",     ratio=2),
    )
    layout["right"].split_column(
        Layout(name="log",  ratio=3),
        Layout(name="risk", ratio=2),
    )

    async def refresh():
        # ── Fetch live data ──────────────────────────────────────────────────
        try:
            balance = await ex.get_usdt_balance()
        except Exception:
            balance = Config.INITIAL_BALANCE

        try:
            raw_positions = await ex.fetch_open_positions()
        except Exception:
            raw_positions = []

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
                    "price":  float(last["close"]),
                    "trend":  trend,
                    "rsi":    float(last["rsi"]),
                    "atr":    float(last["atr"]),
                    "cross":  bool(df_1m["ema_cross_up"].iloc[-3:].any() or
                                   df_1m["ema_cross_down"].iloc[-3:].any()),
                    "vol":    bool(last["vol_spike"]),
                    "vwap":   float(last["vwap"]),
                    "signal": sig.signal,
                    "reason": sig.reason,
                })
            except Exception as e:
                market_data.append({
                    "symbol": symbol, "price": 0, "trend": "neutral",
                    "rsi": 0, "atr": 0, "cross": False, "vol": False,
                    "vwap": 0, "signal": "none", "reason": str(e),
                })

        journal    = load_journal()
        log_lines  = tail_log(14)
        uptime     = get_uptime()

        # ── Render panels ────────────────────────────────────────────────────
        layout["header"].update(make_header(balance, journal, uptime))
        layout["market"].update(make_market_table(market_data))
        layout["positions"].update(make_positions_panel(raw_positions))
        layout["log"].update(make_log_panel(log_lines))
        layout["risk"].update(make_risk_panel(journal, balance))

    # Initial render
    await refresh()

    with Live(layout, console=console, refresh_per_second=1, screen=True) as live:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            try:
                await refresh()
            except Exception as e:
                console.print(f"[red]Dashboard refresh error: {e}[/]")

    await ex.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_dashboard())
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard closed.[/]")
