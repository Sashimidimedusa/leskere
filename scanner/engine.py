"""Motore di scansione a due stadi.

Stadio 1 - una sola chiamata `tickers` copre TUTTO il mercato: da li' ricaviamo
liquidita', variazione 1h e 24h, posizione nel range giornaliero e funding di
ogni simbolo. Filtriamo per liquidita' e teniamo i primi N per pre-score.

Stadio 2 - solo per quei N candidati scarichiamo candele 15m/1h e open interest,
calcoliamo le feature complete e assegniamo il punteggio.

Senza l'imbuto servirebbero ~1200 richieste per ciclo su 400 simboli; con
l'imbuto (e la cache sulle barre chiuse) siamo intorno a 120 la prima volta e
molte meno nei cicli successivi dentro la stessa candela.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .bybit import BybitClient, BybitError, Candle
from .features import BARS_4H, compute_features
from .indicators import scale
from .score import Opportunity, score_symbol
from .store import Store

BTC_SYMBOL = "BTCUSDT"


@dataclass
class ScanResult:
    ts: int                                   # epoch ms di fine scan
    opportunities: List[Opportunity] = field(default_factory=list)
    universe: int = 0
    candidates: int = 0
    errors: int = 0
    duration_s: float = 0.0
    btc_ret_4h: Optional[float] = None
    error_message: Optional[str] = None       # errore globale (rete/geo-block)


def _fnum(d: dict, key: str) -> Optional[float]:
    try:
        raw = d.get(key)
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def prescore_ticker(t: dict) -> Optional[Tuple[float, dict]]:
    """Pre-punteggio dai soli dati del ticker (nessuna chiamata aggiuntiva).

    Serve solo a scegliere CHI approfondire: non deve essere preciso, deve
    essere gratis e non scartare per errore un candidato valido.
    """
    last = _fnum(t, "lastPrice")
    if not last or last <= 0:
        return None

    prev_1h = _fnum(t, "prevPrice1h")
    ret_1h = (last - prev_1h) / prev_1h if prev_1h else None
    ret_24h = _fnum(t, "price24hPcnt")
    turnover = _fnum(t, "turnover24h") or 0.0

    high = _fnum(t, "highPrice24h")
    low = _fnum(t, "lowPrice24h")
    range_extremity = 0.0
    if high and low and high > low:
        pos = (last - low) / (high - low)
        # 0 al centro del range, 1 ai bordi: ci interessa chi sta rompendo.
        range_extremity = abs(pos - 0.5) * 2

    score = (
        0.45 * scale(abs(ret_1h) if ret_1h is not None else None, 0.003, 0.030)
        + 0.20 * scale(abs(ret_24h) if ret_24h is not None else None, 0.02, 0.15)
        + 0.20 * range_extremity
        + 0.15 * scale(turnover, 5e6, 3e8)
    )
    return score, {
        "ret_1h": ret_1h,
        "ret_24h": ret_24h,
        "turnover_24h": turnover,
    }


class Scanner:
    def __init__(self, config: dict, client: Optional[BybitClient] = None, store: Optional[Store] = None):
        self.cfg = config
        self.client = client or BybitClient(
            category=config.get("category", "linear"),
            requests_per_second=config.get("requests_per_second", 10.0),
        )
        self.store = store
        self.latest: Optional[ScanResult] = None

        self._tradable: Optional[set] = None
        self._tradable_ts = 0.0
        self._oi_cache: Dict[str, Tuple[float, list]] = {}

    # ------------------------------------------------------------- universo
    def tradable_symbols(self) -> set:
        """Simboli perpetual USDT effettivamente in negoziazione (cache 6h)."""
        if self._tradable is not None and time.time() - self._tradable_ts < 6 * 3600:
            return self._tradable
        symbols = set()
        for inst in self.client.get_instruments():
            if inst.get("status") != "Trading":
                continue
            if inst.get("quoteCoin") != "USDT":
                continue
            # esclude i future con scadenza: vogliamo solo i perpetui
            if inst.get("contractType") not in (None, "", "LinearPerpetual"):
                continue
            symbols.add(inst["symbol"])
        self._tradable = symbols
        self._tradable_ts = time.time()
        return symbols

    def build_universe(self, tickers: List[dict]) -> List[dict]:
        min_turnover = self.cfg.get("min_turnover_24h", 20_000_000)
        max_spread_bp = self.cfg.get("max_spread_bp", 20.0)
        excluded = set(self.cfg.get("exclude_symbols", []) or [])
        tradable = self.tradable_symbols()

        out = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if symbol in excluded or symbol not in tradable:
                continue
            if (_fnum(t, "turnover24h") or 0) < min_turnover:
                continue
            bid, ask = _fnum(t, "bid1Price"), _fnum(t, "ask1Price")
            if bid and ask and bid > 0:
                spread_bp = (ask - bid) / ((ask + bid) / 2) * 10_000
                if spread_bp > max_spread_bp:
                    continue
            out.append(t)
        return out

    # ------------------------------------------------------- approfondimento
    def _open_interest(self, symbol: str) -> Optional[list]:
        """OI con cache di 5 minuti (la granularita' minima dell'endpoint)."""
        hit = self._oi_cache.get(symbol)
        now = time.time()
        if hit and now - hit[0] < 300:
            return hit[1]
        try:
            series = self.client.get_open_interest(symbol, "5min", limit=30)
        except BybitError:
            return None
        self._oi_cache[symbol] = (now, series)
        return series

    def _deep_scan_symbol(self, ticker: dict, btc_ret_4h: Optional[float]) -> Optional[Opportunity]:
        symbol = ticker["symbol"]
        candles_15m = self.client.get_klines(symbol, 15, limit=200)
        candles_1h = self.client.get_klines(symbol, 60, limit=120)
        oi_series = self._open_interest(symbol) if self.cfg.get("use_open_interest", True) else None

        feats = compute_features(
            symbol=symbol,
            candles_15m=candles_15m,
            candles_1h=candles_1h,
            ticker=ticker,
            btc_ret_4h=btc_ret_4h,
            oi_series=oi_series,
        )
        if feats is None:
            return None
        return score_symbol(
            feats,
            weights=self.cfg.get("weights"),
            stop_atr_mult=self.cfg.get("stop_atr_mult", 1.5),
            target_atr_mult=self.cfg.get("target_atr_mult", 3.0),
        )

    def _btc_return_4h(self) -> Optional[float]:
        """Rendimento 4h di BTC: riferimento per la forza relativa.

        Senza questo, in una giornata in cui sale tutto lo scanner premierebbe
        semplicemente il beta di mercato invece dei simboli che si muovono da soli.
        """
        try:
            candles: List[Candle] = self.client.get_klines(BTC_SYMBOL, 15, limit=BARS_4H + 5)
        except BybitError:
            return None
        if len(candles) < BARS_4H + 1:
            return None
        past = candles[-(BARS_4H + 1)].close
        if past <= 0:
            return None
        return (candles[-1].close - past) / past

    # ------------------------------------------------------------------ scan
    def scan(self) -> ScanResult:
        started = time.time()
        try:
            tickers = self.client.get_tickers()
        except BybitError as exc:
            result = ScanResult(
                ts=int(time.time() * 1000),
                duration_s=time.time() - started,
                error_message=str(exc),
            )
            self.latest = result
            return result

        universe = self.build_universe(tickers)

        prescored = []
        for t in universe:
            pre = prescore_ticker(t)
            if pre is not None:
                prescored.append((pre[0], t))
        prescored.sort(key=lambda x: x[0], reverse=True)

        top_n = self.cfg.get("deep_scan_top", 60)
        candidates = [t for _, t in prescored[:top_n]]

        # I simboli fissati in config entrano sempre, anche se poco mossi.
        always = set(self.cfg.get("always_scan", []) or [])
        if always:
            have = {t["symbol"] for t in candidates}
            candidates += [t for t in universe if t["symbol"] in always and t["symbol"] not in have]

        btc_ret_4h = self._btc_return_4h()

        opportunities: List[Opportunity] = []
        errors = 0
        workers = self.cfg.get("workers", 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._deep_scan_symbol, t, btc_ret_4h): t["symbol"]
                for t in candidates
            }
            for fut in as_completed(futures):
                try:
                    opp = fut.result()
                except Exception:            # un simbolo rotto non ferma lo scan
                    errors += 1
                    continue
                if opp is not None:
                    opportunities.append(opp)

        min_score = self.cfg.get("min_display_score", 25)
        opportunities = [o for o in opportunities if o.score >= min_score]
        opportunities.sort(key=lambda o: o.score, reverse=True)

        result = ScanResult(
            ts=int(time.time() * 1000),
            opportunities=opportunities,
            universe=len(universe),
            candidates=len(candidates),
            errors=errors,
            duration_s=round(time.time() - started, 2),
            btc_ret_4h=btc_ret_4h,
        )
        self.latest = result

        if self.store is not None:
            self._persist(result)
        return result

    def _persist(self, result: ScanResult) -> List[Opportunity]:
        """Registra i segnali sopra soglia, con cooldown per simbolo/direzione."""
        assert self.store is not None
        self.store.record_scan(
            result.universe, result.candidates, len(result.opportunities), result.duration_s
        )

        threshold = self.cfg.get("signal_score", 60)
        cooldown = self.cfg.get("cooldown_minutes", 120)
        logged: List[Opportunity] = []
        for opp in result.opportunities:
            if opp.score < threshold or opp.plan is None:
                continue
            if self.store.recent_signal_exists(opp.symbol, opp.direction, cooldown):
                continue
            self.store.insert_signal(opp, ts_ms=result.ts)
            logged.append(opp)
        return logged
