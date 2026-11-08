#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[setup] creating virtual environment..."
  python3 -m venv .venv
  .venv/bin/pip install -r mvp/requirements.txt
fi

OPEN_BROWSER=1 .venv/bin/python mvp/run.py
