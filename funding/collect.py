"""Raccolta dati per lo studio Funding Edge (Fase 1 — solo descrittivo).

Scarica da Bybit (API v5 pubblica, niente chiavi) lo storico funding rate dei
perpetual USDT, e lo arricchisce con mark/spot price all'istante esatto di
settlement per poter calcolare il basis (mark - spot).

Endpoint usati (NB: NON sono Binance):
  - GET /v5/market/funding/history   -> funding_rate + fundingRateTimestamp
  - GET /v5/market/mark-price-kline   -> mark price all'istante di funding
  - GET /v5/market/kline?category=spot-> spot price all'istante di funding
  - GET /v5/market/open-interest      -> open interest storico (best-effort, opz.)

Principi (regola dura del progetto):
  - Niente logica di trading/dashboard: SOLO raccolta + verifica.
  - Idempotente / riprendibile: upsert su (symbol, funding_time); non riscarica
    le finestre gia' coperte ne' ri-arricchisce righe gia' con prezzo.
  - Nessun riempimento silenzioso: cio' che manca resta NULL ed e' SEGNALATO
    dalle asserzioni di verifica (righe per symbol, range date, buchi).

La tabella DuckDB `funding_history` e' l'unica interfaccia verso analyze.py.

Uso tipico (in locale, sul tuo Mac, con accesso reale alla rete):
    python collect.py                      # scarica tutti i DEFAULT_SYMBOLS
    python collect.py --symbols BTCUSDT ETHUSDT
    python collect.py --start 2022-01-01 --end 2025-01-01
    python collect.py --verify-only        # solo le asserzioni, niente download
    python collect.py --with-oi            # tenta anche l'open interest (recente)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests

try:
    import duckdb
except ImportError:  # pragma: no cover - messaggio chiaro se manca la dipendenza
    sys.exit(
        "Manca duckdb. Installa le dipendenze:\n"
        "    pip install -r funding/requirements.txt"
    )

# ---------------------------------------------------------------------------
# CONFIGURAZIONE (parametri in chiaro, modificabili)
# ---------------------------------------------------------------------------

BASE_URL = "https://api.bybit.com"
CATEGORY = "linear"  # USDT perpetual

# Universo: non solo BTC/ETH. Mix di major + alt con funding storicamente alto e
# basis piu' volatile. Lista configurabile via --symbols.
DEFAULT_SYMBOLS: List[str] = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "MATICUSDT", "DOTUSDT",
    "LINKUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "FILUSDT", "INJUSDT", "SUIUSDT",
    "TRXUSDT", "ETCUSDT", "AAVEUSDT", "UNIUSDT", "FTMUSDT",
]

# Periodo di default: deve includere il bear 2022 (funding negativo, basis vero).
DEFAULT_START = "2022-01-01"
DEFAULT_END: Optional[str] = None  # None = adesso (UTC)

# Le kline per mark/spot vengono scaricate a questo intervallo. 60 (= 1h) si
# allinea per costruzione ai settlement (00/08/16 UTC e anche funding a 1/2/4h).
KLINE_INTERVAL = "60"

# Limiti di pagina degli endpoint Bybit v5.
FUNDING_PAGE_LIMIT = 200    # max consentito da funding/history
KLINE_PAGE_LIMIT = 1000     # max consentito da kline / mark-price-kline
OI_PAGE_LIMIT = 200

# Rate limit / retry.
REQUEST_SLEEP = 0.12        # pausa fra richieste (gentile con il rate-limit IP)
MAX_RETRIES = 6
BACKOFF_BASE = 2.0          # 2s, 4s, 8s, ... su 429/418/403/errori di rete
RATELIMIT_RET_CODES = {10006, 10018}  # codici Bybit "too many requests"

DEFAULT_DB_PATH = "funding/data/funding.duckdb"
# Report leggibile (Markdown). E' committabile: lo apri da GitHub sull'iPhone.
DEFAULT_REPORT_PATH = "funding/reports/verification-latest.md"

MS = 1000  # millisecondi per secondo


# ---------------------------------------------------------------------------
# UTILITA' DI TEMPO
# ---------------------------------------------------------------------------

def _to_utc_ms(date_str_or_dt) -> int:
    """Converte 'YYYY-MM-DD' (o datetime) in epoch millisecondi UTC."""
    if isinstance(date_str_or_dt, datetime):
        dt = date_str_or_dt
    else:
        dt = datetime.strptime(date_str_or_dt, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * MS)


def _ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> datetime UTC naive (per DuckDB TIMESTAMP, sempre UTC)."""
    return datetime.fromtimestamp(ms / MS, tz=timezone.utc).replace(tzinfo=None)


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * MS)


# ---------------------------------------------------------------------------
# LAYER HTTP (retry + backoff + rate-limit)
# ---------------------------------------------------------------------------

class BybitError(RuntimeError):
    pass


def _request(session: requests.Session, path: str, params: dict) -> dict:
    """GET su Bybit v5 con retry/backoff. Restituisce result["list"] grezzo.

    Gestisce esplicitamente 429/418/403 (HTTP) e i retCode di rate-limit Bybit.
    Solleva BybitError se dopo i retry non riesce o se retCode != 0 per un motivo
    non di rate-limit (cosi' un problema NON passa silenzioso).
    """
    url = f"{BASE_URL}{path}"
    last_err: Optional[str] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            last_err = f"network error: {exc}"
            _backoff(attempt, last_err)
            continue

        if resp.status_code in (429, 418, 403):
            last_err = f"HTTP {resp.status_code} (rate limit)"
            _backoff(attempt, last_err)
            continue
        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code} (server)"
            _backoff(attempt, last_err)
            continue
        if resp.status_code != 200:
            raise BybitError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        ret = data.get("retCode")
        if ret in RATELIMIT_RET_CODES:
            last_err = f"retCode {ret} (rate limit): {data.get('retMsg')}"
            _backoff(attempt, last_err)
            continue
        if ret != 0:
            raise BybitError(f"{path}: retCode={ret}: {data.get('retMsg')}")

        time.sleep(REQUEST_SLEEP)
        return data.get("result", {}) or {}

    raise BybitError(f"{path}: esauriti i retry. Ultimo errore: {last_err}")


def _backoff(attempt: int, reason: str) -> None:
    wait = BACKOFF_BASE ** attempt
    print(f"    [retry {attempt + 1}/{MAX_RETRIES}] {reason} -> attendo {wait:.0f}s",
          file=sys.stderr)
    time.sleep(wait)


# ---------------------------------------------------------------------------
# FETCH: funding history (paginato all'indietro su tutto il range)
# ---------------------------------------------------------------------------

def fetch_funding_history(
    session: requests.Session, symbol: str, start_ms: int, end_ms: int
) -> List[Tuple[int, float]]:
    """Restituisce [(funding_time_ms, funding_rate), ...] in [start_ms, end_ms].

    L'endpoint restituisce fino a FUNDING_PAGE_LIMIT righe in ordine decrescente
    di tempo. Pagino spostando endTime sotto al piu' vecchio ricevuto, finche'
    non scendo sotto start_ms o non arrivano piu' righe.
    """
    out: List[Tuple[int, float]] = []
    cursor_end = end_ms
    seen: set = set()
    while cursor_end >= start_ms:
        result = _request(session, "/v5/market/funding/history", {
            "category": CATEGORY,
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": cursor_end,
            "limit": FUNDING_PAGE_LIMIT,
        })
        rows = result.get("list", []) or []
        if not rows:
            break
        oldest = cursor_end
        new_in_page = 0
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            rate = float(r["fundingRate"])
            oldest = min(oldest, ts)
            if ts < start_ms or ts in seen:
                continue
            seen.add(ts)
            out.append((ts, rate))
            new_in_page += 1
        # Avanza la finestra appena sotto il piu' vecchio della pagina.
        next_end = oldest - 1
        if next_end >= cursor_end or new_in_page == 0:
            break  # niente progresso: evita loop infiniti
        cursor_end = next_end
    out.sort()
    return out


# ---------------------------------------------------------------------------
# FETCH: kline (mark o spot) -> mappa {funding_time_ms: open_price}
# ---------------------------------------------------------------------------

def fetch_price_klines(
    session: requests.Session, symbol: str, kind: str, start_ms: int, end_ms: int
) -> Dict[int, float]:
    """Scarica kline e restituisce {start_ms_candela: open_price}.

    kind = "mark" -> /v5/market/mark-price-kline (category linear)
    kind = "spot" -> /v5/market/kline (category spot)
    Uso l'OPEN della candela il cui start coincide col funding_time: e' il prezzo
    a quell'istante. Paginazione all'indietro a blocchi di KLINE_PAGE_LIMIT.
    """
    if kind == "mark":
        path = "/v5/market/mark-price-kline"
        category = CATEGORY
    elif kind == "spot":
        path = "/v5/market/kline"
        category = "spot"
    else:
        raise ValueError(f"kind sconosciuto: {kind}")

    prices: Dict[int, float] = {}
    cursor_end = end_ms
    while cursor_end >= start_ms:
        result = _request(session, path, {
            "category": category,
            "symbol": symbol,
            "interval": KLINE_INTERVAL,
            "start": start_ms,
            "end": cursor_end,
            "limit": KLINE_PAGE_LIMIT,
        })
        rows = result.get("list", []) or []
        if not rows:
            break
        oldest = cursor_end
        for r in rows:
            ts = int(r[0])           # start della candela (ms)
            open_px = float(r[1])    # open
            oldest = min(oldest, ts)
            if ts >= start_ms:
                prices[ts] = open_px
        next_end = oldest - 1
        if next_end >= cursor_end:
            break
        cursor_end = next_end
    return prices


def fetch_open_interest(
    session: requests.Session, symbol: str, start_ms: int, end_ms: int
) -> Dict[int, float]:
    """Open interest storico (best-effort). Retention Bybit limitata: per il 2022
    quasi sicuramente vuoto. Cio' che manca resta NULL (segnalato in verifica)."""
    oi: Dict[int, float] = {}
    cursor_end = end_ms
    while cursor_end >= start_ms:
        try:
            result = _request(session, "/v5/market/open-interest", {
                "category": CATEGORY,
                "symbol": symbol,
                "intervalTime": "1h",
                "startTime": start_ms,
                "endTime": cursor_end,
                "limit": OI_PAGE_LIMIT,
            })
        except BybitError as exc:
            print(f"    [oi] {symbol}: non disponibile ({exc})", file=sys.stderr)
            break
        rows = result.get("list", []) or []
        if not rows:
            break
        oldest = cursor_end
        for r in rows:
            ts = int(r["timestamp"])
            oldest = min(oldest, ts)
            if ts >= start_ms:
                oi[ts] = float(r["openInterest"])
        next_end = oldest - 1
        if next_end >= cursor_end:
            break
        cursor_end = next_end
    return oi


# ---------------------------------------------------------------------------
# LAYER DUCKDB (schema + upsert idempotente)
# ---------------------------------------------------------------------------

def connect(db_path: str) -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect(db_path)
    ensure_schema(con)
    return con


def ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS funding_history (
            symbol         VARCHAR    NOT NULL,
            funding_time   TIMESTAMP  NOT NULL,   -- UTC, istante di settlement
            funding_rate   DOUBLE     NOT NULL,   -- tasso del periodo
            mark_price     DOUBLE,                -- perp @ funding_time
            spot_price     DOUBLE,                -- spot @ funding_time
            basis          DOUBLE,                -- mark_price - spot_price
            volume_24h     DOUBLE,                -- opzionale (NULL di default)
            open_interest  DOUBLE,                -- opzionale (NULL/best-effort)
            ingested_at    TIMESTAMP  DEFAULT now(),  -- audit (rimovibile)
            PRIMARY KEY (symbol, funding_time)
        )
        """
    )


def existing_range(con, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
    """(min_ms, max_ms, count) gia' presenti per symbol. None,None,0 se vuoto."""
    row = con.execute(
        "SELECT min(funding_time), max(funding_time), count(*) "
        "FROM funding_history WHERE symbol = ?",
        [symbol],
    ).fetchone()
    mn, mx, cnt = row
    mn_ms = int(mn.replace(tzinfo=timezone.utc).timestamp() * MS) if mn else None
    mx_ms = int(mx.replace(tzinfo=timezone.utc).timestamp() * MS) if mx else None
    return mn_ms, mx_ms, int(cnt)


def upsert_funding(con, symbol: str, rows: List[Tuple[int, float]]) -> int:
    """Inserisce (symbol, funding_time, funding_rate). Idempotente: ON CONFLICT
    aggiorna solo il rate (non sovrascrive mark/spot gia' arricchiti)."""
    if not rows:
        return 0
    payload = [(symbol, _ms_to_dt(ts), rate) for ts, rate in rows]
    con.executemany(
        """
        INSERT INTO funding_history (symbol, funding_time, funding_rate)
        VALUES (?, ?, ?)
        ON CONFLICT (symbol, funding_time)
        DO UPDATE SET funding_rate = excluded.funding_rate
        """,
        payload,
    )
    return len(payload)


def rows_needing_prices(con, symbol: str) -> List[int]:
    """funding_time (ms) delle righe senza mark o spot: solo queste vanno arricchite."""
    res = con.execute(
        "SELECT funding_time FROM funding_history "
        "WHERE symbol = ? AND (mark_price IS NULL OR spot_price IS NULL) "
        "ORDER BY funding_time",
        [symbol],
    ).fetchall()
    return [int(r[0].replace(tzinfo=timezone.utc).timestamp() * MS) for r in res]


def update_prices(
    con,
    symbol: str,
    mark: Dict[int, float],
    spot: Dict[int, float],
    oi: Optional[Dict[int, float]],
    need: Iterable[int],
) -> Tuple[int, int]:
    """Aggiorna mark/spot/basis (+oi opzionale) per i funding_time in `need`.

    Riempie SOLO con match di timestamp esatto. Nessun fill silenzioso: se manca
    la kline, la colonna resta NULL. Restituisce (righe_aggiornate, match_mancanti).
    """
    updates = []
    missing = 0
    for ts in need:
        m = mark.get(ts)
        s = spot.get(ts)
        if m is None or s is None:
            missing += 1
            continue
        basis = m - s
        oi_val = oi.get(ts) if oi else None
        updates.append((m, s, basis, oi_val, symbol, _ms_to_dt(ts)))
    if updates:
        con.executemany(
            """
            UPDATE funding_history
            SET mark_price = ?, spot_price = ?, basis = ?,
                open_interest = COALESCE(?, open_interest)
            WHERE symbol = ? AND funding_time = ?
            """,
            updates,
        )
    return len(updates), missing


# ---------------------------------------------------------------------------
# ORCHESTRAZIONE PER SYMBOL
# ---------------------------------------------------------------------------

def collect_symbol(
    con, session: requests.Session, symbol: str,
    want_start_ms: int, want_end_ms: int, with_oi: bool,
) -> None:
    print(f"\n=== {symbol} ===")
    mn, mx, cnt = existing_range(con, symbol)

    # 1) Funding: scarico solo le finestre NON coperte (head piu' vecchia + tail).
    windows: List[Tuple[int, int]] = []
    if cnt == 0 or mn is None:
        windows.append((want_start_ms, want_end_ms))
    else:
        if want_start_ms < mn:
            windows.append((want_start_ms, mn - 1))   # backfill storico
        if want_end_ms > mx:
            windows.append((mx + 1, want_end_ms))      # incrementale recente
        if not windows:
            print(f"  funding: gia' coperto [{_ms_to_dt(mn).date()} -> "
                  f"{_ms_to_dt(mx).date()}], {cnt} righe. Niente da scaricare.")

    total_new = 0
    for w_start, w_end in windows:
        print(f"  funding: scarico finestra {_ms_to_dt(w_start).date()} -> "
              f"{_ms_to_dt(w_end).date()} ...")
        rows = fetch_funding_history(session, symbol, w_start, w_end)
        n = upsert_funding(con, symbol, rows)
        total_new += n
        print(f"    -> {n} righe funding")
    if total_new:
        print(f"  funding: {total_new} righe nuove/aggiornate")

    # 2) Arricchimento prezzi: solo le righe ancora senza mark/spot.
    need = rows_needing_prices(con, symbol)
    if not need:
        print("  prezzi: tutte le righe gia' arricchite (mark/spot presenti).")
        return
    px_start, px_end = need[0], need[-1]
    print(f"  prezzi: arricchisco {len(need)} righe "
          f"({_ms_to_dt(px_start).date()} -> {_ms_to_dt(px_end).date()}) ...")
    mark = fetch_price_klines(session, symbol, "mark", px_start, px_end)
    spot = fetch_price_klines(session, symbol, "spot", px_start, px_end)
    oi = fetch_open_interest(session, symbol, px_start, px_end) if with_oi else None
    updated, missing = update_prices(con, symbol, mark, spot, oi, need)
    print(f"    -> mark kline: {len(mark)}, spot kline: {len(spot)}")
    print(f"    -> {updated} righe arricchite, {missing} senza match prezzo (restano NULL)")


# ---------------------------------------------------------------------------
# VERIFICA (asserzioni esplicite: righe, range, buchi) — niente fill silenzioso
# ---------------------------------------------------------------------------

def _modal_interval_ms(times_ms: List[int]) -> Optional[int]:
    """Delta modale fra settlement consecutivi (per derivare la griglia attesa)."""
    if len(times_ms) < 2:
        return None
    deltas: Dict[int, int] = {}
    for a, b in zip(times_ms, times_ms[1:]):
        d = b - a
        if d > 0:
            deltas[d] = deltas.get(d, 0) + 1
    if not deltas:
        return None
    return max(deltas, key=deltas.get)


def _symbol_stats(con, symbol: str) -> dict:
    """Statistiche di verifica per un symbol (usate sia da console che report).

    Calcola righe, range date, intervallo di funding derivato, buchi attesi e
    assenti (con campione dei primi 5), e righe senza mark/spot. Nessun fill: i
    mancanti sono solo contati e segnalati.
    """
    res = con.execute(
        "SELECT funding_time, mark_price, spot_price FROM funding_history "
        "WHERE symbol = ? ORDER BY funding_time",
        [symbol],
    ).fetchall()
    n = len(res)
    if n == 0:
        return {"symbol": symbol, "rows": 0}

    times_ms = [int(r[0].replace(tzinfo=timezone.utc).timestamp() * MS) for r in res]
    no_mark = sum(1 for r in res if r[1] is None)
    no_spot = sum(1 for r in res if r[2] is None)

    interval_ms = _modal_interval_ms(times_ms)
    missing_samples: List[datetime] = []
    if interval_ms:
        interval_h = interval_ms / 3_600_000
        expected = (times_ms[-1] - times_ms[0]) // interval_ms + 1
        gaps = max(0, expected - n)
        if gaps > 0:
            present = set(times_ms)
            t = times_ms[0]
            while t <= times_ms[-1] and len(missing_samples) < 5:
                if t not in present:
                    missing_samples.append(_ms_to_dt(t))
                t += interval_ms
    else:
        interval_h = None
        gaps = 0

    return {
        "symbol": symbol,
        "rows": n,
        "first": _ms_to_dt(times_ms[0]).date(),
        "last": _ms_to_dt(times_ms[-1]).date(),
        "interval_h": interval_h,
        "gaps": gaps,
        "missing_samples": missing_samples,
        "no_mark": no_mark,
        "no_spot": no_spot,
    }


def _intv_str(stats: dict) -> str:
    h = stats.get("interval_h")
    return f"{h:.0f}h" if h else "?"


def verify(con, symbols: List[str]) -> bool:
    """Stampa la tabella di verifica per ogni symbol e segnala i buchi.

    Restituisce True se nessun symbol ha problemi bloccanti (zero righe), False
    altrimenti. I buchi nella serie e i prezzi mancanti sono segnalati ma non
    bloccanti: vanno valutati da te prima di passare ad analyze.py.
    """
    print("\n" + "=" * 78)
    print("VERIFICA RACCOLTA — funding_history")
    print("=" * 78)
    header = (f"{'symbol':<11}{'righe':>7}{'dal':>12}{'al':>12}"
              f"{'intv':>6}{'buchi':>7}{'noMark':>8}{'noSpot':>8}")
    print(header)
    print("-" * len(header))

    all_ok = True
    for symbol in symbols:
        s = _symbol_stats(con, symbol)
        if s["rows"] == 0:
            print(f"{symbol:<11}{0:>7}{'-':>12}{'-':>12}{'-':>6}{'-':>7}{'-':>8}{'-':>8}"
                  "   << NESSUNA RIGA")
            all_ok = False
            continue

        flag = ""
        if s["gaps"] > 0:
            flag += f"  << {s['gaps']} buchi nella serie"
        if s["no_mark"] or s["no_spot"]:
            flag += "  << prezzi mancanti"

        print(f"{symbol:<11}{s['rows']:>7}{str(s['first']):>12}{str(s['last']):>12}"
              f"{_intv_str(s):>6}{s['gaps']:>7}{s['no_mark']:>8}{s['no_spot']:>8}{flag}")

        if s["missing_samples"]:
            shown = ", ".join(str(d) for d in s["missing_samples"])
            more = " ..." if s["gaps"] > len(s["missing_samples"]) else ""
            print(f"            buchi (primi): {shown}{more}")

    print("-" * len(header))
    print("Legenda: intv = intervallo di funding derivato dai dati; "
          "buchi = settlement attesi e assenti;")
    print("         noMark/noSpot = righe senza prezzo (restano NULL, non riempite).")
    if not all_ok:
        print("\n[!] Almeno un symbol non ha righe: controlla rete/allowlist/symbol.")
    return all_ok


# ---------------------------------------------------------------------------
# REPORT MARKDOWN (per leggere il risultato dall'iPhone via GitHub)
# ---------------------------------------------------------------------------

def build_markdown_report(con, symbols: List[str], db_path: str,
                          start_ms: int, end_ms: int) -> Tuple[str, bool]:
    """Costruisce il report di verifica in Markdown. (testo, all_ok).

    GitHub rende il Markdown anche da mobile: committando questo file il
    risultato e' leggibile dall'iPhone senza eseguire nulla sul telefono.
    """
    stats = [_symbol_stats(con, s) for s in symbols]
    all_ok = all(s["rows"] > 0 for s in stats)
    have_issues = any(
        s["rows"] == 0 or s["gaps"] > 0 or s["no_mark"] or s["no_spot"]
        for s in stats
    )
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    period = (f"{datetime.utcfromtimestamp(start_ms / MS).date()} → "
              f"{datetime.utcfromtimestamp(end_ms / MS).date()}")
    status = "⚠️ ATTENZIONE" if (have_issues or not all_ok) else "✅ OK"

    lines: List[str] = []
    lines.append("# Funding Edge — Report di verifica raccolta")
    lines.append("")
    lines.append(f"_Generato: {now}_  ·  DB: `{db_path}`")
    lines.append("")
    lines.append(f"- **Symbol richiesti:** {len(symbols)}")
    lines.append(f"- **Periodo richiesto:** {period}")
    lines.append(f"- **Stato complessivo:** {status}")
    lines.append("")
    lines.append("| symbol | righe | dal | al | intv | buchi | noMark | noSpot |")
    lines.append("|---|---:|---|---|---|---:|---:|---:|")
    for s in stats:
        if s["rows"] == 0:
            lines.append(f"| **{s['symbol']}** | 0 | – | – | – | – | – | – |")
            continue
        lines.append(
            f"| {s['symbol']} | {s['rows']} | {s['first']} | {s['last']} | "
            f"{_intv_str(s)} | {s['gaps']} | {s['no_mark']} | {s['no_spot']} |"
        )
    lines.append("")

    detail = [s for s in stats if s["rows"] == 0 or s["gaps"] > 0
              or s["no_mark"] or s["no_spot"]]
    if detail:
        lines.append("## Dettaglio problemi")
        lines.append("")
        for s in detail:
            if s["rows"] == 0:
                lines.append(f"- **{s['symbol']}**: nessuna riga "
                             "(rete/allowlist/symbol?).")
                continue
            bits = []
            if s["gaps"] > 0:
                shown = ", ".join(str(d) for d in s["missing_samples"])
                more = " …" if s["gaps"] > len(s["missing_samples"]) else ""
                bits.append(f"{s['gaps']} buchi (primi: {shown}{more})")
            if s["no_mark"] or s["no_spot"]:
                bits.append(f"prezzi mancanti: noMark={s['no_mark']}, "
                            f"noSpot={s['no_spot']}")
            lines.append(f"- **{s['symbol']}**: " + "; ".join(bits) + ".")
        lines.append("")

    lines.append("## Legenda")
    lines.append("")
    lines.append("- **intv** = intervallo di funding derivato dai dati "
                 "(non hardcodato).")
    lines.append("- **buchi** = settlement attesi sulla griglia e assenti.")
    lines.append("- **noMark / noSpot** = righe senza prezzo: restano `NULL`, "
                 "**non** vengono riempite.")
    lines.append("")
    return "\n".join(lines), all_ok


def write_report(text: str, path: str) -> None:
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\nReport scritto: {path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Raccolta funding history Bybit (Fase 1).")
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                   help="Lista symbol (default: universo predefinito).")
    p.add_argument("--start", default=DEFAULT_START, help="Data inizio YYYY-MM-DD.")
    p.add_argument("--end", default=DEFAULT_END,
                   help="Data fine YYYY-MM-DD (default: adesso).")
    p.add_argument("--db", default=DEFAULT_DB_PATH, help="Percorso file DuckDB.")
    p.add_argument("--with-oi", action="store_true",
                   help="Tenta anche l'open interest (best-effort, retention corta).")
    p.add_argument("--verify-only", action="store_true",
                   help="Solo asserzioni di verifica, nessun download.")
    p.add_argument("--report", default=DEFAULT_REPORT_PATH,
                   help="Percorso del report Markdown (leggibile da iPhone via GitHub).")
    p.add_argument("--no-report", action="store_true",
                   help="Non scrivere il report Markdown.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    start_ms = _to_utc_ms(args.start)
    end_ms = _to_utc_ms(args.end) if args.end else _now_ms()
    if start_ms >= end_ms:
        sys.exit("start deve essere < end.")

    con = connect(args.db)

    if not args.verify_only:
        session = requests.Session()
        print(f"Raccolta {len(args.symbols)} symbol, "
              f"{datetime.utcfromtimestamp(start_ms / MS).date()} -> "
              f"{datetime.utcfromtimestamp(end_ms / MS).date()}, db={args.db}")
        for symbol in args.symbols:
            try:
                collect_symbol(con, session, symbol, start_ms, end_ms, args.with_oi)
            except BybitError as exc:
                print(f"  [!] {symbol}: errore di raccolta: {exc}", file=sys.stderr)

    ok = verify(con, args.symbols)

    if not args.no_report:
        text, _ = build_markdown_report(con, args.symbols, args.db, start_ms, end_ms)
        write_report(text, args.report)

    con.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
