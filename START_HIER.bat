@echo off
setlocal
cd /d "%~dp0"
title VideoLibre v8 - Lokal starten
color 0A

if not exist ".venv\Scripts\python.exe" (
  echo VideoLibre ist noch nicht eingerichtet.
  echo Bitte zuerst EINRICHTEN.bat doppelt anklicken.
  echo.
  pause
  exit /b 1
)

.venv\Scripts\python.exe run_local.py
if errorlevel 1 (
  echo.
  echo VideoLibre konnte nicht gestartet werden.
  echo Lies die Fehlermeldung oben oder bgutil-provider.log.
  echo.
  pause
)
