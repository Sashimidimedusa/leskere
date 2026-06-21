"""Indicatori tecnici: EMA e conteggio chiusure consecutive."""

from __future__ import annotations

from typing import List


def ema(values: List[float], period: int) -> List[float]:
    """Restituisce la serie EMA allineata a `values`.

    Le prime `period - 1` posizioni sono None perche' la EMA non e' ancora
    definita; viene poi inizializzata con la SMA dei primi `period` valori.
    """
    if period <= 0:
        raise ValueError("period deve essere > 0")
    if len(values) < period:
        return [None] * len(values)

    out: List[float] = [None] * (period - 1)
    sma = sum(values[:period]) / period
    out.append(sma)

    multiplier = 2 / (period + 1)
    prev = sma
    for price in values[period:]:
        prev = (price - prev) * multiplier + prev
        out.append(prev)
    return out


def consecutive_closes_beyond_ema(
    closes: List[float],
    ema_series: List[float],
    side: str,
) -> int:
    """Conta quante candele consecutive, partendo dall'ultima, hanno chiuso
    oltre la EMA nella direzione richiesta.

    side = "above" -> close > ema ; side = "below" -> close < ema.
    Si assume che `closes` contenga solo candele CHIUSE (no candela in corso).
    """
    if side not in ("above", "below"):
        raise ValueError("side deve essere 'above' o 'below'")

    count = 0
    for close, ema_val in zip(reversed(closes), reversed(ema_series)):
        if ema_val is None:
            break
        beyond = close > ema_val if side == "above" else close < ema_val
        if beyond:
            count += 1
        else:
            break
    return count
