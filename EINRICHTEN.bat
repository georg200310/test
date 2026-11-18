@echo off
setlocal
cd /d "%~dp0"
title VideoLibre v10 - Ersteinrichtung
color 0B

echo ================================================
echo       VIDEOLIBRE - ERSTEINRICHTUNG
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo FEHLER: Python wurde nicht gefunden.
  echo Installiere Python 3.11 oder neuer:
  echo https://www.python.org/downloads/windows/
  echo Beim Setup unbedingt "Add Python to PATH" aktivieren.
  echo.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo FEHLER: VideoLibre benoetigt Python 3.11 oder neuer.
  echo Installiere eine aktuelle Python-Version von https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo FEHLER: Node.js wurde nicht gefunden.
  echo Installiere Node.js 22 oder neuer:
  echo https://nodejs.org/en/download/
  echo Danach dieses Fenster schliessen und EINRICHTEN.bat erneut starten.
  echo.
  pause
  exit /b 1
)
node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)"
if errorlevel 1 (
  echo FEHLER: Node.js 22 oder neuer ist erforderlich.
  echo Installiere die aktuelle LTS-Version: https://nodejs.org/en/download/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Erstelle Python-Umgebung ...
  python -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo [1/4] Python-Umgebung bereits vorhanden.
)

echo.
echo [2/4] Installiere VideoLibre- und YouTube-Komponenten ...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :fail
.venv\Scripts\python.exe -m pip install --upgrade --pre -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo [3/4] Lade den lokalen PO-Token-Dienst ...
if not exist "bgutil-ytdlp-pot-provider\server\package.json" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/2.0.2.zip' -OutFile 'bgutil-provider.zip'; Expand-Archive -LiteralPath 'bgutil-provider.zip' -DestinationPath '.' -Force; if (Test-Path 'bgutil-ytdlp-pot-provider') { Remove-Item 'bgutil-ytdlp-pot-provider' -Recurse -Force }; Move-Item 'bgutil-ytdlp-pot-provider-2.0.2' 'bgutil-ytdlp-pot-provider'; Remove-Item 'bgutil-provider.zip' -Force"
  if errorlevel 1 goto :fail
) else (
  echo PO-Token-Quellcode bereits vorhanden.
)

echo.
echo [4/4] Baue den Token-Dienst ...
pushd "bgutil-ytdlp-pot-provider\server"
call npm ci
if errorlevel 1 (popd & goto :fail)
call npx tsc
if errorlevel 1 (popd & goto :fail)
popd

echo.
echo ================================================
echo Einrichtung abgeschlossen!
echo Starte jetzt START_HIER.bat.
echo ================================================
echo.
pause
exit /b 0

:fail
echo.
echo EINRICHTUNG FEHLGESCHLAGEN.
echo Pruefe deine Internetverbindung und die Fehlermeldung oben.
echo Starte EINRICHTEN.bat nach der Fehlerbehebung erneut.
echo.
pause
exit /b 1
