"""Entry point del trading bot EMA multi-timeframe per Bybit.

Avvio:  python -m bot.main --config config.yaml
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Dict, Set, Tuple

import yaml

from .bybit_client import BybitClient
from .notifier import Notifier
from .strategy import StrategyEngine


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    # Le variabili d'ambiente hanno priorita' per i segreti Telegram.
    tg = cfg.setdefault("telegram", {})
    tg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))
    return cfg


def run(cfg: dict) -> None:
    client = BybitClient(category=cfg.get("category", "linear"))
    engine = StrategyEngine(
        client=client,
        timeframes=cfg["timeframes"],
        ema_period=cfg["ema_period"],
        consecutive_closes=cfg["consecutive_closes"],
        direction=cfg.get("direction", "long"),
    )
    notifier = Notifier(
        leverage=cfg.get("leverage", 1),
        bot_token=cfg["telegram"]["bot_token"],
        chat_id=cfg["telegram"]["chat_id"],
    )

    poll = cfg.get("poll_seconds", 15)
    symbols = cfg["symbols"]

    print(
        f"Bot avviato | simboli={symbols} | tf={cfg['timeframes']}m | "
        f"EMA{cfg['ema_period']} | {cfg['consecutive_closes']} chiusure consecutive | "
        f"direction={cfg.get('direction', 'long')} | poll={poll}s",
        flush=True,
    )

    # Stato attivo per evitare avvisi ripetuti: emette solo sul fronte di salita.
    active: Set[Tuple[str, str]] = set()

    while True:
        try:
            seen_now: Set[Tuple[str, str]] = set()
            for symbol in symbols:
                signals = engine.evaluate_symbol(symbol)
                for sig in signals:
                    key = (sig.symbol, sig.side)
                    seen_now.add(key)
                    if key not in active:
                        notifier.send(sig)
                        active.add(key)

            # Reset delle condizioni non piu' valide -> potranno riattivarsi.
            active = active & seen_now
        except Exception as exc:  # il loop non deve morire per un errore di rete
            print(f"[errore] {exc}", flush=True)

        time.sleep(poll)


def main() -> None:
    parser = argparse.ArgumentParser(description="EMA multi-timeframe signal bot (Bybit)")
    parser.add_argument("--config", default="config.yaml", help="percorso del file di configurazione")
    args = parser.parse_args()
    run(load_config(args.config))


if __name__ == "__main__":
    main()
