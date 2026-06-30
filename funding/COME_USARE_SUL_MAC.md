# Come usare "Funding Report" sul Mac (semplice)

Niente GitHub, niente comandi. Doppio click su un'icona → il Mac scarica i dati e
ti apre il report nel browser.

## Prima volta (5 minuti)

1. **Scarica il progetto** aprendo questo link su Safari (parte un download `.zip`):
   https://github.com/Sashimidimedusa/leskere/archive/refs/heads/claude/bybit-funding-analysis-5b9dtt.zip
2. Nella cartella **Download**, fai **doppio click sul file .zip**: si crea una
   cartella (es. `leskere-claude-bybit-funding-analysis-5b9dtt`).
3. Aprila: dentro trovi l'icona **Funding Report**.
4. **Solo la prima volta**, per via della protezione di macOS:
   **click destro sull'icona → Apri → Apri**. (Se fai doppio click normale la
   prima volta, macOS la blocca: usa il click destro.)

## Tutte le volte

- **Doppio click su "Funding Report".**
- Compaiono delle notifiche ("Preparo l'ambiente", "Scarico i dati...").
  La prima raccolta richiede qualche minuto (scarica anche il 2022).
- Alla fine si apre da solo il **report nel browser**: una tabella con, per ogni
  asset, righe scaricate, periodo, e eventuali problemi (buchi, prezzi mancanti).

## Se qualcosa non va

- Se dice che **manca Python**: si apre la pagina per installarlo. Installa, poi
  riapri Funding Report.
- Per i dettagli tecnici di un errore: nella cartella del progetto, file
  `funding/last-run.log`.

> Serve connessione a internet. I dati restano sul tuo Mac (file
> `funding/data/funding.duckdb`); non viene caricato nulla online.
