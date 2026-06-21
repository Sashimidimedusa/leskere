"""Client minimale per i dati di mercato pubblici di Bybit (API v5).

Non servono chiavi API: vengono usati solo endpoint pubblici di mercato.
"""

from __future__ import annotations

from typing import List

import requests

BASE_URL = "https://api.bybit.com"


class BybitError(RuntimeError):
    pass


class BybitClient:
    def __init__(self, category: str = "linear", timeout: int = 10):
        self.category = category
        self.timeout = timeout
        self.session = requests.Session()

    def get_klines(self, symbol: str, interval: int, limit: int = 200) -> List[dict]:
        """Restituisce le candele in ordine cronologico (vecchia -> nuova).

        Ogni candela: {start, open, high, low, close, volume}.
        L'ultima della lista puo' essere la candela ANCORA IN FORMAZIONE.
        """
        params = {
            "category": self.category,
            "symbol": symbol,
            "interval": str(interval),
            "limit": limit,
        }
        url = f"{BASE_URL}/v5/market/kline"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        if data.get("retCode") != 0:
            raise BybitError(f"Bybit retCode={data.get('retCode')}: {data.get('retMsg')}")

        rows = data["result"]["list"]
        # Bybit restituisce dal piu' recente al piu' vecchio: invertiamo.
        rows = list(reversed(rows))
        candles = []
        for r in rows:
            candles.append(
                {
                    "start": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                }
            )
        return candles
