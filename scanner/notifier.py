"""Notifiche Telegram per i segnali sopra soglia (opzionale)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import requests

from .score import Opportunity


def format_opportunity(opp: Opportunity) -> str:
    icon = "🟢 LONG" if opp.direction == "long" else "🔴 SHORT"
    lines = [f"{icon}  {opp.symbol}   score {opp.score:.0f}/100", f"Prezzo: {opp.price:.6g}"]

    if opp.plan:
        p = opp.plan
        lines += [
            f"Entry {p.entry:.6g} | Stop {p.stop:.6g} ({p.risk_pct * 100:.2f}%) | Target {p.target:.6g}",
            f"R:R {p.risk_reward:.1f}",
        ]
    if opp.reasons:
        lines.append("Perche':")
        lines += [f"  • {r}" for r in opp.reasons]
    lines.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", enabled: bool = True):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.bot_token and self.chat_id)

    def send(self, opp: Opportunity) -> None:
        text = format_opportunity(opp)
        print("\n" + "-" * 56 + f"\n{text}\n" + "-" * 56, flush=True)
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
        except requests.RequestException as exc:      # una notifica persa non ferma lo scanner
            print(f"[telegram] invio fallito: {exc}", flush=True)
