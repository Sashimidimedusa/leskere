# Repo trading — Bybit

Due strumenti distinti:

| Cartella | Cosa fa |
|---|---|
| **[`scanner/`](scanner/README.md)** | **Scanner di opportunità intraday (1-8h)**: ordina tutto il mercato dei perpetual USDT per punteggio, con dashboard web, registro dei segnali e verifica automatica degli esiti. È lo strumento principale. |
| `bot/` | Bot storico a segnale singolo: confluenza EMA su più time frame, documentato qui sotto. |

---

# EMA Multi-Timeframe Signal Bot (Bybit)

Bot che genera **chiamate (segnali)** su asset cripto perpetual di **Bybit** pensati
per il trading in leva. Una chiamata nasce quando il prezzo ha chiuso **N candele
consecutive sopra la EMA** in **confluenza su più time frame** (default: 1m, 3m, 5m).

> ⚠️ Il bot **non invia ordini** e non tocca il tuo conto: produce solo avvisi
> (console e, opzionalmente, Telegram). Le decisioni di trading restano tue.
> Nessuna chiave API è necessaria: usa solo i dati pubblici di mercato.

## Strategia

Un segnale **LONG** viene emesso quando, su **ogni** time frame configurato:

- la EMA del periodo scelto (default `EMA10`) è calcolata sulle candele chiuse;
- le ultime `consecutive_closes` candele (default 3) hanno **chiuso sopra** la EMA.

Con `direction: short` la condizione è simmetrica (chiusure **sotto** la EMA);
con `direction: both` vengono valutate entrambe.

Il segnale viene emesso **una sola volta** quando la condizione si attiva
(fronte di salita): non viene ripetuto a ogni ciclo finché la condizione resta
valida, e può riattivarsi dopo che si è interrotta.

> Nota: vengono usate solo le candele **già chiuse**; quella ancora in formazione
> viene esclusa, così "chiusura sopra la EMA" è una chiusura reale e non un valore
> intra-candela che può ancora cambiare.

## Installazione

```bash
pip install -r requirements.txt
```

## Configurazione

Modifica `config.yaml`:

| Parametro | Significato | Default |
|---|---|---|
| `symbols` | Simboli perpetual USDT da monitorare | BTCUSDT, ETHUSDT, SOLUSDT |
| `timeframes` | Time frame in minuti da mettere in confluenza | 1, 3, 5 |
| `ema_period` | Periodo della EMA | 10 |
| `consecutive_closes` | Chiusure consecutive richieste su ogni time frame | 3 |
| `direction` | `long`, `short` oppure `both` | long |
| `poll_seconds` | Intervallo tra i controlli | 15 |
| `leverage` | Leva mostrata nel testo della chiamata (solo informativa) | 10 |
| `category` | `linear` (USDT perp), `inverse`, `spot` | linear |
| `telegram` | Token/chat per le notifiche Telegram (opzionale) | vuoto |

### Telegram (opzionale)

Per ricevere le chiamate su Telegram, crea un bot con
[@BotFather](https://t.me/BotFather) e imposta:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"
```

(le variabili d'ambiente hanno priorità sui valori in `config.yaml`).

## Avvio

```bash
python -m bot.main --config config.yaml
```

Esempio di chiamata stampata:

```
🟢 LONG  BTCUSDT  (leva 10x)
Prezzo: 65250.5
Condizione: chiusure consecutive sopra EMA su tutti i time frame
  - 1m: 4 chiusure (close=65250.5 / ema=65180.2)
  - 3m: 3 chiusure (close=65250.5 / ema=65120.7)
  - 5m: 5 chiusure (close=65250.5 / ema=65010.1)
Orario: 2026-06-21 12:34:56 UTC
```

## Test

```bash
python3 tests/test_indicators.py
```

## Accesso di rete (Claude Code on the web)

Il bot interroga `https://api.bybit.com`. Se lo esegui dentro un ambiente
Claude Code on the web con allowlist di rete, **aggiungi `api.bybit.com`** (ed
eventualmente `api.telegram.org`) alle impostazioni di egress dell'ambiente,
altrimenti riceverai un errore `403 Host not in allowlist`. In locale, sulla tua
macchina, non serve alcuna configurazione. Vedi:
https://code.claude.com/docs/en/claude-code-on-the-web

## Disclaimer

Strumento a scopo informativo/educativo. Il trading con leva su cripto comporta
un rischio elevato di perdita del capitale. Nessun segnale è garanzia di profitto.
