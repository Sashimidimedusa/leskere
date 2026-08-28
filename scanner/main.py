"""Entry point dello scanner.

    python3 -m scanner.main                     # scan continuo + dashboard
    python3 -m scanner.main --once              # un solo scan, stampa a video
    python3 -m scanner.main --no-server         # solo scan + Telegram, senza dashboard
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from typing import Optional

import yaml

from .bybit import BybitClient
from .engine import Scanner
from .notifier import TelegramNotifier
from .server import DashboardState, local_ip, make_server
from .store import Store
from .tracker import OutcomeTracker

DEFAULT_CONFIG = "scanner_config.yaml"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    tg = cfg.setdefault("telegram", {})
    # I segreti presi dall'ambiente vincono su quelli scritti nel file.
    tg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))
    return cfg


def print_table(result) -> None:
    if result.error_message:
        print(f"\n[errore] {result.error_message}\n")
        return
    print(
        f"\nScan {time.strftime('%H:%M:%S')} | universo {result.universe} | "
        f"analizzati {result.candidates} | in lista {len(result.opportunities)} | "
        f"{result.duration_s}s"
    )
    if not result.opportunities:
        print("  nessun candidato sopra la soglia.")
        return
    print(f"  {'#':>2}  {'SIMBOLO':<14}{'DIR':<7}{'SCORE':>6}  {'PREZZO':>12}"
          f"{'1h':>8}{'4h':>8}{'RVOL':>7}{'ATR%':>7}   MOTIVI")
    for i, o in enumerate(result.opportunities[:20], 1):
        f = o.features or {}
        def p(v, d=1):
            return "—" if v is None else f"{v * 100:.{d}f}%"
        print(
            f"  {i:>2}  {o.symbol:<14}{o.direction.upper():<7}{o.score:>6.0f}  "
            f"{o.price:>12.6g}{p(f.get('ret_1h')):>8}{p(f.get('ret_4h')):>8}"
            f"{(f.get('rvol_1h') or 0):>6.1f}x{p(f.get('atr_pct_1h'), 2):>7}   "
            f"{'; '.join(o.reasons[:2])}"
        )
    print()


def scan_loop(scanner: Scanner, notifier: Optional[TelegramNotifier], cfg: dict, stop_event: threading.Event) -> None:
    interval = cfg.get("scan_interval_seconds", 90)
    alert_score = cfg.get("alert_score", 72)
    alerted: dict = {}
    cooldown_ms = cfg.get("cooldown_minutes", 120) * 60_000

    while not stop_event.is_set():
        try:
            result = scanner.scan()
            print_table(result)

            if notifier is not None:
                now_ms = result.ts
                for opp in result.opportunities:
                    if opp.score < alert_score or opp.plan is None:
                        continue
                    key = (opp.symbol, opp.direction)
                    if now_ms - alerted.get(key, 0) < cooldown_ms:
                        continue
                    notifier.send(opp)
                    alerted[key] = now_ms
        except Exception as exc:                 # il loop non deve morire mai
            print(f"[scan] errore: {exc}", flush=True)
        stop_event.wait(interval)


def tracker_loop(tracker: OutcomeTracker, cfg: dict, stop_event: threading.Event) -> None:
    interval = cfg.get("tracker_interval_seconds", 300)
    while not stop_event.is_set():
        try:
            n = tracker.run_once()
            if n:
                print(f"[tracker] esiti registrati: {n}", flush=True)
        except Exception as exc:
            print(f"[tracker] errore: {exc}", flush=True)
        stop_event.wait(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scanner opportunita' intraday (Bybit)")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="un solo scan e poi esci")
    parser.add_argument("--no-server", action="store_true", help="non avviare la dashboard")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.port:
        cfg["port"] = args.port

    client = BybitClient(
        category=cfg.get("category", "linear"),
        requests_per_second=cfg.get("requests_per_second", 10.0),
    )
    store = Store(cfg.get("db_path", "scanner/data/scanner.db"))
    scanner = Scanner(cfg, client=client, store=store)

    if args.once:
        print_table(scanner.scan())
        store.close()
        return

    tg = cfg.get("telegram", {})
    notifier = TelegramNotifier(
        bot_token=tg.get("bot_token", ""),
        chat_id=tg.get("chat_id", ""),
        enabled=tg.get("enabled", False),
    )

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=scan_loop, args=(scanner, notifier, cfg, stop_event), daemon=True),
        threading.Thread(target=tracker_loop, args=(OutcomeTracker(client, store), cfg, stop_event), daemon=True),
    ]
    for t in threads:
        t.start()

    if args.no_server:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        stop_event.set()
        store.close()
        return

    host = cfg.get("host", "0.0.0.0")
    port = cfg.get("port", 8787)
    server = make_server(DashboardState(scanner, store, cfg), host, port)
    print(f"\nDashboard:  http://localhost:{port}")
    if host == "0.0.0.0":
        print(f"Da iPhone:  http://{local_ip()}:{port}   (stessa rete wifi)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiusura...")
    finally:
        stop_event.set()
        server.shutdown()
        store.close()


if __name__ == "__main__":
    main()
