"""Indicatori puri, senza dipendenze esterne e senza I/O.

Tutte le funzioni lavorano su liste di float in ordine cronologico
(vecchio -> nuovo) e restituiscono None quando non ci sono abbastanza dati,
invece di sollevare eccezioni: durante uno scan un simbolo appena listato con
poche candele non deve far cadere l'intero ciclo.
"""

from __future__ import annotations

from typing import List, Optional, Sequence


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Serie EMA allineata a `values`; inizializzata con la SMA dei primi `period`."""
    if period <= 0:
        raise ValueError("period deve essere > 0")
    if len(values) < period:
        return [None] * len(values)

    out: List[Optional[float]] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)

    k = 2 / (period + 1)
    prev = seed
    for price in values[period:]:
        prev = (price - prev) * k + prev
        out.append(prev)
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    return series[-1] if series else None


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    """True Range barra per barra; la prima barra usa solo high-low."""
    out: List[float] = []
    for i in range(len(closes)):
        if i == 0:
            out.append(highs[i] - lows[i])
            continue
        prev_close = closes[i - 1]
        out.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Optional[float]:
    """ATR con smoothing di Wilder (lo standard, non la media semplice)."""
    if len(closes) < period + 1:
        return None
    tr = true_ranges(highs, lows, closes)
    value = sum(tr[1:period + 1]) / period
    for t in tr[period + 1:]:
        value = (value * (period - 1) + t) / period
    return value


def atr_series(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> List[Optional[float]]:
    """Serie ATR allineata alle barre: serve per misurare la compressione
    (ATR corrente vs il suo comportamento tipico recente)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    tr = true_ranges(highs, lows, closes)
    value = sum(tr[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, n):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out


def median(values: Sequence[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def percentile_rank(values: Sequence[float], target: float) -> Optional[float]:
    """Frazione di `values` <= target, in 0..1. Utile per normalizzare una
    misura rispetto alla sua storia recente senza ipotesi di distribuzione."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v <= target) / len(vals)


def rvol(volumes: Sequence[float], window: int, lookback_windows: int) -> Optional[float]:
    """Volume relativo: somma delle ultime `window` barre diviso la MEDIANA
    delle stesse somme sulle `lookback_windows` finestre precedenti.

    Mediana e non media: una singola pump storica gonfierebbe la media e
    nasconderebbe per giorni ogni nuovo scatto di volume.
    """
    if window <= 0 or lookback_windows <= 0:
        return None
    needed = window * (lookback_windows + 1)
    if len(volumes) < needed:
        return None

    current = sum(volumes[-window:])
    history: List[float] = []
    for i in range(1, lookback_windows + 1):
        end = len(volumes) - i * window
        history.append(sum(volumes[end - window:end]))

    baseline = median(history)
    if not baseline or baseline <= 0:
        return None
    return current / baseline


def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], volumes: Sequence[float]) -> Optional[float]:
    """VWAP sulle barre fornite (tipicamente le ultime 24h), prezzo tipico HLC/3."""
    total_v = 0.0
    total_pv = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        if v <= 0:
            continue
        typical = (h + l + c) / 3
        total_pv += typical * v
        total_v += v
    if total_v <= 0:
        return None
    return total_pv / total_v


def pct_change(values: Sequence[float], bars: int) -> Optional[float]:
    """Variazione percentuale sulle ultime `bars` barre (0.05 = +5%)."""
    if bars <= 0 or len(values) < bars + 1:
        return None
    past = values[-(bars + 1)]
    if past == 0:
        return None
    return (values[-1] - past) / past


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def scale(value: Optional[float], low: float, high: float) -> float:
    """Mappa `value` da [low, high] a [0, 1] con saturazione ai bordi.

    None -> 0.0 (dato mancante non deve regalare punteggio).
    """
    if value is None:
        return 0.0
    if high == low:
        return 0.0
    return clamp((value - low) / (high - low))
