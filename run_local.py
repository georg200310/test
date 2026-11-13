from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
PROVIDER_SERVER = ROOT / "bgutil-ytdlp-pot-provider" / "server"
PROVIDER_ENTRY = PROVIDER_SERVER / "build" / "main.js"
PROVIDER_HOST = "127.0.0.1"
PROVIDER_PORT = 4416
APP_HOST = "127.0.0.1"
APP_PORT = 10000


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_for_port(process: subprocess.Popen, host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def open_browser_when_ready() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if port_open(APP_HOST, APP_PORT):
            webbrowser.open(f"http://{APP_HOST}:{APP_PORT}")
            return
        time.sleep(0.4)


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("FEHLER: Node.js 22 oder neuer wurde nicht gefunden.")
        print("Installiere Node.js LTS und starte danach START_HIER.bat erneut.")
        return 1
    if not PROVIDER_ENTRY.is_file():
        print("FEHLER: Der lokale PO-Token-Dienst wurde nicht eingerichtet.")
        print("Bitte zuerst EINRICHTEN.bat ausführen.")
        return 1
    reuse_provider = port_open(PROVIDER_HOST, PROVIDER_PORT)
    if reuse_provider:
        print(f"Auf Port {PROVIDER_PORT} läuft bereits ein Dienst; VideoLibre versucht, ihn als BgUtils wiederzuverwenden.")
    if port_open(APP_HOST, APP_PORT):
        print(f"FEHLER: Port {APP_PORT} wird bereits verwendet.")
        print("Schließe das andere Programm oder ändere APP_PORT in run_local.py.")
        return 1

    log_path = ROOT / "bgutil-provider.log"
    log_file = log_path.open("w", encoding="utf-8", errors="replace")
    provider: subprocess.Popen | None = None
    try:
        if not reuse_provider:
            print("Starte lokalen PO-Token-Dienst ...")
            provider = subprocess.Popen(
                [node, str(PROVIDER_ENTRY), "--port", str(PROVIDER_PORT)],
                cwd=str(PROVIDER_SERVER),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if not wait_for_port(provider, PROVIDER_HOST, PROVIDER_PORT):
                print("FEHLER: Der PO-Token-Dienst konnte nicht gestartet werden.")
                print(f"Details stehen in: {log_path.name}")
                return 1

        print("Starte VideoLibre lokal unter http://127.0.0.1:10000")
        print("Nur dein eigener PC kann diese lokale Adresse erreichen.")
        print("Zum Beenden dieses Fenster schließen oder STRG+C drücken.\n")
        threading.Thread(target=open_browser_when_ready, daemon=True).start()
        uvicorn.run("app:app", host=APP_HOST, port=APP_PORT, log_level="warning", access_log=False)
        return 0
    except KeyboardInterrupt:
        print("\nVideoLibre wird beendet ...")
        return 0
    finally:
        if provider and provider.poll() is None:
            provider.terminate()
            try:
                provider.wait(timeout=5)
            except subprocess.TimeoutExpired:
                provider.kill()
                provider.wait(timeout=3)
        log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
