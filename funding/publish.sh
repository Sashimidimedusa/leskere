#!/usr/bin/env bash
#
# Esegui la raccolta sul Mac e PUBBLICA il report su GitHub, cosi' lo leggi
# dall'iPhone (app GitHub o browser) senza eseguire nulla sul telefono.
#
# Il file DuckDB resta locale (gitignored): viene committato SOLO il report
# Markdown in funding/reports/. Passa eventuali argomenti a collect.py.
#
# Esempi:
#   ./funding/publish.sh
#   ./funding/publish.sh --symbols BTCUSDT ETHUSDT --start 2022-01-01
#   ./funding/publish.sh --verify-only
#
set -euo pipefail

cd "$(dirname "$0")/.."   # radice del repo

BRANCH="claude/bybit-funding-analysis-5b9dtt"
REPORT="funding/reports/verification-latest.md"

echo ">> Raccolta + verifica (collect.py)"
python funding/collect.py "$@" || true   # un exit!=0 (es. buchi) non blocca la pubblicazione

if [[ ! -f "$REPORT" ]]; then
  echo "!! Report non trovato ($REPORT): niente da pubblicare." >&2
  exit 1
fi

echo ">> Pubblico il report su $BRANCH"
git add "$REPORT"
if git diff --cached --quiet; then
  echo "   Nessuna modifica al report: niente da committare."
  exit 0
fi

git commit -m "Report verifica raccolta funding ($(date -u '+%Y-%m-%d %H:%M UTC'))"

for attempt in 1 2 3 4; do
  if git push -u origin "$BRANCH"; then
    echo ">> Fatto. Apri il report dall'iPhone:"
    echo "   GitHub > leskere > branch $BRANCH > $REPORT"
    exit 0
  fi
  wait=$((2 ** attempt))
  echo "   push fallito, ritento fra ${wait}s..." >&2
  sleep "$wait"
done

echo "!! push fallito dopo i retry." >&2
exit 1
