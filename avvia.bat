@echo off
setlocal
REM Windows: doppio clic per avviare Finance & Admin
cd /d "%~dp0"
title Finance ^& Admin - Baccano Partners

if exist ".venv\Scripts\python.exe" goto install

echo.
echo  Prima installazione: cerco Python 3.11 o successivo...
set "PY="
REM "<nul" evita che il Python Install Manager resti in attesa di una risposta
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" <nul >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" <nul >nul 2>&1 && set "PY=python"
if not defined PY goto nopython

echo  Trovato. Preparo l'ambiente (1-2 minuti, solo la prima volta)...
%PY% -m venv .venv <nul
if errorlevel 1 goto nopython

:install
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo  Installazione dei componenti non riuscita: controlla la connessione a internet e riprova.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
pause
exit /b 0

:nopython
echo.
echo  ----------------------------------------------------------------
echo   Python non e' ancora installato (o e' una versione troppo vecchia).
echo.
echo   Se hai installato il "Python Install Manager", apri il Prompt dei
echo   comandi e scrivi:
echo.
echo       py install 3.13
echo.
echo   poi rispondi "y" alle domande e, finito, fai di nuovo doppio clic
echo   su avvia.bat.
echo.
echo   In alternativa scarica "Python 3.13" da https://www.python.org/downloads/
echo   e durante l'installazione spunta "Add python.exe to PATH".
echo  ----------------------------------------------------------------
echo.
pause
exit /b 1
