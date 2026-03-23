"""
risk.py - Risk management engine
Handles position sizing, daily drawdown guard, and trade journal.
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from .config import Config
from .logger import get_logger

log = get_logger("risk", Config.LOG_LEVEL)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)


class RiskManager:
    """
    Stateful risk manager that persists daily P&L and enforces
    hard limits before any trade is allowed.
    """

    def __init__(self, initial_balance: float = Config.INITIAL_BALANCE):
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance
        self.today = date.today().isoformat()
        self.journal_path = DATA_DIR / f"journal_{self.today}.json"
        self.daily_pnl: float = 0.0
        self.trade_count: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self._load_journal()

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _load_journal(self):
        """Load today's journal if it exists."""
        if self.journal_path.exists():
            try:
                with open(self.journal_path) as f:
                    data = json.load(f)
                self.daily_pnl   = data.get("daily_pnl", 0.0)
                self.trade_count = data.get("trade_count", 0)
                self.wins        = data.get("wins", 0)
                self.losses      = data.get("losses", 0)
                log.info(
                    f"Journal loaded: PnL={self.daily_pnl:+.2f} USDT | "
                    f"Trades={self.trade_count} | W/L={self.wins}/{self.losses}"
                )
            except Exception as e:
                log.warning(f"Could not load journal: {e}")

    def _save_journal(self):
        with open(self.journal_path, "w") as f:
            json.dump(
                {
                    "date": self.today,
                    "daily_pnl": round(self.daily_pnl, 4),
                    "trade_count": self.trade_count,
                    "wins": self.wins,
                    "losses": self.losses,
                    "win_rate": self.win_rate,
                },
                f,
                indent=2,
            )

    # ── Core checks ──────────────────────────────────────────────────────────────

    def is_trading_allowed(self, current_balance: float) -> tuple[bool, str]:
        """
        Returns (allowed: bool, reason: str).
        Must pass before ANY new trade is opened.
        """
        # Daily drawdown guard
        if self.initial_balance > 0:
            daily_loss_pct = -self.daily_pnl / self.initial_balance
            if daily_loss_pct >= Config.DAILY_DRAWDOWN_LIMIT:
                return (
                    False,
                    f"Daily drawdown limit hit: {daily_loss_pct:.1%} >= {Config.DAILY_DRAWDOWN_LIMIT:.1%}",
                )

        # Peak drawdown guard (overall account)
        self.peak_balance = max(self.peak_balance, current_balance)
        if self.peak_balance > 0:
            peak_drawdown = (self.peak_balance - current_balance) / self.peak_balance
            if peak_drawdown >= 0.25:   # 25% from equity peak → halt
                return (
                    False,
                    f"Peak drawdown limit hit: {peak_drawdown:.1%} from equity peak",
                )

        return True, "ok"

    def calculate_position_size(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float,
        leverage: int = Config.LEVERAGE,
        symbol: str = "",
    ) -> float:
        """
        Fixed fractional sizing based on REAL account balance:
          risk_amount  = balance × MAX_RISK_PER_TRADE
          margin_used  = risk_amount  (this is how much USDT we put up)
          notional     = margin_used × leverage
          quantity     = notional / entry_price

        Stop-loss is used as a sanity check — if SL is wider than 5% of
        entry we reject the trade (prevents oversizing on bad ATR spikes).

        Returns quantity in base currency (e.g. BTC, ETH, SOL).
        """
        risk_amount = balance * Config.MAX_RISK_PER_TRADE   # e.g. $5000 × 2% = $100
        notional    = risk_amount * leverage                 # e.g. $100 × 10 = $1000 notional
        quantity    = notional / entry_price

        price_risk_pct = abs(entry_price - stop_loss) / entry_price
        if price_risk_pct == 0:
            log.warning("SL equals entry price, skipping")
            return 0.0
        if price_risk_pct > 0.05:
            log.warning(f"SL too wide: {price_risk_pct:.2%} from entry, skipping")
            return 0.0

        # ── Exchange-specific minimums (from Binance Futures testnet) ────────────
        # BTC: stepSize=0.001, minNotional=$100
        # ETH: stepSize=0.001, minNotional=$20
        # SOL: stepSize=0.01,  minNotional=$5
        MIN_NOTIONAL = {
            "BTC/USDT": 100.0,
            "ETH/USDT": 20.0,
            "SOL/USDT": 5.0,
        }
        STEP_SIZE = {
            "BTC/USDT": 0.001,
            "ETH/USDT": 0.001,
            "SOL/USDT": 0.01,
        }

        # Use passed symbol, fallback to price-range detection
        if symbol in MIN_NOTIONAL:
            sym = symbol
        elif entry_price > 10000:
            sym = "BTC/USDT"
        elif entry_price > 500:
            sym = "ETH/USDT"
        else:
            sym = "SOL/USDT"

        min_notional = MIN_NOTIONAL.get(sym, 5.0)
        step         = STEP_SIZE.get(sym, 0.001)

        # Hard notional cap: max $200 notional per trade (keeps risk sane on $5k testnet)
        MAX_NOTIONAL = 200.0
        if notional > MAX_NOTIONAL:
            quantity = MAX_NOTIONAL / entry_price
            log.info(f"Notional capped at ${MAX_NOTIONAL} → qty={quantity:.6f}")

        # Floor: ensure we meet min notional
        if quantity * entry_price < min_notional:
            quantity = min_notional / entry_price
            log.info(f"Quantity raised to meet min notional ${min_notional} → qty={quantity:.6f}")

        # Round to exchange step size
        quantity = round(int(quantity / step) * step, 6)

        if quantity <= 0:
            log.warning(f"Quantity rounded to zero for step={step}, skipping")
            return 0.0

        final_notional = quantity * entry_price
        log.info(
            f"Position size: qty={quantity} | notional=${final_notional:.2f} "
            f"| margin=${final_notional / leverage:.2f} | SL dist={price_risk_pct:.3%}"
        )
        return quantity

    # ── Trade lifecycle ───────────────────────────────────────────────────────────

    def record_trade(self, pnl: float, symbol: str, side: str):
        """Call this after every closed trade."""
        self.daily_pnl   += pnl
        self.trade_count += 1
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1

        outcome = "WIN ✅" if pnl >= 0 else "LOSS ❌"
        log.info(
            f"Trade closed: {outcome} | {symbol} {side.upper()} | PnL={pnl:+.4f} USDT | "
            f"Daily PnL={self.daily_pnl:+.4f} | W/L={self.wins}/{self.losses}"
        )
        self._save_journal()

    # ── Stats ─────────────────────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0

    def summary(self) -> str:
        return (
            f"Daily PnL: {self.daily_pnl:+.4f} USDT | "
            f"Trades: {self.trade_count} | "
            f"Win rate: {self.win_rate:.1%} ({self.wins}W / {self.losses}L)"
        )
