# Funding Edge — Studio descrittivo (Fase 1)

Studio **go/no-go** su una strategia cash-and-carry delta-neutral (long spot +
short perp) su Bybit per incassare il funding rate. **Fase 1 = solo dati e
metriche descrittive.** Nessuna logica di trading, nessuna dashboard, nessuna
ottimizzazione finche' i numeri non dicono che vale la pena.

Due file, con la tabella DuckDB come unica interfaccia:

- `collect.py` — raccolta dati (questo step).
- `analyze.py` — metriche descrittive e report go/no-go (step successivo, **non
  ancora scritto**).

## Setup (in locale, sul tuo Mac)

```bash
pip install -r funding/requirements.txt
```

> La raccolta interroga `https://api.bybit.com`. In locale non serve nulla; in un
> ambiente Claude Code on the web va aggiunto `api.bybit.com` all'allowlist di
> egress, altrimenti la rete e' bloccata.

## Raccolta

```bash
python funding/collect.py                       # universo predefinito, dal 2022
python funding/collect.py --symbols BTCUSDT ETHUSDT
python funding/collect.py --start 2022-01-01 --end 2025-01-01
python funding/collect.py --with-oi             # tenta anche l'open interest
python funding/collect.py --verify-only         # solo asserzioni, niente download
```

Caratteristiche:

- **Paginazione** completa dello storico funding (non solo l'ultima pagina).
- **Rate-limit/retry** con backoff (gestisce 429/418/403 e i retCode Bybit).
- **Idempotente / riprendibile**: upsert su `(symbol, funding_time)`; non
  riscarica le finestre gia' coperte ne' ri-arricchisce righe gia' con prezzo.
- **Niente fill silenzioso**: cio' che manca resta `NULL` ed e' segnalato.

### Endpoint Bybit usati

| dato | endpoint |
|---|---|
| funding rate storico | `GET /v5/market/funding/history` |
| mark price @ funding | `GET /v5/market/mark-price-kline` |
| spot price @ funding | `GET /v5/market/kline?category=spot` |
| open interest (opz.) | `GET /v5/market/open-interest` |

> Nota: `funding/history` restituisce **solo** rate + timestamp. `mark_price` e
> `spot_price` vengono ricostruiti agganciando l'**open** della kline (1h) il cui
> start coincide col `funding_time`. `volume_24h` e `open_interest` punto-nel-tempo
> non sono recuperabili in modo affidabile per il 2022 → restano `NULL`.

## Schema `funding_history`

| campo | tipo | note |
|---|---|---|
| `symbol` | VARCHAR | es. BTCUSDT |
| `funding_time` | TIMESTAMP | UTC, settlement (3/giorno con interval 8h) |
| `funding_rate` | DOUBLE | tasso del periodo |
| `mark_price` | DOUBLE | perp @ funding_time |
| `spot_price` | DOUBLE | spot @ funding_time |
| `basis` | DOUBLE | `mark_price - spot_price` |
| `volume_24h` | DOUBLE | opzionale (NULL di default) |
| `open_interest` | DOUBLE | opzionale (NULL / best-effort) |
| `ingested_at` | TIMESTAMP | audit |

PK: `(symbol, funding_time)`.

## Verifica

Alla fine di ogni run (o con `--verify-only`) viene stampata una tabella con,
per ogni symbol: **righe**, **range date**, **intervallo di funding derivato**,
**buchi** nella serie, **righe senza mark/spot**. I buchi vengono campionati ed
elencati. Vanno valutati prima di passare ad `analyze.py`.

Lo stesso contenuto viene salvato in **Markdown** in
`funding/reports/verification-latest.md` (disattivabile con `--no-report`,
percorso con `--report`).

## Lanciare e leggere dal telefono

Bybit blocca gli IP cloud (verificato: i runner GitHub ospitati danno 0 righe),
quindi la raccolta deve girare su una macchina non bloccata: **il tuo Mac**.

### Opzione consigliata: pulsante su GitHub → eseguito sul Mac
Registra il Mac come *self-hosted runner* (una volta): poi premi **Run workflow**
dall'app GitHub e il job gira sul Mac e committa il report.
Setup: vedi **`funding/SELF_HOSTED_RUNNER.md`**.

> App **GitHub** → **Actions** → **Funding report (Bybit)** → **Run workflow**
> poi leggi `funding/reports/verification-latest.md`

### Alternativa senza runner: un comando sul Mac
```bash
./funding/publish.sh            # esegue collect.py e PUSHA il report su GitHub
./funding/publish.sh --verify-only
```
Committa **solo** il report Markdown (il DuckDB resta locale). Lo leggi dallo
stesso file su GitHub. Lo stesso meccanismo servira' per il report go/no-go di
`analyze.py`.
