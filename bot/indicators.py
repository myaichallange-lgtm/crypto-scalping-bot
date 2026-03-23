"""
indicators.py - Technical analysis engine
Multi-confluence scalping strategy — tuned for active signal generation.

Signal hierarchy:
  1. Primary:   EMA cross + RSI + volume + VWAP
  2. Secondary: RSI extreme bounce off BB band (oversold/overbought)
  3. Tertiary:  EMA momentum continuation (no cross needed, trend strong)

Each signal class has progressively relaxed conditions but the same
ATR-based SL/TP and risk management applies to all.
"""

import pandas as pd
import numpy as np
import ta
import ta.trend
import ta.momentum
import ta.volatility
import ta.volume

from .config import Config
from .logger import get_logger

log = get_logger("indicators", Config.LOG_LEVEL)


def ohlcv_to_df(ohlcv: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(
        ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 220:
        log.debug(f"compute_indicators: only {len(df)} bars, need ≥60 — returning empty")
        return df.iloc[0:0]

    c = Config

    # ── Trend EMAs ───────────────────────────────────────────────────────────────
    df["ema_fast"]  = ta.trend.EMAIndicator(df["close"], window=c.EMA_FAST).ema_indicator()
    df["ema_slow"]  = ta.trend.EMAIndicator(df["close"], window=c.EMA_SLOW).ema_indicator()
    df["ema_trend"] = ta.trend.EMAIndicator(df["close"], window=c.EMA_TREND).ema_indicator()

    # ── Additional EMAs for momentum confirmation ────────────────────────────────
    df["ema_200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()

    # ── RSI ─────────────────────────────────────────────────────────────────────
    df["rsi"]    = ta.momentum.RSIIndicator(df["close"], window=c.RSI_PERIOD).rsi()
    df["rsi_14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()  # slower RSI for context

    # ── Stochastic RSI (great for oversold/overbought entries) ───────────────────
    stoch = ta.momentum.StochasticOscillator(
        df["high"], df["low"], df["close"], window=14, smooth_window=3
    )
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    # ── MACD (momentum direction) ────────────────────────────────────────────────
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"]        = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"]   = macd.macd_diff()
    df["macd_bullish"] = df["macd_hist"] > 0

    # ── Bollinger Bands ──────────────────────────────────────────────────────────
    bb = ta.volatility.BollingerBands(df["close"], window=c.BB_PERIOD, window_dev=c.BB_STD)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"]   = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    df["bb_pct"]   = bb.bollinger_pband()   # 0=at lower, 1=at upper

    # ── ATR ──────────────────────────────────────────────────────────────────────
    df["atr"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=c.ATR_PERIOD
    ).average_true_range()

    # ── VWAP ─────────────────────────────────────────────────────────────────────
    df["vwap"] = ta.volume.VolumeWeightedAveragePrice(
        df["high"], df["low"], df["close"], df["volume"], window=14
    ).volume_weighted_average_price()

    # ── Volume ───────────────────────────────────────────────────────────────────
    df["vol_ma"]    = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > (df["vol_ma"] * c.VOLUME_THRESHOLD)
    df["vol_ok"]    = df["volume"] > (df["vol_ma"] * 0.8)   # relaxed volume — just above average

    # ── EMA crossover signals ────────────────────────────────────────────────────
    df["ema_bullish"]    = df["ema_fast"] > df["ema_slow"]
    df["ema_cross_up"]   = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))

    # ── RSI direction (rising vs falling) ────────────────────────────────────────
    df["rsi_rising"]  = df["rsi"] > df["rsi"].shift(2)
    df["rsi_falling"] = df["rsi"] < df["rsi"].shift(2)

    # ── Candle body direction ────────────────────────────────────────────────────
    df["bullish_candle"] = df["close"] > df["open"]
    df["bearish_candle"] = df["close"] < df["open"]

    df.dropna(inplace=True)
    return df


def compute_trend_bias(df_5m: pd.DataFrame) -> str:
    """Bull / bear / neutral based on 5m 50-EMA vs price."""
    if df_5m.empty or "ema_trend" not in df_5m.columns:
        return "neutral"
    last  = df_5m.iloc[-1]
    price = last["close"]
    ema   = last["ema_trend"]
    if price > ema * 1.0002:
        return "bull"
    elif price < ema * 0.9998:
        return "bear"
    return "neutral"


class SignalResult:
    __slots__ = ("signal", "entry_price", "stop_loss", "take_profit", "atr", "reason", "grade")

    def __init__(self, signal="none", entry_price=0.0, stop_loss=0.0,
                 take_profit=0.0, atr=0.0, reason="", grade=""):
        self.signal      = signal
        self.entry_price = entry_price
        self.stop_loss   = stop_loss
        self.take_profit = take_profit
        self.atr         = atr
        self.reason      = reason
        self.grade       = grade   # A/B/C — signal quality

    def __repr__(self):
        if self.signal == "none":
            return f"SignalResult(none | {self.reason})"
        return (
            f"SignalResult([{self.grade}] {self.signal.upper()} | "
            f"entry={self.entry_price:.4f} SL={self.stop_loss:.4f} "
            f"TP={self.take_profit:.4f} | {self.reason})"
        )


def generate_signal(df_1m: pd.DataFrame, df_5m: pd.DataFrame) -> SignalResult:
    """
    Three-tier signal system:

    Grade A — Full confluence (original strict conditions)
      EMA cross + RSI range + vol spike + VWAP side + trend aligned

    Grade B — RSI extreme bounce (high-probability reversal)
      RSI(7) < 20 oversold + price at/below BB lower + rising RSI/candle
      OR RSI(7) > 80 overbought + price at/above BB upper + falling

    Grade C — Momentum continuation
      Strong EMA alignment + MACD histogram positive + RSI in momentum zone
      + above-average volume (not spike required) + trend aligned

    All grades use identical ATR-based SL/TP and risk management.
    """
    c = Config

    if len(df_1m) < 5:
        return SignalResult(reason="insufficient data")

    trend = compute_trend_bias(df_5m)
    last  = df_1m.iloc[-1]
    prev  = df_1m.iloc[-2]
    prev2 = df_1m.iloc[-3]

    price          = float(last["close"])
    rsi            = float(last["rsi"])
    rsi_14         = float(last["rsi_14"])
    stoch_k        = float(last["stoch_k"])
    stoch_d        = float(last["stoch_d"])
    atr            = float(last["atr"])
    vol_spike      = bool(last["vol_spike"])
    vol_ok         = bool(last["vol_ok"])
    vwap           = float(last["vwap"])
    bb_lower       = float(last["bb_lower"])
    bb_upper       = float(last["bb_upper"])
    bb_mid         = float(last["bb_mid"])
    bb_pct         = float(last["bb_pct"])
    macd_bullish   = bool(last["macd_bullish"])
    ema_bull       = bool(last["ema_bullish"])
    rsi_rising     = bool(last["rsi_rising"])
    rsi_falling    = bool(last["rsi_falling"])
    bullish_candle = bool(last["bullish_candle"])
    bearish_candle = bool(last["bearish_candle"])
    macd_hist      = float(last["macd_hist"])

    # Crossover in last 3 bars
    cross_up   = bool(df_1m["ema_cross_up"].iloc[-3:].any())
    cross_down = bool(df_1m["ema_cross_down"].iloc[-3:].any())

    sl_dist = atr * c.ATR_SL_MULT
    tp_dist = atr * c.ATR_TP_MULT

    def long_result(reason, grade):
        entry = price
        return SignalResult("long", entry, round(entry - sl_dist, 6),
                            round(entry + tp_dist, 6), atr, reason, grade)

    def short_result(reason, grade):
        entry = price
        return SignalResult("short", entry, round(entry + sl_dist, 6),
                            round(entry - tp_dist, 6), atr, reason, grade)

    # ─────────────────────────────────────────────────────────────────────────────
    # GRADE A — Full confluence
    # ─────────────────────────────────────────────────────────────────────────────

    if (trend == "bull" and cross_up and 35 < rsi < 68
            and vol_spike and price > vwap * 0.999):
        return long_result(
            f"[A] EMA cross↑ | RSI={rsi:.1f} | trend=bull | vol✓ | VWAP✓", "A"
        )

    if (trend == "bear" and cross_down and 32 < rsi < 65
            and vol_spike and price < vwap * 1.001):
        return short_result(
            f"[A] EMA cross↓ | RSI={rsi:.1f} | trend=bear | vol✓ | VWAP✓", "A"
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # GRADE B — RSI extreme reversal (works in any trend, highest edge at extremes)
    # ─────────────────────────────────────────────────────────────────────────────

    # B-LONG: deeply oversold bounce
    # Trend filter removed for extreme RSI — oversold IS the signal
    if (rsi < 25                            # very oversold RSI(7)
            and rsi_14 < 40                 # slow RSI confirms weakness
            and stoch_k < 30               # stochastic also oversold
            and price <= bb_lower * 1.003  # at/near BB lower band
            and (rsi_rising or bullish_candle)  # ANY sign of reversal
            and trend != "bear"):          # don't fight strong downtrend
        return long_result(
            f"[B] RSI extreme bounce | RSI={rsi:.1f} | Stoch={stoch_k:.1f} | BB lower✓", "B"
        )

    # B-SHORT: deeply overbought rejection
    if (rsi > 75                            # very overbought RSI(7)
            and rsi_14 > 60                 # slow RSI confirms
            and stoch_k > 70               # stochastic also overbought
            and price >= bb_upper * 0.997  # at/near BB upper band
            and (rsi_falling or bearish_candle)
            and trend != "bull"):
        return short_result(
            f"[B] RSI extreme rejection | RSI={rsi:.1f} | Stoch={stoch_k:.1f} | BB upper✓", "B"
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # GRADE C — Momentum continuation (trend is strong, ride it)
    # ─────────────────────────────────────────────────────────────────────────────

    # C-LONG: trending up strongly, pull back to EMA, MACD positive
    if (trend == "bull"
            and ema_bull                   # EMA9 > EMA21
            and macd_bullish               # MACD histogram positive
            and macd_hist > 0              # and growing
            and 40 < rsi < 65             # momentum zone — not extreme
            and price >= last["ema_fast"] * 0.999  # price near or above EMA9 (not far)
            and price <= bb_mid * 1.003    # not extended above midline
            and vol_ok
            and not cross_down):           # no bearish cross recently
        return long_result(
            f"[C] Momentum long | RSI={rsi:.1f} | MACD↑ | EMA aligned | trend=bull", "C"
        )

    # C-SHORT: trending down strongly
    if (trend == "bear"
            and not ema_bull
            and not macd_bullish
            and macd_hist < 0
            and 35 < rsi < 60
            and price <= last["ema_fast"] * 1.001
            and price >= bb_mid * 0.997
            and vol_ok
            and not cross_up):
        return short_result(
            f"[C] Momentum short | RSI={rsi:.1f} | MACD↓ | EMA aligned | trend=bear", "C"
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # No signal
    # ─────────────────────────────────────────────────────────────────────────────
    reasons = []
    if trend == "neutral":
        reasons.append("no trend")
    if not vol_ok:
        reasons.append("low vol")
    if rsi >= 68 or rsi <= 32:
        reasons.append(f"RSI extreme ({rsi:.0f})")
    if not (cross_up or cross_down or ema_bull or not ema_bull):
        reasons.append("no momentum")
    if not reasons:
        reasons.append("no confluence")

    return SignalResult(reason=" | ".join(reasons))
