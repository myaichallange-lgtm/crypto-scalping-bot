"""
position_manager.py - Tracks open positions locally and syncs with exchange.
Manages SL/TP order lifecycle and trailing stop logic.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .exchange import ExchangeClient
from .risk import RiskManager
from .logger import get_logger

log = get_logger("positions", Config.LOG_LEVEL)


@dataclass
class Position:
    symbol: str
    side: str           # 'long' | 'short'
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    atr: float
    reason: str
    order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    peak_price: float = 0.0
    trough_price: float = 0.0
    opened_at: float = 0.0      # unix timestamp — for race condition guard

    def __post_init__(self):
        self.peak_price   = self.entry_price
        self.trough_price = self.entry_price
        self.opened_at    = time.time()

    @property
    def close_side(self) -> str:
        return "sell" if self.side == "long" else "buy"

    def update_extremes(self, current_price: float):
        if self.side == "long":
            self.peak_price = max(self.peak_price, current_price)
        else:
            self.trough_price = min(self.trough_price, current_price)

    def trailing_stop(self, current_price: float) -> Optional[float]:
        """
        Activates a trailing stop once price moves 1 ATR in our favour.
        Trails at 1× ATR behind the peak (long) or trough (short).
        Returns new SL price if it should be updated, else None.
        """
        trail_dist = self.atr * 1.0   # trail at 1× ATR

        if self.side == "long":
            if self.peak_price > self.entry_price + self.atr:
                new_sl = round(self.peak_price - trail_dist, 6)
                if new_sl > self.stop_loss:
                    return new_sl
        else:
            if self.trough_price < self.entry_price - self.atr:
                new_sl = round(self.trough_price + trail_dist, 6)
                if new_sl < self.stop_loss:
                    return new_sl
        return None


class PositionManager:
    """
    Manages open positions for all traded pairs.
    One position per symbol maximum.
    """

    def __init__(self, exchange: ExchangeClient, risk: RiskManager):
        self.exchange = exchange
        self.risk     = risk
        self.positions: dict[str, Position] = {}   # symbol → Position

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    # ── Open ─────────────────────────────────────────────────────────────────────

    async def open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        atr: float,
        reason: str,
    ) -> bool:
        if self.has_position(symbol):
            log.warning(f"Already in position for {symbol}, skipping")
            return False

        if self.open_count >= Config.MAX_OPEN_POSITIONS:
            log.warning(
                f"Max open positions ({Config.MAX_OPEN_POSITIONS}) reached, skipping {symbol}"
            )
            return False

        try:
            # Set leverage
            await self.exchange.set_leverage(symbol, Config.LEVERAGE)

            # Cancel any orphaned SL/TP orders — wait for propagation
            await self.exchange.cancel_all_orders(symbol)
            await asyncio.sleep(1.0)   # give exchange 1s to process cancels

            # Market entry
            order = await self.exchange.place_market_order(symbol,
                "buy" if side == "long" else "sell", quantity)

            # Place SL and TP orders
            sl_order = await self.exchange.place_stop_order(
                symbol, "sell" if side == "long" else "buy", quantity, stop_loss
            )
            tp_order = await self.exchange.place_take_profit_order(
                symbol, "sell" if side == "long" else "buy", quantity, take_profit
            )

            pos = Position(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
                atr=atr,
                reason=reason,
                order_id=order.get("id"),
                sl_order_id=sl_order.get("id"),
                tp_order_id=tp_order.get("id"),
            )
            self.positions[symbol] = pos

            log.info(
                f"✅ POSITION OPENED | {symbol} {side.upper()} | "
                f"qty={quantity} | entry={entry_price:.4f} | "
                f"SL={stop_loss:.4f} | TP={take_profit:.4f} | {reason}"
            )
            return True

        except Exception as e:
            log.error(f"Failed to open position for {symbol}: {e}")
            return False

    # ── Monitor (called every loop tick) ─────────────────────────────────────────

    async def monitor_positions(self, current_prices: dict[str, float]):
        """
        For each open position:
        1. Update price extremes
        2. Check if trailing stop should move
        3. Detect if SL/TP was hit (exchange-side) and record PnL
        """
        closed = []
        exchange_positions = {
            p["symbol"]: p for p in await self.exchange.fetch_open_positions()
        }

        for symbol, pos in self.positions.items():
            price = current_prices.get(symbol)
            if price is None:
                continue

            pos.update_extremes(price)

            # Check if position was closed on exchange (SL or TP hit)
            # Guard: ignore for first 30s after opening (exchange propagation lag)
            ex_symbol = symbol.replace("/", "")
            age_seconds = time.time() - pos.opened_at
            if ex_symbol not in exchange_positions:
                if age_seconds < 30:
                    log.debug(f"{symbol}: position not yet visible on exchange ({age_seconds:.0f}s old), waiting...")
                    continue
                # Position gone — calculate approximate PnL
                pnl = self._estimate_pnl(pos, price)
                self.risk.record_trade(pnl, symbol, pos.side)
                closed.append(symbol)
                log.info(f"Position closed externally: {symbol} (SL/TP hit) after {age_seconds:.0f}s")
                continue

            # Trailing stop update
            new_sl = pos.trailing_stop(price)
            if new_sl and new_sl != pos.stop_loss:
                log.info(
                    f"Trailing SL updated: {symbol} {pos.stop_loss:.4f} → {new_sl:.4f}"
                )
                pos.stop_loss = new_sl
                try:
                    await self.exchange.cancel_all_orders(symbol)
                    await self.exchange.place_stop_order(
                        symbol, pos.close_side, pos.quantity, new_sl
                    )
                    await self.exchange.place_take_profit_order(
                        symbol, pos.close_side, pos.quantity, pos.take_profit
                    )
                except Exception as e:
                    log.error(f"Failed to update trailing SL for {symbol}: {e}")

        for sym in closed:
            del self.positions[sym]

    # ── Emergency close ───────────────────────────────────────────────────────────

    async def close_all(self, reason: str = "emergency"):
        log.warning(f"Closing all positions: {reason}")
        for symbol, pos in list(self.positions.items()):
            try:
                await self.exchange.cancel_all_orders(symbol)
                await self.exchange.close_position(symbol, pos.side, pos.quantity)
                del self.positions[symbol]
            except Exception as e:
                log.error(f"Failed to close {symbol}: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────────

    def _estimate_pnl(self, pos: Position, exit_price: float) -> float:
        """Rough PnL estimate for journal (exchange fees not deducted here)."""
        if pos.side == "long":
            return (exit_price - pos.entry_price) * pos.quantity * Config.LEVERAGE
        else:
            return (pos.entry_price - exit_price) * pos.quantity * Config.LEVERAGE

    def status_table(self) -> list[dict]:
        """Returns a list of dicts for pretty-printing."""
        rows = []
        for sym, pos in self.positions.items():
            rows.append(
                {
                    "Symbol": sym,
                    "Side": pos.side.upper(),
                    "Entry": pos.entry_price,
                    "SL": pos.stop_loss,
                    "TP": pos.take_profit,
                    "Qty": pos.quantity,
                }
            )
        return rows
