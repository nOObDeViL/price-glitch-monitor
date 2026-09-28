#!/usr/bin/env bash
# One-shot installer for macOS / Linux: venv + dependencies + browser, then the setup wizard.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.9+ is required (https://www.python.org/downloads/)"; exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Python 3.9+ is required"; exit 1; }

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
# Bundled Chromium is the fallback; installed Google Chrome is preferred automatically.
.venv/bin/python -m playwright install chromium
if [ "$(uname)" = "Linux" ]; then .venv/bin/python -m playwright install-deps chromium || true; fi

echo
echo "✅ Installed. Launching setup..."
exec .venv/bin/python run.py --setup
