# Lanciare la raccolta da GitHub, eseguita sul tuo Mac

Obiettivo: premere **Run workflow** dall'app GitHub (anche dall'iPhone) e far
girare la raccolta **sul tuo Mac** (che raggiunge Bybit, a differenza dei runner
cloud che sono geo-bloccati). Il report finisce su GitHub e lo leggi dal telefono.

Per farlo, il Mac va registrato **una volta sola** come *self-hosted runner*.
Il setup richiede il terminale solo adesso; dopo gira come **servizio in
background** e non lo tocchi piu': basta il pulsante su GitHub.

## Prerequisiti (una volta)
- Il Mac deve avere `python3` (verifica con `python3 --version`; se manca,
  installalo da python.org o con Homebrew).
- Il Mac deve essere **acceso e con il tuo utente loggato** quando premi il
  pulsante (il servizio gira a livello utente).

## 1) Registra il runner (copia-incolla dalla pagina GitHub)

1. Sul Mac apri questa pagina (browser):
   **https://github.com/Sashimidimedusa/leskere/settings/actions/runners/new?arch=arm64&os=osx**
   (se hai un Mac Intel cambia `arch=arm64` in `arch=x64`)
2. GitHub mostra una sequenza di comandi **gia' compilati con un token valido**.
   Copiali e incollali nel Terminale uno dopo l'altro. Hanno questa forma
   (NON copiare i miei: usa quelli della pagina, col token vero):

   ```bash
   mkdir -p ~/actions-runner && cd ~/actions-runner
   curl -o actions-runner-osx.tar.gz -L https://github.com/actions/runner/releases/download/vX.Y.Z/actions-runner-osx-arm64-X.Y.Z.tar.gz
   tar xzf actions-runner-osx.tar.gz
   ./config.sh --url https://github.com/Sashimidimedusa/leskere --token <TOKEN_DALLA_PAGINA>
   ```

   Durante `./config.sh` premi Invio a tutte le domande (nome, gruppo, **labels**:
   lascia il default, che include gia' `self-hosted` — il workflow usa quella).

## 2) Fai partire il runner come servizio (cosi' non serve piu' il terminale)

Sempre in `~/actions-runner`:

```bash
./svc.sh install
./svc.sh start
```

Da ora il runner parte da solo quando accendi il Mac e sei loggato. Per
controllare/fermare in futuro: `./svc.sh status` / `./svc.sh stop`.

Verifica: su GitHub, **Settings > Actions > Runners**, deve comparire il tuo Mac
come **Idle** (pallino verde).

## 3) Lancia dal telefono (uso quotidiano, niente terminale)

1. App **GitHub** → repo **leskere** → **Actions**
2. **Funding report (Bybit)** → **Run workflow**
   (lascia i campi vuoti per universo completo dal 2022, o specifica symbol/date)
3. Aspetta il pallino verde, poi apri il report:
   `github.com/Sashimidimedusa/leskere/blob/claude/bybit-funding-analysis-5b9dtt/funding/reports/verification-latest.md`

La prima raccolta richiede qualche minuto; le successive sono incrementali. Il
DuckDB resta sul Mac tra un run e l'altro (non viene caricato su GitHub).

## Note
- Se il run resta in coda ("Waiting for a runner"), il Mac e' spento / non
  loggato / il servizio e' fermo: accendi/logga il Mac o `./svc.sh start`.
- In alternativa, senza pulsante, resta sempre valido `./funding/publish.sh` dal
  Mac (un comando).
