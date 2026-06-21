"""Logica dei segnali: confluenza EMA su piu' time frame."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .bybit_client import BybitClient
from .indicators import consecutive_closes_beyond_ema, ema


@dataclass
class TimeframeResult:
    timeframe: int
    consecutive: int
    last_close: float
    last_ema: float


@dataclass
class Signal:
    symbol: str
    side: str  # "long" oppure "short"
    price: float
    per_timeframe: List[TimeframeResult]


def _closed_candles(candles: List[dict], interval_minutes: int) -> List[dict]:
    """Rimuove l'ultima candela se e' ancora in formazione."""
    if not candles:
        return candles
    last = candles[-1]
    interval_ms = interval_minutes * 60 * 1000
    now_ms = int(time.time() * 1000)
    if last["start"] + interval_ms > now_ms:
        return candles[:-1]
    return candles


def _side_label(direction: str) -> List[str]:
    if direction == "long":
        return ["above"]
    if direction == "short":
        return ["below"]
    if direction == "both":
        return ["above", "below"]
    raise ValueError("direction deve essere long, short o both")


class StrategyEngine:
    def __init__(
        self,
        client: BybitClient,
        timeframes: List[int],
        ema_period: int,
        consecutive_closes: int,
        direction: str,
    ):
        self.client = client
        self.timeframes = timeframes
        self.ema_period = ema_period
        self.consecutive_closes = consecutive_closes
        self.direction = direction

    def _evaluate_side(self, symbol: str, side: str) -> Optional[Signal]:
        """Valuta una direzione (above/below) su tutti i time frame.

        Restituisce un Signal solo se la condizione e' soddisfatta su OGNI
        time frame, altrimenti None.
        """
        results: List[TimeframeResult] = []
        last_price = None

        for tf in self.timeframes:
            candles = self.client.get_klines(symbol, tf, limit=200)
            candles = _closed_candles(candles, tf)
            if len(candles) < self.ema_period + self.consecutive_closes:
                return None

            closes = [c["close"] for c in candles]
            ema_series = ema(closes, self.ema_period)
            count = consecutive_closes_beyond_ema(closes, ema_series, side)

            if count < self.consecutive_closes:
                return None

            last_price = closes[-1]
            results.append(
                TimeframeResult(
                    timeframe=tf,
                    consecutive=count,
                    last_close=closes[-1],
                    last_ema=ema_series[-1],
                )
            )

        signal_side = "long" if side == "above" else "short"
        return Signal(
            symbol=symbol,
            side=signal_side,
            price=last_price,
            per_timeframe=results,
        )

    def evaluate_symbol(self, symbol: str) -> List[Signal]:
        signals: List[Signal] = []
        for side in _side_label(self.direction):
            sig = self._evaluate_side(symbol, side)
            if sig is not None:
                signals.append(sig)
        return signals
