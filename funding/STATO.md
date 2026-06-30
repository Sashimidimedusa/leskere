# Funding Edge — Stato del progetto (nota di passaggio)

> Per il "me" locale sul Mac: leggi questo file per allinearti. La rete verso
> Bybit qui FUNZIONA (a differenza dell'ambiente cloud dove è bloccata).

## Obiettivo (Fase 1 — solo descrittivo)
Decidere go/no-go su una strategia **cash-and-carry delta-neutral** su perpetual
Bybit (long spot + short perp per incassare il funding). **Niente logica di
trading/dashboard/ottimizzazione** finché i numeri non dicono che vale la pena.

## Regole dure dell'utente
1. Solo: raccolta dati → verifica → metriche descrittive → report go/no-go.
2. Ogni step verificabile: righe per symbol, range date, **buchi** espliciti.
3. **Nessun riempimento silenzioso** dei dati mancanti.
4. Costi modellati in chiaro: mostrare funding **lordo E netto** affiancati.
5. Mediana oltre alla media (la media è gonfiata dagli spike).
6. Benchmark USDT: **non inventarlo**, lo fornisce l'utente fresco.

## Cosa c'è già
- `funding/collect.py` — raccolta Bybit v5 (`/v5/market/funding/history` +
  mark/spot da kline), DuckDB, paginazione, retry/backoff, idempotente,
  verifica (righe/range/intervallo/buchi), report MD+HTML, preflight anti-block.
- `funding/publish.sh` — esegue collect e pubblica il report (uso da Mac).
- `Funding Report.html` — visualizzatore stand-alone: scarica il funding da
  Bybit nel browser e calcola funding annualizzato (mediano + medio realizzato),
  % periodi positivi/negativi, funding peggiore, costi, PASS/FAIL su benchmark.
- (Storico) workflow GitHub Actions: accantonato, i runner cloud sono geo-bloccati.

## Risultato reale già visto (dal visualizzatore web)
- Connessione Bybit dal browser: OK. Dati 2022→2026, ~4926 righe/symbol, 0 buchi.
- ATTENZIONE: per molti alt il **funding mediano = 0,01%/8h** (default Bybit) ≈
  **10,95%/anno**. La mediana da sola illude: va guardato il **medio realizzato**
  e la **distribuzione dei negativi**.

## Prossimi passi (in ordine)
1. **Basis risk + drawdown** del delta-neutral (`funding + Δbasis`): manca.
   Richiede di scaricare anche mark e spot price agli istanti di funding
   (collect.py lo fa già; il visualizzatore web no). Questo è il pezzo che il
   brief mette al centro per il vero go/no-go.
2. Inserire il **benchmark USDT** corrente (dato dall'utente) + margine
   rischio-coda → flag PASS/FAIL definitivo.
3. Solo dopo, se PASS, l'utente deciderà se proseguire oltre la Fase 1.

## Come eseguire in locale
```
pip install -r funding/requirements.txt
python funding/collect.py            # raccolta reale + verifica
```
Branch di lavoro: `claude/bybit-funding-analysis-5b9dtt`.
