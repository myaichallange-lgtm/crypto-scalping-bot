"""
engine.py - Main trading loop
Orchestrates: data fetch → indicators → signal → risk check → order execution
"""

import asyncio
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich import box

from .config import Config
from .exchange import ExchangeClient
from .indicators import ohlcv_to_df, compute_indicators, generate_signal
from .position_manager import PositionManager
from .risk import RiskManager
from .logger import get_logger

log     = get_logger("engine", Config.LOG_LEVEL)
console = Console()


class TradingEngine:
    def __init__(self):
        self.exchange  = ExchangeClient()
        self.risk      = RiskManager()
        self.positions = PositionManager(self.exchange, self.risk)
        self._running  = False
        self._tick     = 0

    # ── Lifecycle ────────────────────────────────────────────────────────────────

    async def start(self):
        log.info("=" * 60)
        log.info("  🤖  Scalping Bot Starting")
        log.info(f"  Pairs:    {', '.join(Config.PAIRS)}")
        log.info(f"  Testnet:  {Config.TESTNET}")
        log.info(f"  Leverage: {Config.LEVERAGE}x")
        log.info(f"  Risk/trade: {Config.MAX_RISK_PER_TRADE:.0%}")
        log.info("=" * 60)

        await self.exchange.connect()
        await self._sync_positions_from_exchange()
        self._running = True

        try:
            await self._loop()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received — shutting down gracefully")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        self._running = False
        if self.positions.open_count:
            close = input("\nClose all positions on exit? [y/N]: ").strip().lower()
            if close == "y":
                await self.positions.close_all("shutdown")
        log.info(f"Final: {self.risk.summary()}")
        await self.exchange.close()
        log.info("Bot stopped.")

    # ── Startup sync ─────────────────────────────────────────────────────────────

    async def _sync_positions_from_exchange(self):
        """
        On startup: fetch any open positions from the exchange and close them.
        This prevents the bot from opening duplicate positions on top of
        leftover positions from a previous run.
        """
        try:
            raw = await self.exchange.fetch_open_positions()
            if not raw:
                log.info("Startup sync: no open positions on exchange — clean start")
                return
            log.warning(f"Startup sync: found {len(raw)} open position(s) from previous run — closing all")
            for p in raw:
                ccxt_sym = p.get('info', {}).get('symbol', '')
                symbol = None
                for base in ['BTC', 'ETH', 'SOL']:
                    if ccxt_sym.startswith(base):
                        symbol = f"{base}/USDT"
                        break
                if not symbol:
                    continue
                side  = p['side']
                size  = float(p['contracts'])
                close = 'sell' if side == 'long' else 'buy'
                try:
                    await self.exchange.cancel_all_orders(symbol)
                    await self.exchange.exchange.create_order(
                        symbol, 'market', close, size,
                        params={'reduceOnly': True}
                    )
                    log.info(f"Startup sync: closed {symbol} {side} x{size}")
                except Exception as e:
                    log.error(f"Startup sync: failed to close {symbol}: {e}")
        except Exception as e:
            log.error(f"Startup sync error: {e}")

    # ── Main loop ────────────────────────────────────────────────────────────────

    async def _loop(self):
        while self._running:
            self._tick += 1
            try:
                await self._tick_all_pairs()
            except Exception as e:
                log.error(f"Tick error: {e}", exc_info=True)

            if self._tick % 10 == 0:
                self._print_status()

            # Print mini-status every tick
            log.info(f"Tick #{self._tick} done — sleeping {Config.LOOP_SLEEP_SECONDS}s")
            await asyncio.sleep(Config.LOOP_SLEEP_SECONDS)

    async def _tick_all_pairs(self):
        """Run one evaluation cycle for all configured pairs concurrently."""
        balance = await self.exchange.get_usdt_balance()
        log.debug(f"Balance: {balance:.2f} USDT | Open positions: {self.positions.open_count}")

        # Risk check before doing anything
        allowed, reason = self.risk.is_trading_allowed(balance)
        if not allowed:
            log.warning(f"⛔ Trading halted: {reason}")
            # Still monitor positions (let SL/TP work)
            current_prices = await self._get_current_prices()
            await self.positions.monitor_positions(current_prices)
            return

        # Fetch data for all pairs concurrently
        tasks = [self._evaluate_pair(symbol, balance) for symbol in Config.PAIRS]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Monitor existing positions
        current_prices = await self._get_current_prices()
        await self.positions.monitor_positions(current_prices)

    async def _evaluate_pair(self, symbol: str, balance: float):
        """Fetch data, compute signals, and optionally enter a trade."""
        try:
            # Fetch candles for both timeframes concurrently
            # 500 bars needed for EMA200 + MACD warm-up
            ohlcv_1m, ohlcv_5m = await asyncio.gather(
                self.exchange.fetch_ohlcv(symbol, Config.ENTRY_TF, limit=500),
                self.exchange.fetch_ohlcv(symbol, Config.TREND_TF, limit=200),
            )

            df_1m = compute_indicators(ohlcv_to_df(ohlcv_1m))
            df_5m = compute_indicators(ohlcv_to_df(ohlcv_5m))

            signal = generate_signal(df_1m, df_5m)
            log.debug(f"{symbol}: {signal}")

            if signal.signal == "none":
                return

            if self.positions.has_position(symbol):
                log.debug(f"{symbol}: already in position, skipping signal")
                return

            # Position sizing
            quantity = self.risk.calculate_position_size(
                balance, signal.entry_price, signal.stop_loss, symbol=symbol
            )
            if quantity <= 0:
                log.warning(f"{symbol}: position size too small, skipping")
                return

            log.info(f"🔔 SIGNAL: {signal}")

            await self.positions.open_position(
                symbol=symbol,
                side=signal.signal,
                entry_price=signal.entry_price,
                quantity=quantity,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                atr=signal.atr,
                reason=signal.reason,
            )

        except Exception as e:
            log.error(f"Error evaluating {symbol}: {e}", exc_info=True)

    # ── Helpers ───────────────────────────────────────────────────────────────────

    async def _get_current_prices(self) -> dict[str, float]:
        prices = {}
        for symbol in Config.PAIRS:
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                prices[symbol] = float(ticker["last"])
            except Exception:
                pass
        return prices

    def _print_status(self):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        console.print(f"\n[bold cyan]─── Bot Status ─── {now}[/]")
        console.print(f"  {self.risk.summary()}")

        if self.positions.open_count:
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
            for col in ["Symbol", "Side", "Entry", "SL", "TP", "Qty"]:
                table.add_column(col)
            for row in self.positions.status_table():
                table.add_row(
                    row["Symbol"],
                    f"[green]{row['Side']}[/]" if row["Side"] == "LONG" else f"[red]{row['Side']}[/]",
                    str(row["Entry"]),
                    str(row["SL"]),
                    str(row["TP"]),
                    str(row["Qty"]),
                )
            console.print(table)
        else:
            console.print("  [dim]No open positions[/]")
        console.print()
