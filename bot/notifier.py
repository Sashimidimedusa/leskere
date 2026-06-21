"""Invio delle chiamate: console e (opzionale) Telegram."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests

from .strategy import Signal


def format_signal(signal: Signal, leverage: int) -> str:
    arrow = "🟢 LONG" if signal.side == "long" else "🔴 SHORT"
    side_word = "sopra" if signal.side == "long" else "sotto"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"{arrow}  {signal.symbol}  (leva {leverage}x)",
        f"Prezzo: {signal.price}",
        f"Condizione: chiusure consecutive {side_word} EMA su tutti i time frame",
    ]
    for tf in signal.per_timeframe:
        lines.append(
            f"  - {tf.timeframe}m: {tf.consecutive} chiusure "
            f"(close={tf.last_close} / ema={round(tf.last_ema, 4)})"
        )
    lines.append(f"Orario: {ts}")
    return "\n".join(lines)


class Notifier:
    def __init__(self, leverage: int, bot_token: str = "", chat_id: str = ""):
        self.leverage = leverage
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, signal: Signal) -> None:
        text = format_signal(signal, self.leverage)
        print("\n" + "=" * 50)
        print(text)
        print("=" * 50 + "\n", flush=True)
        if self.bot_token and self.chat_id:
            self._send_telegram(text)

    def _send_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
        except requests.RequestException as exc:  # non bloccare il bot
            print(f"[telegram] invio fallito: {exc}", flush=True)
