"""Verifica automatica degli esiti: cosa e' successo DOPO ogni segnale.

Per ogni segnale maturo scarica le candele successive e registra:
  - rendimento nel verso del segnale all'orizzonte
  - MFE / MAE espressi in R (multipli del rischio), non in percentuale: cosi'
    simboli con volatilita' diversa sono confrontabili
  - se sono arrivati prima stop o target

Regola conservativa: se una barra contiene sia lo stop sia il target non
possiamo sapere quale sia stato toccato per primo, quindi contiamo lo stop.
Meglio sottostimare la strategia che innamorarsene per un artefatto.
"""

from __future__ import annotations

import json
from typing import List, Optional

from .bybit import BybitClient, BybitError, Candle
from .store import HORIZONS, Store

EVAL_INTERVAL_MIN = 5   # granularita' delle candele usate per la verifica


def _slice_candles(candles: List[Candle], start_ms: int, end_ms: int) -> List[Candle]:
    return [c for c in candles if start_ms <= c.start <= end_ms]


def evaluate_signal(
    candles: List[Candle],
    direction: str,
    entry: float,
    stop: Optional[float],
    target: Optional[float],
) -> dict:
    """Calcola ret / MFE / MAE / hit sulle candele successive al segnale."""
    if not candles or entry <= 0:
        return {"price_at": None, "ret": None, "mfe": None, "mae": None, "hit": "none"}

    sign = 1.0 if direction == "long" else -1.0
    risk = abs(entry - stop) if stop else None

    best = entry
    worst = entry
    hit = "none"
    for c in candles:
        favorable = c.high if direction == "long" else c.low
        adverse = c.low if direction == "long" else c.high
        if sign * (favorable - best) > 0:
            best = favorable
        if sign * (adverse - worst) < 0:
            worst = adverse

        if hit == "none" and stop is not None and target is not None:
            stop_touched = c.low <= stop if direction == "long" else c.high >= stop
            target_touched = c.high >= target if direction == "long" else c.low <= target
            if stop_touched:
                hit = "stop"        # conservativo anche se nella stessa barra c'e' il target
            elif target_touched:
                hit = "target"

    price_at = candles[-1].close
    ret = sign * (price_at - entry) / entry
    mfe = (sign * (best - entry) / risk) if risk else None
    mae = (sign * (worst - entry) / risk) if risk else None
    return {"price_at": price_at, "ret": ret, "mfe": mfe, "mae": mae, "hit": hit}


class OutcomeTracker:
    def __init__(self, client: BybitClient, store: Store):
        self.client = client
        self.store = store

    def run_once(self, horizons=HORIZONS) -> int:
        """Valuta tutti i segnali maturi. Restituisce quanti ne ha registrati."""
        rows = self.store.pending_evaluations(horizons)
        done = 0
        for row in rows:
            horizon_min = row["horizon_min"]
            start_ms = row["ts"]
            end_ms = start_ms + horizon_min * 60_000

            # Le candele vanno ancorate alla FINE dell'orizzonte, non a "adesso":
            # un segnale puo' essere verificato molto dopo la sua scadenza (scanner
            # riavviato, orizzonte a 8h, arretrati da smaltire) e chiedere le
            # ultime N barre restituirebbe una finestra che non contiene il segnale.
            bars = int(horizon_min / EVAL_INTERVAL_MIN) + 10
            try:
                candles = self.client.get_klines(
                    row["symbol"],
                    EVAL_INTERVAL_MIN,
                    limit=min(bars, 1000),
                    use_cache=False,
                    end_ms=end_ms + EVAL_INTERVAL_MIN * 60_000,
                )
            except BybitError:
                continue

            window = _slice_candles(candles, start_ms, end_ms)
            if not window:
                continue

            entry = row["entry"] if row["entry"] is not None else row["price"]
            outcome = evaluate_signal(
                window, row["direction"], entry, row["stop"], row["target"]
            )
            self.store.record_outcome(
                signal_id=row["id"],
                horizon_min=horizon_min,
                price_at=outcome["price_at"],
                ret=outcome["ret"],
                mfe=outcome["mfe"],
                mae=outcome["mae"],
                hit=outcome["hit"],
            )
            done += 1
        return done
