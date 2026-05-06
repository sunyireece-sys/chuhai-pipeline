#!/usr/bin/env bash
# One-shot installer: venv + Python deps + Playwright Chromium.
# Safe to re-run (pip / playwright install handle idempotency).
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "→ creating .venv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ upgrading pip"
pip install --quiet --upgrade pip

echo "→ installing Python dependencies"
pip install --quiet -r requirements.txt

echo "→ installing Playwright Chromium (first time only; ~200MB)"
playwright install chromium

echo
echo "✅ setup done."
echo "Next:"
echo "  1. cp .env.example .env     # then paste your SERPER_API_KEY"
echo "  2. source .venv/bin/activate"
echo "  3. python pipeline.py runs/<your-run-dir>/01_keywords.md"
