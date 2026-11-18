# VideoLibre v10 — Invidious API + yt-dlp fallback

Diese Version versucht zuerst die Invidious-API für YouTube-Suche und Videoformate. Erst wenn eine API-Instanz nicht erreichbar ist oder keine abspielbaren Medienformate liefert, verwendet VideoLibre wieder yt-dlp direkt.

## Neu in v10

- **Invidious zuerst:** Suche über `/api/v1/search` und Videoauflösung über `/api/v1/videos/:id`.
- **Instanzliste automatisch:** VideoLibre liest die offizielle Invidious-Instanzliste unter `https://api.invidious.io/instances.json` und verwendet nur HTTPS-Instanzen, die dort `api=true` melden.
- **Aktuelle Fallback-Instanz:** Falls die Liste nicht erreichbar ist, ist `yt.chocolatemoo53.com` als dokumentierte API-fähige Instanz hinterlegt (Stand 18.11.2026).
- **Fallback zu yt-dlp:** Wenn Invidious nicht antwortet, werden die bisherigen yt-dlp-Player-Clients getestet.
- **Fallback beim Streamstart:** Falls eine Invidious-Medien-URL keine Daten liefert, versucht VideoLibre yt-dlp ein zweites Mal.
- **Echter Datei-Download:** `/api/download/{video_id}?quality=1080` sendet den Stream mit `Content-Disposition: attachment` an den Browser.
- **Player bleibt erhalten:** `/api/live/{video_id}?quality=1080` liefert weiterhin den Live-Stream.
- **Cookies bleiben optional:** `YOUTUBE_COOKIES_B64` und `YOUTUBE_COOKIES_FILE` werden nur verwendet, wenn yt-dlp als Ersatzquelle aufgerufen wird.

## Warum Invidious?

Invidious stellt API-Endpunkte für die Suche und die Video-Metadaten einschließlich `adaptiveFormats`, `formatStreams` und `hlsUrl` bereit. VideoLibre wählt daraus die passende Video- und Audiospur und verbindet sie mit FFmpeg. Nicht jede Instanz aktiviert die API, und öffentliche Instanzen können ausfallen oder ebenfalls keine Formate liefern.

Die offizielle Instanzliste ist derzeit klein. Am 18.11.2026 war `yt.chocolatemoo53.com` die gelistete öffentliche Instanz mit `api=true`; andere gelistete Instanzen meldeten `api=false`. VideoLibre prüft die Liste zur Laufzeit erneut und verlässt sich nicht dauerhaft nur auf diese Momentaufnahme.

## Optional: eigene Invidious-Instanz festlegen

In Render → Environment kann optional `INVIDIOUS_INSTANCES` gesetzt werden. Mehrere HTTPS-Origins werden mit Komma getrennt:

```text
INVIDIOUS_INSTANCES=https://example-one.invalid,https://example-two.invalid
```

Nur Instanzen verwenden, denen du vertraust und die ihre API aktiviert haben. Leere Variable bedeutet: offizielle Instanzliste automatisch verwenden.

## Lokal unter Windows

Voraussetzungen: Windows 10/11, Python 3.11+, Node.js 22+ und eine Internetverbindung.

1. ZIP entpacken.
2. `EINRICHTEN.bat` ausführen.
3. `START_HIER.bat` ausführen.
4. `http://127.0.0.1:10000` öffnen.

## Online mit Render

1. Gesamten Inhalt dieses Projektordners in das GitHub-Repository laden (einschließlich `Dockerfile`, `render.yaml`, `start.sh`, `app.py` und `static/`).
2. In Render einen Deploy auslösen oder den automatischen GitHub-Deploy abwarten.
3. `/api/health` öffnen. Dort sollten `invidious_api_configured: true`, die erkannten `invidious_instances` und `primary_source` sichtbar sein, wenn eine API-Instanz gefunden wird.

## Qualität

- `720`, `1080`, `1440`, `2160`: höchste verfügbare Quelle bis zu dieser Höhe
- `best`: höchste erkannte Videoquelle
- Wenn eine Instanz nur kombinierte progressive Formate anbietet, kann die maximale Auflösung geringer sein als bei getrennten adaptiven Spuren.

## Grenzen

- Die API eines Drittanbieters ist keine Verfügbarkeitsgarantie. Die Instanz kann überlastet sein, API-Zugriff abschalten oder von YouTube keine Medienformate erhalten.
- Cookies und direkte yt-dlp-Auflösung sind weiterhin nur Fallbacks; Invidious umgeht keine DRM- oder Bezahlschranken.
- Öffentliche Instanzen nicht überlasten; nur Inhalte laden, für die du die nötigen Rechte oder Erlaubnis hast.
- Live-Streams, private, altersbeschränkte, regionale oder anderweitig eingeschränkte Videos können weiterhin nicht verfügbar sein.
- Ein echter YouTube-Live-Download wurde während der Erstellung nicht ausgeführt. Python-/JavaScript-Syntax und lokale Format-Auswahltests werden separat geprüft.
