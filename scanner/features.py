"""Estrazione delle feature per simbolo.

Timeframe di lavoro per l'orizzonte intraday 1-8h:
  - 15m -> timeframe base (volume relativo, breakout, volatilita', entry)
  - 1h  -> contesto di trend e ATR per stop/target

Ogni feature e' Optional: un simbolo appena listato o poco liquido puo' non
averne alcune, e lo score deve poter comunque essere calcolato penalizzandolo.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence

from .bybit import Candle
from .indicators import (
    atr,
    atr_series,
    ema,
    median,
    pct_change,
    percentile_rank,
    rvol,
    vwap,
)

# Barre di 15m contenute nelle finestre temporali che ci interessano.
BARS_1H = 4
BARS_4H = 16
BARS_24H = 96

# Parametri del volume relativo: finestra da 1h confrontata con le 24h precedenti.
RVOL_WINDOW = BARS_1H
RVOL_LOOKBACK = 24


@dataclass
class Features:
    symbol: str
    price: float

    # --- liquidita' / costi
    turnover_24h: float
    spread_bp: Optional[float]          # spread bid-ask in basis point

    # --- volatilita'
    atr_pct_15m: Optional[float]        # ATR14 su 15m in % del prezzo
    atr_pct_1h: Optional[float]
    atr_abs_1h: Optional[float]         # ATR14 su 1h in valore assoluto (per stop/target)
    squeeze_pct: Optional[float]        # percentile dell'ATR corrente sulla sua storia (0 = compresso)

    # --- volume
    rvol_1h: Optional[float]            # volume ultima ora / mediana stesse finestre 24h

    # --- momentum
    ret_1h: Optional[float]
    ret_4h: Optional[float]
    ret_24h: Optional[float]
    rs_btc_4h: Optional[float]          # ret_4h - ret_4h di BTC (forza relativa)

    # --- struttura di prezzo
    range_pos_24h: Optional[float]      # 0 = minimo 24h, 1 = massimo 24h
    breakout_atr: Optional[float]       # distanza dal massimo/minimo recente, in ATR
    ema_align_15m: int                  # +1 rialzo, -1 ribasso, 0 misto
    ema_align_1h: int
    vwap_dist_atr: Optional[float]      # distanza dal VWAP 24h, in ATR (segno = lato)

    # --- derivati
    oi_change_1h: Optional[float]       # variazione % open interest sull'ultima ora
    funding_rate: Optional[float]

    def to_dict(self) -> Dict:
        return asdict(self)


def _ohlcv(candles: Sequence[Candle]):
    return (
        [c.high for c in candles],
        [c.low for c in candles],
        [c.close for c in candles],
        [c.volume for c in candles],
    )


def _ema_alignment(closes: Sequence[float], fast: int = 20, slow: int = 50) -> int:
    """+1 se prezzo > EMA veloce > EMA lenta, -1 nel caso simmetrico, 0 altrimenti."""
    if len(closes) < slow + 1:
        return 0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return 0
    price = closes[-1]
    if price > ema_fast > ema_slow:
        return 1
    if price < ema_fast < ema_slow:
        return -1
    return 0


def _breakout_in_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    price: float,
    atr_value: Optional[float],
    lookback: int = BARS_24H,
    exclude: int = 2,
) -> Optional[float]:
    """Quanto il prezzo sporge oltre il massimo (o sotto il minimo) recente, in ATR.

    Le ultime `exclude` barre sono escluse dal calcolo del massimo: altrimenti
    il massimo conterrebbe la spinta stessa che stiamo cercando di misurare e
    il breakout risulterebbe sempre pari a zero.
    Positivo = rottura al rialzo, negativo = rottura al ribasso, 0 = dentro il range.
    """
    if atr_value is None or atr_value <= 0:
        return None
    window_high = highs[-lookback:-exclude] if exclude else highs[-lookback:]
    window_low = lows[-lookback:-exclude] if exclude else lows[-lookback:]
    if len(window_high) < 10:
        return None

    top = max(window_high)
    bottom = min(window_low)
    if price > top:
        return (price - top) / atr_value
    if price < bottom:
        return (price - bottom) / atr_value
    return 0.0


def _range_position(highs: Sequence[float], lows: Sequence[float], price: float, lookback: int = BARS_24H) -> Optional[float]:
    window_high = highs[-lookback:]
    window_low = lows[-lookback:]
    if len(window_high) < 10:
        return None
    top = max(window_high)
    bottom = min(window_low)
    if top <= bottom:
        return None
    return (price - bottom) / (top - bottom)


def _oi_change(oi_series: Optional[List[dict]], points: int = 12) -> Optional[float]:
    """Variazione % dell'open interest sugli ultimi `points` campioni (5min -> 12 = 1h)."""
    if not oi_series or len(oi_series) < points + 1:
        return None
    past = oi_series[-(points + 1)]["openInterest"]
    now = oi_series[-1]["openInterest"]
    if past <= 0:
        return None
    return (now - past) / past


def compute_features(
    symbol: str,
    candles_15m: Sequence[Candle],
    candles_1h: Sequence[Candle],
    ticker: dict,
    btc_ret_4h: Optional[float] = None,
    oi_series: Optional[List[dict]] = None,
) -> Optional[Features]:
    """Calcola tutte le feature. None se non ci sono dati minimi utilizzabili."""
    if len(candles_15m) < BARS_24H // 2 or len(candles_1h) < 20:
        return None

    highs, lows, closes, volumes = _ohlcv(candles_15m)
    h1, l1, c1, _v1 = _ohlcv(candles_1h)
    price = closes[-1]
    if price <= 0:
        return None

    atr_15m = atr(highs, lows, closes, 14)
    atr_1h = atr(h1, l1, c1, 14)
    atr_pct_15m = (atr_15m / price) if atr_15m else None
    atr_pct_1h = (atr_1h / price) if atr_1h else None

    # Compressione: dove sta l'ATR di adesso rispetto alle sue ultime 100 letture.
    squeeze_pct = None
    if atr_15m is not None:
        history = [a for a in atr_series(highs, lows, closes, 14)[-100:] if a is not None]
        if len(history) >= 30:
            squeeze_pct = percentile_rank(history, atr_15m)

    vwap_value = vwap(highs[-BARS_24H:], lows[-BARS_24H:], closes[-BARS_24H:], volumes[-BARS_24H:])
    vwap_dist_atr = None
    if vwap_value and atr_15m:
        vwap_dist_atr = (price - vwap_value) / atr_15m

    ret_4h = pct_change(closes, BARS_4H)
    rs_btc_4h = None
    if ret_4h is not None and btc_ret_4h is not None:
        rs_btc_4h = ret_4h - btc_ret_4h

    spread_bp = None
    try:
        bid = float(ticker.get("bid1Price") or 0)
        ask = float(ticker.get("ask1Price") or 0)
        if bid > 0 and ask > 0:
            spread_bp = (ask - bid) / ((ask + bid) / 2) * 10_000
    except (TypeError, ValueError):
        spread_bp = None

    def _fnum(key: str) -> Optional[float]:
        try:
            raw = ticker.get(key)
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Features(
        symbol=symbol,
        price=price,
        turnover_24h=_fnum("turnover24h") or 0.0,
        spread_bp=spread_bp,
        atr_pct_15m=atr_pct_15m,
        atr_pct_1h=atr_pct_1h,
        atr_abs_1h=atr_1h,
        squeeze_pct=squeeze_pct,
        rvol_1h=rvol(volumes, RVOL_WINDOW, RVOL_LOOKBACK),
        ret_1h=pct_change(closes, BARS_1H),
        ret_4h=ret_4h,
        ret_24h=pct_change(closes, BARS_24H),
        rs_btc_4h=rs_btc_4h,
        range_pos_24h=_range_position(highs, lows, price),
        breakout_atr=_breakout_in_atr(highs, lows, price, atr_15m),
        ema_align_15m=_ema_alignment(closes),
        ema_align_1h=_ema_alignment(c1),
        vwap_dist_atr=vwap_dist_atr,
        oi_change_1h=_oi_change(oi_series),
        funding_rate=_fnum("fundingRate"),
    )
