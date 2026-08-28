# Scanner opportunità intraday — Bybit perpetual

Scanner che ordina **tutto** il mercato dei perpetual USDT di Bybit e ti mostra,
in una dashboard, i candidati con la maggiore probabilità di muoversi nelle
prossime **1-8 ore**, ognuno con il suo piano operativo.

> Lo scanner **non invia ordini** e non tocca il conto: usa solo dati pubblici,
> nessuna chiave API. Le decisioni restano tue.

---

## Perché è costruito così

**Non è un segnale booleano.** Ogni simbolo prende un punteggio 0-100 e la
dashboard è una classifica. Un booleano ti obbliga a fissare una soglia a
priori; un punteggio lascia che sia il backtest a dirti dove sta la soglia utile.

**Guarda tutto il mercato, non una watchlist.** Le occasioni intraday nascono
sull'alt che *oggi* ha volume anomalo, non sui soliti tre simboli maggiori.

**Misura se stesso.** Ogni segnale sopra soglia finisce in SQLite; un tracker
ripassa dopo 1h/4h/8h e registra cosa è successo davvero. La scheda
*Performance* confronta le fasce di punteggio: se le fasce alte non battono le
basse, lo score non sta discriminando e i pesi vanno rivisti. È l'unica
differenza tra "sembra funzionare" e "funziona".

---

## L'imbuto a due stadi

Scansionare ~400 simboli × 3 timeframe significherebbe ~1200 richieste per
ciclo. Invece:

| Stadio | Costo | Cosa fa |
|---|---|---|
| 1 — screening | **1 richiesta** | `tickers` restituisce prezzo, variazione 1h e 24h, range giornaliero, volume, funding e open interest di *tutti* i simboli. Filtro di liquidità + pre-punteggio, tengono i primi `deep_scan_top`. |
| 2 — approfondimento | ~2-3 richieste per candidato | Candele 15m e 1h + storico open interest solo per i sopravvissuti; feature complete e punteggio finale. |

Le candele chiuse vengono messe in cache fino alla chiusura della barra
successiva: dentro la stessa candela i cicli successivi sono quasi gratis.

## Le feature che compongono il punteggio

| Componente | Peso | Cosa cattura |
|---|---|---|
| `volume` | 0.24 | RVOL: volume dell'ultima ora contro la **mediana** delle stesse finestre nelle 24h precedenti. Mediana e non media, così una pump di ieri non nasconde lo scatto di oggi. |
| `breakout` | 0.18 | Quanto il prezzo sporge oltre il massimo/minimo delle 24h, misurato **in ATR**. Le ultime 2 barre sono escluse dal calcolo del massimo, altrimenti il massimo conterrebbe la spinta stessa. |
| `momentum` | 0.16 | Spinta già in atto sulle 4h. |
| `rel_strength` | 0.14 | Rendimento 4h **meno** quello di BTC: distingue chi si muove da solo da chi segue soltanto il mercato. |
| `trend` | 0.13 | Allineamento prezzo/EMA20/EMA50 su 15m e 1h (il timeframe superiore pesa di più). |
| `volatility` | 0.08 | Premia la banda utile di ATR (0.4%-2.5% su 1h): sotto non copre i costi, sopra lo stop diventa ingestibile. Non è "più volatilità è meglio". |
| `open_interest` | 0.07 | OI in aumento = posizioni **nuove**, non solo chiusure altrui: conferma il movimento in entrambe le direzioni. |

E tre **penalità** moltiplicative: spread largo, prezzo già troppo esteso dal
VWAP (entrare a 5 ATR di distanza significa comprare la fine del movimento),
funding alto contro la direzione (trade già affollato).

La **direzione** nasce da un voto di maggioranza tra segnali indipendenti. Se i
voti si annullano non viene emessa alcuna chiamata: il rumore di range è
esattamente il problema che vogliamo evitare.

Il **piano operativo** è dimensionato sull'ATR 1h — stop a 1.5 ATR, target a
3 ATR, R:R 2 — perché la stessa distanza percentuale non significa la stessa
cosa su un simbolo che oscilla lo 0.3% e su uno che fa il 4%.

---

## Uso

```bash
pip install -r requirements.txt

python3 -m scanner.main              # scan continuo + dashboard
python3 -m scanner.main --once       # un solo scan, stampato a video
python3 -m scanner.main --no-server  # solo scan + notifiche, niente dashboard
```

All'avvio la console stampa i due indirizzi:

```
Dashboard:  http://localhost:8787
Da iPhone:  http://192.168.1.x:8787   (stessa rete wifi)
```

La dashboard ha tre schede: **Opportunità** (la classifica live, si aggiorna da
sola ogni 10s — tocca una riga per vedere *perché* è in lista e il piano
completo), **Segnali registrati** e **Performance**.

### Telegram (opzionale)

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"
```

Poi `telegram.enabled: true` in `scanner_config.yaml`. Le notifiche partono solo
sopra `alert_score`, con cooldown per simbolo/direzione.

---

## Prima di operarci: il backtest

```bash
python3 -m scanner.backtest --days 30 --top 40
python3 -m scanner.backtest --days 30 --offline   # riusa la cache su disco
```

Ricalcola lo score su finestre passate (**solo barre già chiuse**: nessuna
informazione dal futuro) e misura il rendimento a 1h/4h/8h per fascia di
punteggio. In fondo stampa il verdetto: se la fascia ≥60 non batte la fascia
<40, lo score non discrimina.

**Limiti dichiarati:** open interest, funding e spread storici non sono
ricostruiti (quelle componenti vengono neutralizzate e i pesi rinormalizzati,
quindi lo score del backtest non è identico a quello live); i rendimenti sono
lordi, senza commissioni né slippage; l'universo è scelto sulla liquidità di
oggi, quindi c'è un po' di survivorship bias.

---

## Configurazione

Tutto in `scanner_config.yaml`, commentato riga per riga. I parametri che
cambiano di più il comportamento:

| Parametro | Effetto |
|---|---|
| `min_turnover_24h` | Soglia di liquidità dell'universo. Alzarla riduce le trappole illiquide. |
| `deep_scan_top` | Quanti candidati passano allo stadio 2: copertura contro chiamate API. |
| `min_display_score` | Sotto questo punteggio non compare in dashboard. |
| `signal_score` | Sopra questo il segnale viene **registrato** per la verifica. |
| `alert_score` | Sopra questo parte la notifica Telegram. |
| `weights` | I pesi delle componenti. Rivedili **dopo** il backtest, non prima. |
| `stop_atr_mult` / `target_atr_mult` | Geometria del piano operativo. |

---

## Rete

Lo scanner interroga `api.bybit.com`. **I runner cloud di GitHub e gli ambienti
Claude Code on the web sono geo-bloccati da Bybit** (rispondono 403): va
eseguito da una macchina che raggiunge l'API — nel nostro caso il Mac, lo stesso
che ospita il self-hosted runner. Se un ciclo riceve 403 la dashboard lo mostra
in chiaro invece di restare silenziosamente vuota.

## Test

```bash
python3 -m unittest tests.test_scanner -v
```

43 test, nessuna rete: candele sintetiche deterministiche. Coprono indicatori,
esclusione della candela in formazione, feature, direzione, penalità, geometria
del piano, verifica degli esiti (inclusa la regola conservativa sulla barra che
tocca sia stop sia target), registro SQLite e assenza di look-ahead nel backtest.

## Disclaimer

Strumento informativo/educativo. Il trading in leva su cripto comporta un
rischio elevato di perdita del capitale. Nessun punteggio è garanzia di profitto.
