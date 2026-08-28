"""Registro persistente su SQLite: segnali emessi ed esiti verificati.

E' la parte che trasforma "sembra funzionare" in un numero. Ogni segnale sopra
soglia viene scritto con tutte le sue feature; un tracker ripassa piu' tardi e
registra cosa e' successo davvero (rendimento, massimo favorevole, massimo
avverso, se e' arrivato prima lo stop o il target).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict, List, Optional

DEFAULT_DB = "scanner/data/scanner.db"

# Orizzonti su cui verifichiamo ogni segnale (in minuti).
HORIZONS = (60, 240, 480)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,          -- epoch ms di emissione
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    score        REAL    NOT NULL,
    price        REAL    NOT NULL,
    entry        REAL,
    stop         REAL,
    target       REAL,
    risk_reward  REAL,
    risk_pct     REAL,
    components   TEXT,
    penalties    TEXT,
    reasons      TEXT,
    features     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, direction, ts);

CREATE TABLE IF NOT EXISTS outcomes (
    signal_id     INTEGER NOT NULL,
    horizon_min   INTEGER NOT NULL,
    evaluated_ts  INTEGER NOT NULL,
    price_at      REAL,
    ret           REAL,     -- rendimento nel verso del segnale (0.01 = +1%)
    mfe           REAL,     -- massima escursione favorevole, in R
    mae           REAL,     -- massima escursione avversa, in R
    hit           TEXT,     -- 'target' | 'stop' | 'none'
    PRIMARY KEY (signal_id, horizon_min),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS scans (
    ts          INTEGER PRIMARY KEY,
    universe    INTEGER,
    candidates  INTEGER,
    scored      INTEGER,
    duration_s  REAL
);
"""


class Store:
    def __init__(self, path: str = DEFAULT_DB):
        import os

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL: la dashboard legge mentre lo scanner scrive, senza bloccarsi.
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- scrittura
    def recent_signal_exists(self, symbol: str, direction: str, within_minutes: int) -> bool:
        """Evita di riscrivere lo stesso segnale a ogni ciclo di scan."""
        cutoff = int(time.time() * 1000) - within_minutes * 60_000
        cur = self._conn.execute(
            "SELECT 1 FROM signals WHERE symbol=? AND direction=? AND ts>=? LIMIT 1",
            (symbol, direction, cutoff),
        )
        return cur.fetchone() is not None

    def insert_signal(self, opp, ts_ms: Optional[int] = None) -> int:
        ts_ms = ts_ms or int(time.time() * 1000)
        plan = opp.plan
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO signals
                   (ts, symbol, direction, score, price, entry, stop, target,
                    risk_reward, risk_pct, components, penalties, reasons, features)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts_ms,
                    opp.symbol,
                    opp.direction,
                    opp.score,
                    opp.price,
                    plan.entry if plan else None,
                    plan.stop if plan else None,
                    plan.target if plan else None,
                    plan.risk_reward if plan else None,
                    plan.risk_pct if plan else None,
                    json.dumps(opp.components),
                    json.dumps(opp.penalties),
                    json.dumps(opp.reasons, ensure_ascii=False),
                    json.dumps(opp.features),
                ),
            )
            return int(cur.lastrowid)

    def record_scan(self, universe: int, candidates: int, scored: int, duration_s: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO scans (ts, universe, candidates, scored, duration_s) VALUES (?,?,?,?,?)",
                (int(time.time() * 1000), universe, candidates, scored, round(duration_s, 2)),
            )

    def record_outcome(
        self,
        signal_id: int,
        horizon_min: int,
        price_at: Optional[float],
        ret: Optional[float],
        mfe: Optional[float],
        mae: Optional[float],
        hit: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO outcomes
                   (signal_id, horizon_min, evaluated_ts, price_at, ret, mfe, mae, hit)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (signal_id, horizon_min, int(time.time() * 1000), price_at, ret, mfe, mae, hit),
            )

    # --------------------------------------------------------------- lettura
    def pending_evaluations(self, horizons=HORIZONS) -> List[sqlite3.Row]:
        """Segnali il cui orizzonte e' maturato ma non ancora verificato."""
        now = int(time.time() * 1000)
        rows: List[sqlite3.Row] = []
        for horizon in horizons:
            cutoff = now - horizon * 60_000
            cur = self._conn.execute(
                """SELECT s.*, ? AS horizon_min FROM signals s
                   LEFT JOIN outcomes o
                     ON o.signal_id = s.id AND o.horizon_min = ?
                   WHERE s.ts <= ? AND o.signal_id IS NULL
                   ORDER BY s.ts ASC LIMIT 200""",
                (horizon, horizon, cutoff),
            )
            rows.extend(cur.fetchall())
        return rows

    def recent_signals(self, limit: int = 100) -> List[Dict]:
        cur = self._conn.execute(
            """SELECT s.*,
                      (SELECT hit FROM outcomes WHERE signal_id=s.id AND horizon_min=60)  AS hit_1h,
                      (SELECT ret FROM outcomes WHERE signal_id=s.id AND horizon_min=60)  AS ret_1h,
                      (SELECT ret FROM outcomes WHERE signal_id=s.id AND horizon_min=240) AS ret_4h
               FROM signals s ORDER BY s.ts DESC LIMIT ?""",
            (limit,),
        )
        out = []
        for row in cur.fetchall():
            item = dict(row)
            item["reasons"] = json.loads(item.get("reasons") or "[]")
            item.pop("features", None)
            item.pop("components", None)
            item.pop("penalties", None)
            out.append(item)
        return out

    def performance(self, horizon_min: int = 240, min_signals: int = 1) -> List[Dict]:
        """Statistiche per fascia di punteggio: e' il verdetto sullo scanner.

        Se le fasce alte non battono le fasce basse, lo score non sta
        discriminando nulla e i pesi vanno rivisti.
        """
        cur = self._conn.execute(
            """SELECT
                   CAST(s.score / 10 AS INTEGER) * 10 AS bucket,
                   COUNT(*)                            AS n,
                   AVG(o.ret)                          AS avg_ret,
                   AVG(CASE WHEN o.ret > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(o.mfe)                          AS avg_mfe,
                   AVG(o.mae)                          AS avg_mae,
                   AVG(CASE WHEN o.hit='target' THEN 1.0 ELSE 0.0 END) AS target_rate,
                   AVG(CASE WHEN o.hit='stop'   THEN 1.0 ELSE 0.0 END) AS stop_rate
               FROM signals s JOIN outcomes o ON o.signal_id = s.id
               WHERE o.horizon_min = ?
               GROUP BY bucket HAVING n >= ?
               ORDER BY bucket DESC""",
            (horizon_min, min_signals),
        )
        return [dict(r) for r in cur.fetchall()]

    def summary(self) -> Dict:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM signals")
        total = cur.fetchone()["n"]
        cur = self._conn.execute(
            """SELECT COUNT(*) AS n,
                      AVG(CASE WHEN ret > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                      AVG(ret) AS avg_ret
               FROM outcomes WHERE horizon_min = 240"""
        )
        row = cur.fetchone()
        return {
            "signals_total": total,
            "evaluated_4h": row["n"] or 0,
            "win_rate_4h": row["win_rate"],
            "avg_ret_4h": row["avg_ret"],
        }
