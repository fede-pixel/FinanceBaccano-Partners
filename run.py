"""Avvia l'app in locale e apre il browser."""
import threading
import webbrowser

import uvicorn

from app import config


def main() -> None:
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"\n  Finance & Admin attivo su {url}\n  Dati in: {config.DATA_DIR}\n  Per chiudere: CTRL+C\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
