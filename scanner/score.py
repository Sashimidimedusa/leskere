"""Scoring composito e piano operativo.

Filosofia: niente segnale booleano. Ogni simbolo riceve un punteggio 0-100 e la
dashboard mostra una classifica. Un booleano ti costringe a scegliere una soglia
a priori; un punteggio la fa scegliere ai dati (vedi scanner/backtest.py, che
misura il rendimento forward per fascia di punteggio).

Ogni componente e' normalizzato in 0..1 ed e' calcolato NELLA DIREZIONE del
segnale: la stessa feature che vale 1 per un long vale 0 per uno short.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

from .features import Features
from .indicators import clamp, scale

# I pesi sommano a 1.0. Sono l'ipotesi di partenza, non una verita': il backtest
# serve proprio a rivedere questi numeri sui dati.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "volume": 0.24,       # volume relativo: senza volume il movimento non regge
    "breakout": 0.18,     # rottura di struttura recente
    "momentum": 0.16,     # spinta gia' in atto sulle 4h
    "rel_strength": 0.14,  # si muove da solo o segue solo BTC?
    "trend": 0.13,        # allineamento con il timeframe superiore
    "volatility": 0.08,   # ampiezza sufficiente ma non ingestibile
    "open_interest": 0.07,  # denaro nuovo che entra a confermare
}

# Banda di volatilita' utile (ATR 1h in % del prezzo) per un orizzonte 1-8h.
VOL_BAND_LOW = 0.004      # sotto: il movimento non paga spread e commissioni
VOL_BAND_HIGH = 0.025     # sopra: stop troppo largo, size troppo piccola


@dataclass
class TradePlan:
    entry: float
    stop: float
    target: float
    risk_reward: float
    risk_pct: float          # distanza dello stop in % del prezzo
    invalidation: str


@dataclass
class Opportunity:
    symbol: str
    direction: str                     # "long" | "short"
    score: float                       # 0..100
    price: float
    components: Dict[str, float] = field(default_factory=dict)   # contributi 0..1
    penalties: Dict[str, float] = field(default_factory=dict)    # fattori <= 1
    reasons: List[str] = field(default_factory=list)
    plan: Optional[TradePlan] = None
    features: Optional[Dict] = None

    def to_dict(self) -> Dict:
        data = asdict(self)
        return data


def infer_direction(f: Features) -> Optional[str]:
    """Voto di maggioranza tra segnali direzionali indipendenti.

    Se i voti si annullano il simbolo e' indeciso: meglio nessuna chiamata che
    una chiamata a caso (il rumore di range e' esattamente il problema che
    vogliamo evitare).
    """
    votes = 0
    votes += 2 * f.ema_align_1h                 # il contesto superiore pesa doppio
    votes += f.ema_align_15m

    if f.ret_4h is not None:
        votes += 1 if f.ret_4h > 0 else (-1 if f.ret_4h < 0 else 0)
    if f.breakout_atr:
        votes += 2 if f.breakout_atr > 0 else -2
    if f.vwap_dist_atr is not None:
        votes += 1 if f.vwap_dist_atr > 0 else -1

    if votes >= 2:
        return "long"
    if votes <= -2:
        return "short"
    return None


def _directional(value: Optional[float], direction: str) -> Optional[float]:
    """Riporta una grandezza con segno nel verso del segnale (long: invariata)."""
    if value is None:
        return None
    return value if direction == "long" else -value


def _volatility_component(atr_pct_1h: Optional[float]) -> float:
    """1.0 dentro la banda utile, decadimento lineare fuori.

    Non e' "piu' volatilita' = meglio": sotto la banda il movimento non copre i
    costi, sopra lo stop diventa cosi' largo da rendere la posizione inutile.
    """
    if atr_pct_1h is None:
        return 0.0
    if VOL_BAND_LOW <= atr_pct_1h <= VOL_BAND_HIGH:
        return 1.0
    if atr_pct_1h < VOL_BAND_LOW:
        return clamp(atr_pct_1h / VOL_BAND_LOW)
    return clamp(1 - (atr_pct_1h - VOL_BAND_HIGH) / (VOL_BAND_HIGH * 2))


def compute_components(f: Features, direction: str) -> Dict[str, float]:
    breakout = _directional(f.breakout_atr, direction)
    momentum = _directional(f.ret_4h, direction)
    rel = _directional(f.rs_btc_4h, direction)

    trend = 0.0
    if f.ema_align_1h == (1 if direction == "long" else -1):
        trend += 0.6
    if f.ema_align_15m == (1 if direction == "long" else -1):
        trend += 0.4

    return {
        # rvol 1 = volume nella norma, 4 = quattro volte il tipico
        "volume": scale(f.rvol_1h, 1.0, 4.0),
        # mezzo ATR oltre il massimo e' gia' una rottura credibile, 2 ATR e' netta
        "breakout": scale(breakout, 0.0, 2.0),
        "momentum": scale(momentum, 0.003, 0.05),
        "rel_strength": scale(rel, 0.0, 0.035),
        "trend": trend,
        "volatility": _volatility_component(f.atr_pct_1h),
        # OI in aumento = posizioni NUOVE, non solo chiusure altrui: conferma
        # il movimento in entrambe le direzioni
        "open_interest": scale(f.oi_change_1h, 0.0, 0.025),
    }


def compute_penalties(f: Features, direction: str) -> Dict[str, float]:
    """Fattori moltiplicativi <= 1 su costi e situazioni affollate."""
    penalties: Dict[str, float] = {}

    # Spread: 2 bp e' normale, oltre 15 bp l'entrata mangia una fetta del target.
    if f.spread_bp is not None:
        penalties["spread"] = clamp(1 - max(0.0, f.spread_bp - 2) / 25, 0.35, 1.0)

    # Estensione: entrare a 5 ATR dal VWAP significa comprare la fine del
    # movimento, che e' il modo classico di prendere il ritracciamento in faccia.
    dist = _directional(f.vwap_dist_atr, direction)
    if dist is not None:
        penalties["overextension"] = clamp(1 - max(0.0, dist - 3.5) / 5, 0.4, 1.0)

    # Funding: se paghi molto per stare nel verso in cui vuoi entrare, il trade
    # e' gia' affollato. 0.05%/8h e' gia' alto.
    funding = _directional(f.funding_rate, direction)
    if funding is not None:
        penalties["funding"] = clamp(1 - max(0.0, funding - 0.0005) / 0.002, 0.6, 1.0)

    return penalties


def _reasons(f: Features, comp: Dict[str, float], pen: Dict[str, float], direction: str) -> List[str]:
    """Motivi leggibili, ordinati per contributo: la dashboard deve dire PERCHE'."""
    out: List[str] = []
    ranked = sorted(comp.items(), key=lambda kv: kv[1] * DEFAULT_WEIGHTS.get(kv[0], 0), reverse=True)

    for name, value in ranked:
        if value < 0.35:
            continue
        if name == "volume" and f.rvol_1h:
            out.append(f"Volume {f.rvol_1h:.1f}x il tipico")
        elif name == "breakout" and f.breakout_atr:
            verso = "sopra il massimo" if direction == "long" else "sotto il minimo"
            out.append(f"Rottura {abs(f.breakout_atr):.1f} ATR {verso} 24h")
        elif name == "momentum" and f.ret_4h is not None:
            out.append(f"Spinta {f.ret_4h * 100:+.1f}% in 4h")
        elif name == "rel_strength" and f.rs_btc_4h is not None:
            out.append(f"Forza relativa vs BTC {f.rs_btc_4h * 100:+.1f}%")
        elif name == "trend":
            out.append("Trend allineato 15m + 1h" if value > 0.9 else "Trend allineato su 1h")
        elif name == "open_interest" and f.oi_change_1h is not None:
            out.append(f"Open interest {f.oi_change_1h * 100:+.1f}% in 1h")
        elif name == "volatility" and f.atr_pct_1h is not None:
            out.append(f"Volatilita' utile (ATR 1h {f.atr_pct_1h * 100:.1f}%)")

    for name, factor in pen.items():
        if factor >= 0.9:
            continue
        if name == "spread" and f.spread_bp is not None:
            out.append(f"Attenzione: spread {f.spread_bp:.1f} bp")
        elif name == "overextension":
            out.append("Attenzione: prezzo gia' molto esteso dal VWAP")
        elif name == "funding" and f.funding_rate is not None:
            out.append(f"Attenzione: funding {f.funding_rate * 100:.3f}% contro di te")

    return out[:5]


def build_plan(
    f: Features,
    direction: str,
    stop_atr_mult: float = 1.5,
    target_atr_mult: float = 3.0,
) -> Optional[TradePlan]:
    """Stop e target dimensionati sull'ATR 1h: la stessa distanza in % non ha lo
    stesso significato su un simbolo che oscilla lo 0.3% e su uno che fa il 4%."""
    atr_ref = f.atr_abs_1h
    if not atr_ref or atr_ref <= 0 or f.price <= 0:
        return None

    entry = f.price
    if direction == "long":
        stop = entry - stop_atr_mult * atr_ref
        target = entry + target_atr_mult * atr_ref
    else:
        stop = entry + stop_atr_mult * atr_ref
        target = entry - target_atr_mult * atr_ref

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    invalidation = (
        f"Chiusura 15m {'sotto' if direction == 'long' else 'sopra'} "
        f"{stop:.6g} annulla l'idea"
    )
    return TradePlan(
        entry=entry,
        stop=stop,
        target=target,
        risk_reward=abs(target - entry) / risk,
        risk_pct=risk / entry,
        invalidation=invalidation,
    )


def score_symbol(
    f: Features,
    weights: Optional[Dict[str, float]] = None,
    stop_atr_mult: float = 1.5,
    target_atr_mult: float = 3.0,
) -> Optional[Opportunity]:
    """Punteggio 0-100 per un simbolo. None se non c'e' una direzione chiara."""
    direction = infer_direction(f)
    if direction is None:
        return None

    weights = weights or DEFAULT_WEIGHTS
    components = compute_components(f, direction)
    penalties = compute_penalties(f, direction)

    total_weight = sum(weights.values()) or 1.0
    raw = sum(components.get(name, 0.0) * w for name, w in weights.items()) / total_weight

    factor = 1.0
    for value in penalties.values():
        factor *= value

    return Opportunity(
        symbol=f.symbol,
        direction=direction,
        score=round(100 * raw * factor, 1),
        price=f.price,
        components={k: round(v, 3) for k, v in components.items()},
        penalties={k: round(v, 3) for k, v in penalties.items()},
        reasons=_reasons(f, components, penalties, direction),
        plan=build_plan(f, direction, stop_atr_mult, target_atr_mult),
        features=f.to_dict(),
    )
