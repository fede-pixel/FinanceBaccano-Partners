#!/bin/bash
# macOS: doppio clic per avviare
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Prima installazione: preparo l'ambiente..."
  python3 -m venv .venv || { echo "Installa Python 3.11+ da https://www.python.org"; read; exit 1; }
fi
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python run.py
