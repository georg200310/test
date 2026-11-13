# VideoLibre v7 — Live Stream Edition

VideoLibre v7 startet die Wiedergabe, während FFmpeg die Daten noch vom Quellserver abruft. Es wird **genau eine ausgewählte Videoauflösung** angefordert und zum Browser übertragen. Die alte Funktion, die erst eine vollständige Datei heruntergeladen hat, ist deaktiviert.

## Was sich geändert hat

- Neuer Endpunkt `/api/live/{video_id}?quality=1080` für progressive Wiedergabe.
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

1. ZIP in einen neuen Ordner entpacken, zum Beispiel `C:\VideoLibre-v7`.
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
- Kostenlose Instanzen haben ein flüchtiges Dateisystem. v7 schreibt für normale Live-Wiedergabe keine vollständigen Videodateien auf die Festplatte.
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
