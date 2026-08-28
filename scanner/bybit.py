"""Client per i dati di mercato pubblici di Bybit v5 (nessuna chiave API).

Pensato per lo scan di centinaia di simboli: sessione per-thread, retry con
backoff, rate limiter a token bucket e cache delle candele allineata alla
chiusura della barra (una candela 15m non cambia piu' una volta chiusa, quindi
riscaricarla a ogni ciclo e' solo spreco di rate limit).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

BASE_URL = "https://api.bybit.com"

# Intervalli Bybit espressi in minuti -> stringa attesa dall'API.
_INTERVAL_MAP = {
    1: "1", 3: "3", 5: "5", 15: "15", 30: "30",
    60: "60", 120: "120", 240: "240", 360: "360", 720: "720",
    1440: "D",
}


def _looks_blocked(exc: Optional[Exception]) -> bool:
    """Distingue un blocco (proxy/geo-block) da una rete semplicemente instabile.

    Il 403 puo' arrivare da Bybit oppure dal proxy sul tunnel CONNECT, e in quel
    secondo caso non c'e' nessuna risposta HTTP da ispezionare: resta solo il
    testo dell'eccezione.
    """
    if exc is None:
        return False
    text = str(exc).lower()
    return "403" in text or "forbidden" in text or "tunnel connection failed" in text


class BybitError(RuntimeError):
    """Errore applicativo restituito da Bybit (retCode != 0)."""


@dataclass
class Candle:
    start: int      # epoch ms di apertura della barra
    open: float
    high: float
    low: float
    close: float
    volume: float   # volume in base asset
    turnover: float  # volume in quote asset (USDT)


class _RateLimiter:
    """Token bucket semplice: al massimo `rate` richieste al secondo."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            if wait <= 0:
                self._next_slot = now + self.interval
                wait = 0.0
            else:
                self._next_slot += self.interval
        if wait > 0:
            time.sleep(wait)


class BybitClient:
    def __init__(
        self,
        category: str = "linear",
        timeout: int = 15,
        max_retries: int = 3,
        requests_per_second: float = 10.0,
    ):
        self.category = category
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = _RateLimiter(requests_per_second)
        self._local = threading.local()
        self._kline_cache: Dict[tuple, tuple] = {}   # (symbol, interval) -> (expires_at, candles)
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ http
    @property
    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers["User-Agent"] = "leskere-scanner/1.0"
            self._local.session = sess
        return sess

    def _get(self, path: str, params: dict) -> dict:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._limiter.acquire()
            try:
                resp = self._session.get(
                    f"{BASE_URL}{path}", params=params, timeout=self.timeout
                )
                # 403 = geo-block (runner cloud / ambiente con allowlist): inutile insistere.
                if resp.status_code == 403:
                    raise BybitError(
                        "403 da Bybit: host bloccato (geo-block o allowlist di rete). "
                        "Lo scanner va eseguito da una rete che raggiunge api.bybit.com."
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                if data.get("retCode") != 0:
                    raise BybitError(
                        f"retCode={data.get('retCode')}: {data.get('retMsg')} ({path})"
                    )
                return data["result"]
            except BybitError:
                raise
            except Exception as exc:                      # rete instabile: riprova
                last_exc = exc
                if _looks_blocked(exc):
                    break                                 # bloccati: inutile insistere
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))

        if _looks_blocked(last_exc):
            raise BybitError(
                "api.bybit.com non raggiungibile: connessione bloccata (403). "
                "Succede sui runner cloud GitHub e negli ambienti Claude Code on the web, "
                "che Bybit geo-blocca. Esegui lo scanner da una macchina che raggiunge "
                "l'API (es. il Mac)."
            )
        raise BybitError(f"richiesta fallita dopo {self.max_retries} tentativi: {last_exc}")

    # --------------------------------------------------------------- endpoint
    def get_tickers(self) -> List[dict]:
        """Snapshot di TUTTI i simboli della categoria in una sola chiamata.

        Campi utili: lastPrice, bid1Price, ask1Price, price24hPcnt, turnover24h,
        volume24h, openInterest, openInterestValue, fundingRate, nextFundingTime.
        """
        result = self._get("/v5/market/tickers", {"category": self.category})
        return result.get("list", [])

    def get_instruments(self) -> List[dict]:
        """Anagrafica strumenti: serve per status (Trading) e tick/qty step."""
        out: List[dict] = []
        cursor = ""
        while True:
            params = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = self._get("/v5/market/instruments-info", params)
            out.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    def get_klines(
        self,
        symbol: str,
        interval: int,
        limit: int = 200,
        use_cache: bool = True,
        end_ms: Optional[int] = None,
    ) -> List[Candle]:
        """Candele in ordine cronologico (vecchia -> nuova), CANDELA IN CORSO ESCLUSA.

        Lo scanner ragiona solo su barre chiuse: un breakout misurato su una barra
        ancora in formazione puo' sparire prima della chiusura.
        """
        if interval not in _INTERVAL_MAP:
            raise ValueError(f"intervallo non supportato: {interval}")

        cache_key = (symbol, interval)
        now = time.time()
        if use_cache and end_ms is None:
            with self._cache_lock:
                hit = self._kline_cache.get(cache_key)
            if hit and hit[0] > now:
                return hit[1]

        params = {
            "category": self.category,
            "symbol": symbol,
            "interval": _INTERVAL_MAP[interval],
            "limit": min(limit + 1, 1000),   # +1: l'ultima verra' scartata
        }
        if end_ms is not None:
            params["end"] = end_ms

        result = self._get("/v5/market/kline", params)
        rows = list(reversed(result.get("list", [])))   # Bybit: dal piu' recente
        candles = [
            Candle(
                start=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
                turnover=float(r[6]) if len(r) > 6 else 0.0,
            )
            for r in rows
        ]
        candles = drop_unclosed(candles, interval)[-limit:]

        if use_cache and end_ms is None and candles:
            # La cache scade quando chiude la barra successiva: fino ad allora
            # il risultato sarebbe identico.
            interval_ms = interval * 60_000
            expires_at = (candles[-1].start + 2 * interval_ms) / 1000.0
            with self._cache_lock:
                self._kline_cache[cache_key] = (expires_at, candles)
        return candles

    def get_open_interest(
        self, symbol: str, interval_time: str = "5min", limit: int = 200
    ) -> List[dict]:
        """Storico open interest, ordine cronologico. Voci: {timestamp, openInterest}."""
        result = self._get(
            "/v5/market/open-interest",
            {
                "category": self.category,
                "symbol": symbol,
                "intervalTime": interval_time,
                "limit": limit,
            },
        )
        rows = result.get("list", [])
        out = [
            {"timestamp": int(r["timestamp"]), "openInterest": float(r["openInterest"])}
            for r in rows
        ]
        out.sort(key=lambda x: x["timestamp"])
        return out


def drop_unclosed(candles: List[Candle], interval_minutes: int, now_ms: Optional[int] = None) -> List[Candle]:
    """Rimuove dalla coda le barre non ancora chiuse."""
    if not candles:
        return candles
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    interval_ms = interval_minutes * 60_000
    out = list(candles)
    while out and out[-1].start + interval_ms > now_ms:
        out.pop()
    return out
