"""Scanner di opportunita' di trading intraday sui perpetual Bybit.

Pipeline a due stadi:
  1. una sola chiamata `tickers` copre tutto il mercato -> filtro liquidita' + pre-score
  2. candele e open interest solo per i candidati sopravvissuti allo stadio 1

Vedi scanner/README.md per l'architettura completa.
"""

__all__ = ["bybit", "indicators", "features", "score", "store", "engine"]
