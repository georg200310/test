# VideoLibre v8 — YouTube extractor compatibility + live stream

VideoLibre v8 startet die Wiedergabe, während FFmpeg die Daten noch vom Quellserver abruft. Es wird **genau eine ausgewählte Videoauflösung** angefordert und zum Browser übertragen. Die alte Funktion, die erst eine vollständige Datei heruntergeladen hat, ist deaktiviert.

## Was sich geändert hat

- Neuer Endpunkt `/api/live/{video_id}?quality=1080` für progressive Wiedergabe.
- Neuer Vorab-Test `/api/resolve/{video_id}?quality=1080`: zeigt echte yt-dlp-/YouTube-Fehler im Player statt einer irreführenden MIME-Meldung.
- yt-dlp aktiviert den bereits vorhandenen Node-22-Laufzeit-Interpreter ausdrücklich für YouTubes EJS-JavaScript-Challenges.
- yt-dlp wird im Docker- und Windows-Setup mit dem empfohlenen Nightly-Kanal installiert, damit YouTube-Extractor-Fixes schneller verfügbar sind.
- Bei der Videoauflösung werden nacheinander `mweb`, `web_embedded` und `android_vr` als Player-Clients versucht; der erfolgreiche Client wird im Status angezeigt.
- yt-dlp darf die jeweiligen Client-User-Agents selbst setzen; ein alter, global fest eingestellter Desktop-User-Agent wird nicht mehr über die Client-Header gelegt.
- yt-dlp wählt eine Videospur bis zur gewählten Auflösung und genau eine Audiospur.
- FFmpeg führt Bild und Ton live zusammen und gibt fragmentiertes MP4 (H.264) oder WebM (VP9/AV1) aus.
- Bei unterstützten Quell-Codecs wird nicht neu kodiert. Nur seltene, inkompatible Codec-Kombinationen werden in H.264/AAC umgewandelt.
- Browser-Player beginnt, sobald die ersten decodierbaren Fragmente eintreffen.
- Abbruch im Player beendet den dazugehörigen FFmpeg-Prozess.
- Begrenzung auf zwei gleichzeitige Live-Streams pro Serverinstanz.
- Kein vollständiger Datei-Download und kein Cache mehr für die normale Wiedergabe.

**Hinweis:** Bei einem Live-Stream sind Start und Pufferung schneller als beim bisherigen Komplettdownload. Vollständiges Zurückspulen bzw. Springen an beliebige Stellen ist bei diesem einfachen Ein-Pass-Stream nicht verfügbar. Das Format und die verfügbare Auflösung hängen vom Quellvideo und Browser ab.

## Lokal unter Windows

Voraussetzungen:
- Windows 10/11, 64 Bit
- Python 3.11+
- Node.js 22+
- Internetverbindung für YouTube und die Einrichtung

1. ZIP in einen neuen Ordner entpacken, zum Beispiel `C:\VideoLibre-v8`.
2. `EINRICHTEN.bat` ausführen.
3. `START_HIER.bat` ausführen.
4. Der Browser öffnet `http://127.0.0.1:10000`.
5. Zum Beenden im schwarzen Fenster `STRG+C` drücken.

Wähle die gewünschte Qualität vor dem Start. Es wird nur diese Qualitätsstufe angefordert; wenn sie nicht verfügbar ist, verwendet yt-dlp die höchste passende Stufe darunter.

## Online mit Render hosten

Das Repository enthält `Dockerfile`, `start.sh` und `render.yaml`. Der Container startet sowohl den Python-Server als auch den BgUtils-PO-Token-Dienst. Es werden keine lokalen Windows-Komponenten benötigt.

### Veröffentlichung

1. Erstelle ein **öffentliches GitHub-Repository** (z. B. `videolibre-live`).
2. Lade den gesamten Inhalt dieses Projektordners in das Repository hoch. Nicht nur den ZIP-Ordner selbst hochladen.
3. Öffne <https://render.com/> und melde dich an.
4. Wähle **New → Blueprint** und verbinde dein GitHub-Repository.
5. Render liest `render.yaml`, baut das Docker-Image und veröffentlicht den Webdienst.
6. Nach erfolgreichem Deploy zeigt Render eine öffentliche `https://…onrender.com`-Adresse.

Health check: `/api/health`

### Render-Hinweise

- Die Vorlage verwendet den kostenlosen Web-Service-Plan, sofern Render ihn für dein Konto anbietet.
- Kostenlose Render-Webdienste fahren nach einer Inaktivitätsphase herunter und brauchen beim nächsten Zugriff einen Kaltstart. Für eine ständig verfügbare Seite ist ein kostenpflichtiger, nicht schlafender Web-Service sinnvoll.
- Der Live-Stream benötigt ausreichend ausgehende Bandbreite und CPU. Die Vorlage begrenzt deshalb auf zwei gleichzeitige Streams je Instanz.
- Kostenlose Instanzen haben ein flüchtiges Dateisystem. v8 schreibt für normale Live-Wiedergabe keine vollständigen Videodateien auf die Festplatte.
- Die Standardvorlage ist für einen privaten Test und wenige gleichzeitige Nutzer gedacht, nicht für ein großes öffentliches Publikum. Erhöhe Stream-Limits und Compute-Leistung erst nach Lasttests.
- Die öffentliche Website enthält keine Nutzeranmeldung. Jeder, der die URL kennt, kann die Such- und Stream-Funktionen verwenden. Teile sie deshalb zunächst nur mit Personen, denen du vertraust.

## Qualität und Geschwindigkeit

- `720`: maximale 720p-Quelle
- `1080`: maximale 1080p-Quelle
- `1440`: maximale 1440p-Quelle
- `2160`: maximale 2160p-Quelle
- `best`: höchste verfügbare Quelle

Es wird kein mehrfaches Auflösungsset erzeugt. Pro Wiedergabe wird eine Auflösung plus die passende Audiospur verwendet. Höhere Qualität erfordert mehr Netzwerkbandbreite. Ob ein Browser den konkreten Codec direkt unterstützt, hängt von Gerät, Betriebssystem und Browser ab.

## Technische Hinweise

- API: FastAPI + yt-dlp
- Live-Muxing: FFmpeg mit fragmentiertem MP4/WebM
- PO-Token-Dienst: BgUtils auf `127.0.0.1:4416`
- Start: `start.sh` (Render/Docker) oder `run_local.py` (Windows)

Es besteht keine Garantie für dauerhafte Kompatibilität. YouTube kann Formate, Zugriffsbeschränkungen oder Anforderungen an Tokens ändern. Private, DRM-geschützte oder sonst gesperrte Videos können nicht wiedergegeben werden.


## Bestehendes GitHub-/Render-Projekt aktualisieren

1. Lade die Datei `videolibre-source.zip` aus dem beigefügten Update-Paket in das Hauptverzeichnis deines GitHub-Repositories hoch.
2. Die vorhandene GitHub-Action `Unpack VideoLibre` entpackt die ZIP in die Repository-Wurzel und löscht anschließend die ZIP-Datei.
3. Render startet wegen `autoDeploy: true` danach automatisch einen neuen Build.
4. Öffne nach dem Deploy `https://DEINE-RENDER-ADRESSE.onrender.com/api/health`. Die Antwort enthält `yt_dlp_version`, `youtube_js_runtime`, `youtube_player_fallbacks` und `preflight_diagnostics`.

Beim Abspielen prüft die Website nun zunächst `/api/resolve/...`. Falls YouTube keine Player-Antwort liefert, wird diese konkrete Meldung im Player angezeigt, bevor der MIME-Fehler des Browserplayers entstehen kann.

## Warum die Änderung nötig ist

Node.js war im Docker-Image zwar bereits vorhanden, aber yt-dlp aktiviert Node in der Python-API nicht automatisch. Node wird jetzt explizit als JS-Laufzeit aktiviert. Außerdem wird der von yt-dlp empfohlene Nightly-Kanal verwendet und die Extraktion versucht alternative YouTube-Player-Clients. Das kann YouTube-Änderungen abfangen, garantiert aber keinen Erfolg, wenn YouTube die öffentliche Hosting-IP blockiert.


**Wichtige Hosting-Grenze:** Wenn YouTube die ausgehende IP-Adresse des Render-Dienstes blockiert, können weder EJS noch ein PO-Token-Provider diese IP-Sperre garantiert beheben. In diesem Fall ist ein Hosting-Anbieter mit anderer bzw. dedizierter ausgehender IP nötig. Der BgUtils-Anbieter selbst weist ebenfalls darauf hin, dass ein PO-Token Bot-Checks und 403-Fehler nicht garantiert umgeht.
