@echo off
REM Windows: doppio clic per avviare
cd /d "%~dp0"
if not exist .venv (
  echo Prima installazione: preparo l'ambiente...
  py -3 -m venv .venv || python -m venv .venv || (echo Installa Python 3.11+ da https://www.python.org & pause & exit /b 1)
)
.venv\Scripts\pip install -q -r requirements.txt
.venv\Scripts\python run.py
pause
