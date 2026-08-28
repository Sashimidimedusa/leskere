"""Validazione storica dello score.

Domanda a cui risponde: uno score alto ha davvero portato, in passato, un
rendimento migliore di uno score basso? Se la risposta e' no, lo scanner sta
solo ordinando rumore e i pesi vanno rivisti PRIMA di metterci soldi.

    python3 -m scanner.backtest --days 30 --symbols BTCUSDT ETHUSDT SOLUSDT
    python3 -m scanner.backtest --days 30 --top 40      # i 40 piu' liquidi
    python3 -m scanner.backtest --days 30 --offline     # riusa la cache su disco

Limiti onesti di questo backtest (vanno tenuti a mente leggendo i numeri):
  - open interest, funding e spread storici non sono ricostruiti: le relative
    componenti/penalita' sono neutralizzate e i pesi rinormalizzati, quindi lo
    score qui NON e' identico a quello live;
  - i rendimenti forward sono lordi: niente commissioni ne' slippage;
  - l'universo e' scelto sulla liquidita' di OGGI, quindi c'e' un po' di
    survivorship bias sui simboli che nel frattempo sono morti.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from typing import Dict, List, Optional, Sequence

from .bybit import BybitClient, BybitError, Candle
from .features import BARS_1H, BARS_4H, compute_features
from .score import DEFAULT_WEIGHTS, build_plan, infer_direction, score_symbol

HISTORY_DIR = "scanner/data/history"
MS_15M = 15 * 60_000

# Storico: senza OI/funding/spread quelle componenti sarebbero sempre 0 e
# schiaccerebbero tutti gli score. Le togliamo e rinormalizziamo il resto.
BACKTEST_WEIGHTS = {k: v for k, v in DEFAULT_WEIGHTS.items() if k != "open_interest"}


# --------------------------------------------------------------- storico ---
def _cache_path(symbol: str, interval: int) -> str:
    return os.path.join(HISTORY_DIR, f"{symbol}_{interval}m.json")


def load_cached(symbol: str, interval: int) -> Optional[List[Candle]]:
    path = _cache_path(symbol, interval)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        rows = json.load(fh)
    return [Candle(**r) for r in rows]


def save_cached(symbol: str, interval: int, candles: Sequence[Candle]) -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(_cache_path(symbol, interval), "w", encoding="utf-8") as fh:
        json.dump([c.__dict__ for c in candles], fh)


def fetch_history(client: BybitClient, symbol: str, interval: int, days: int) -> List[Candle]:
    """Scarica `days` giorni di candele paginando all'indietro (limite 1000/chiamata)."""
    target_bars = int(days * 24 * 60 / interval) + 200      # margine per gli indicatori
    out: List[Candle] = []
    end_ms: Optional[int] = None

    while len(out) < target_bars:
        batch = client.get_klines(symbol, interval, limit=1000, use_cache=False, end_ms=end_ms)
        if not batch:
            break
        out = batch + out
        end_ms = batch[0].start - 1
        if len(batch) < 900:                                 # storico esaurito
            break
    # deduplica per sicurezza (le pagine possono sovrapporsi di una barra)
    seen = {}
    for c in out:
        seen[c.start] = c
    return [seen[k] for k in sorted(seen)]


def get_history(client: Optional[BybitClient], symbol: str, interval: int, days: int, offline: bool) -> List[Candle]:
    if offline:
        cached = load_cached(symbol, interval)
        if cached is None:
            raise BybitError(f"nessuna cache per {symbol} {interval}m: lancia una volta senza --offline")
        return cached
    assert client is not None
    candles = fetch_history(client, symbol, interval, days)
    save_cached(symbol, interval, candles)
    return candles


# ---------------------------------------------------------------- replay ---
def _slice_to(candles: Sequence[Candle], ts: int) -> List[Candle]:
    """Solo le barre CHIUSE al momento ts: nessuna informazione dal futuro."""
    return [c for c in candles if c.start < ts]


def _forward(candles_15m: Sequence[Candle], ts: int, bars: int) -> Optional[List[Candle]]:
    future = [c for c in candles_15m if c.start >= ts]
    if len(future) < bars:
        return None
    return future[:bars]


def _excursions(window: Sequence[Candle], direction: str, entry: float, stop: float, target: float) -> Dict:
    sign = 1.0 if direction == "long" else -1.0
    risk = abs(entry - stop) or None
    best, worst, hit = entry, entry, "none"
    for c in window:
        fav = c.high if direction == "long" else c.low
        adv = c.low if direction == "long" else c.high
        if sign * (fav - best) > 0:
            best = fav
        if sign * (adv - worst) < 0:
            worst = adv
        if hit == "none":
            stop_touched = c.low <= stop if direction == "long" else c.high >= stop
            target_touched = c.high >= target if direction == "long" else c.low <= target
            if stop_touched:
                hit = "stop"
            elif target_touched:
                hit = "target"
    return {
        "ret": sign * (window[-1].close - entry) / entry,
        "mfe": (sign * (best - entry) / risk) if risk else None,
        "mae": (sign * (worst - entry) / risk) if risk else None,
        "hit": hit,
    }


def replay_symbol(
    symbol: str,
    candles_15m: List[Candle],
    candles_1h: List[Candle],
    btc_15m: List[Candle],
    step_bars: int = 4,
    min_score: float = 0.0,
) -> List[Dict]:
    """Ricalcola lo score ogni `step_bars` barre e misura cosa e' successo dopo."""
    results: List[Dict] = []
    # Serve abbastanza storia a sinistra per RVOL (100 barre) ed EMA50 su 1h.
    start_index = 220
    horizons = {"1h": BARS_1H, "4h": BARS_4H, "8h": BARS_4H * 2}

    for i in range(start_index, len(candles_15m) - BARS_4H * 2, step_bars):
        ts = candles_15m[i].start
        hist_15m = candles_15m[:i]
        hist_1h = _slice_to(candles_1h, ts)
        if len(hist_1h) < 60:
            continue

        btc_hist = _slice_to(btc_15m, ts)
        btc_ret_4h = None
        if len(btc_hist) > BARS_4H:
            past = btc_hist[-(BARS_4H + 1)].close
            if past > 0:
                btc_ret_4h = (btc_hist[-1].close - past) / past

        feats = compute_features(
            symbol=symbol,
            candles_15m=hist_15m,
            candles_1h=hist_1h,
            ticker={},                    # nessun dato di book/funding storico
            btc_ret_4h=btc_ret_4h,
            oi_series=None,
        )
        if feats is None:
            continue

        opp = score_symbol(feats, weights=BACKTEST_WEIGHTS)
        if opp is None or opp.plan is None or opp.score < min_score:
            continue

        row = {"ts": ts, "symbol": symbol, "direction": opp.direction, "score": opp.score}
        usable = False
        for label, bars in horizons.items():
            window = _forward(candles_15m, ts, bars)
            if window is None:
                continue
            exc = _excursions(window, opp.direction, opp.plan.entry, opp.plan.stop, opp.plan.target)
            row[f"ret_{label}"] = exc["ret"]
            row[f"hit_{label}"] = exc["hit"]
            row[f"mfe_{label}"] = exc["mfe"]
            row[f"mae_{label}"] = exc["mae"]
            usable = True
        if usable:
            results.append(row)
    return results


# ------------------------------------------------------------ aggregazione --
def aggregate(rows: List[Dict], horizon: str = "4h") -> List[Dict]:
    buckets: Dict[int, List[Dict]] = {}
    for r in rows:
        if r.get(f"ret_{horizon}") is None:
            continue
        buckets.setdefault(int(r["score"] // 10) * 10, []).append(r)

    out = []
    for bucket in sorted(buckets, reverse=True):
        group = buckets[bucket]
        rets = [g[f"ret_{horizon}"] for g in group]
        mfes = [g[f"mfe_{horizon}"] for g in group if g.get(f"mfe_{horizon}") is not None]
        maes = [g[f"mae_{horizon}"] for g in group if g.get(f"mae_{horizon}") is not None]
        hits = [g[f"hit_{horizon}"] for g in group]
        out.append({
            "bucket": bucket,
            "n": len(group),
            "win_rate": sum(1 for r in rets if r > 0) / len(rets),
            "avg_ret": statistics.mean(rets),
            "median_ret": statistics.median(rets),
            "avg_mfe": statistics.mean(mfes) if mfes else None,
            "avg_mae": statistics.mean(maes) if maes else None,
            "target_rate": hits.count("target") / len(hits),
            "stop_rate": hits.count("stop") / len(hits),
        })
    return out


def print_report(rows: List[Dict], horizons=("1h", "4h", "8h")) -> None:
    if not rows:
        print("\nNessuna osservazione: storico troppo corto o nessuna direzione chiara.\n")
        return

    print(f"\nOsservazioni totali: {len(rows)}")
    for horizon in horizons:
        table = aggregate(rows, horizon)
        if not table:
            continue
        print(f"\n--- Orizzonte {horizon} " + "-" * 52)
        print(f"{'fascia':>8} {'n':>6} {'win':>7} {'ret medio':>11} {'ret mediano':>12} "
              f"{'MFE(R)':>8} {'MAE(R)':>8} {'target':>8} {'stop':>7}")
        for r in table:
            print(
                f"{r['bucket']:>4}-{r['bucket'] + 9:<3} {r['n']:>6} "
                f"{r['win_rate'] * 100:>6.1f}% {r['avg_ret'] * 100:>10.2f}% "
                f"{r['median_ret'] * 100:>11.2f}% "
                f"{(r['avg_mfe'] or 0):>8.2f} {(r['avg_mae'] or 0):>8.2f} "
                f"{r['target_rate'] * 100:>7.0f}% {r['stop_rate'] * 100:>6.0f}%"
            )

    # Il test che conta: le fasce alte battono le fasce basse?
    table = aggregate(rows, "4h")
    high = [r for r in table if r["bucket"] >= 60]
    low = [r for r in table if r["bucket"] < 40]
    if high and low:
        h = sum(r["avg_ret"] * r["n"] for r in high) / sum(r["n"] for r in high)
        l = sum(r["avg_ret"] * r["n"] for r in low) / sum(r["n"] for r in low)
        print(f"\nScore >=60: {h * 100:+.2f}% medio a 4h   |   Score <40: {l * 100:+.2f}%")
        print("Verdetto:", "lo score discrimina." if h > l else
              "lo score NON discrimina: rivedi i pesi prima di operarci.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest dello score dello scanner")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", nargs="*", default=None, help="lista esplicita di simboli")
    parser.add_argument("--top", type=int, default=0, help="usa i N simboli piu' liquidi")
    parser.add_argument("--step", type=int, default=4, help="ogni quante barre 15m rivalutare (4 = 1h)")
    parser.add_argument("--offline", action="store_true", help="usa solo la cache su disco")
    args = parser.parse_args()

    client = None if args.offline else BybitClient()

    symbols = args.symbols or []
    if not symbols and args.top and client is not None:
        tickers = client.get_tickers()
        tickers = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        tickers.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
        symbols = [t["symbol"] for t in tickers[:args.top]]
    if not symbols:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    if "BTCUSDT" not in symbols:
        symbols = ["BTCUSDT"] + symbols

    print(f"Simboli: {len(symbols)} | giorni: {args.days} | passo: {args.step} barre 15m")
    btc_15m = get_history(client, "BTCUSDT", 15, args.days, args.offline)

    all_rows: List[Dict] = []
    for symbol in symbols:
        try:
            c15 = btc_15m if symbol == "BTCUSDT" else get_history(client, symbol, 15, args.days, args.offline)
            c1h = get_history(client, symbol, 60, args.days, args.offline)
        except BybitError as exc:
            print(f"  {symbol}: saltato ({exc})")
            continue
        rows = replay_symbol(symbol, c15, c1h, btc_15m, step_bars=args.step)
        print(f"  {symbol}: {len(rows)} osservazioni")
        all_rows.extend(rows)

    print_report(all_rows)


if __name__ == "__main__":
    main()
