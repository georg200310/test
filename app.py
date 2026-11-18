from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import json
import ipaddress
import os
import re
import socket
import time
from collections import defaultdict, deque
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit
from urllib.request import Request as URLRequest, urlopen

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("videolibre")

app = FastAPI(title="VideoLibre v10 Invidious", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CACHE_TTL_SECONDS = 180
SEARCH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
# Short-lived cache bridges the UI preflight request and the following live-stream request.
# Signed source URLs are retained only briefly and never written to disk.
TRACKS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
TRACKS_CACHE_TTL_SECONDS = 20
QUALITY_HEIGHTS = {"720": 720, "1080": 1080, "1440": 1440, "2160": 2160, "best": None}
REQUESTS: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMIT = 40
RATE_WINDOW = 60
EXTRACTION_SLOTS = asyncio.Semaphore(2)
LIVE_STREAM_SEMAPHORE = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_STREAMS", "2"))))

# Invidious is tried first for search and stream metadata. The registry is the
# official list at api.invidious.io; only HTTPS instances advertising api=true
# are selected. This snapshot is a last-resort fallback if the registry is down.
INVIDIOUS_REGISTRY_URL = "https://api.invidious.io/instances.json?pretty=1&sort_by=type,users"
INVIDIOUS_DEFAULTS = ("https://yt.chocolatemoo53.com",)
INVIDIOUS_CACHE_SECONDS = 900
INVIDIOUS_INSTANCES_CACHE: tuple[float, list[str]] = (0.0, [])
INVIDIOUS_USER_AGENT = "VideoLibre/10.0 (+https://docs.invidious.io/)"

def youtube_cookie_file() -> Path | None:
    """Return an optional Netscape cookies file without exposing it in logs.

    For hosted deployments, YOUTUBE_COOKIES_B64 can contain the Base64-encoded
    contents of a cookies.txt file. YOUTUBE_COOKIES_FILE can point at a mounted
    secret file instead. Never commit either file or its contents to the repo.
    """
    mounted_path = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if mounted_path:
        path = Path(mounted_path)
        if not path.is_file():
            raise RuntimeError("YOUTUBE_COOKIES_FILE ist gesetzt, aber die Datei ist nicht vorhanden.")
        return path

    encoded = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if not encoded:
        return None

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("YOUTUBE_COOKIES_B64 ist kein gültiger Base64-Wert.") from exc

    first_line = raw.decode("utf-8-sig", errors="replace").splitlines()[:1]
    if not first_line or first_line[0].strip() not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
        raise RuntimeError("Die YouTube-Cookie-Datei muss das Netscape/cookies.txt-Format besitzen.")

    path = Path("/tmp") / f"videolibre-youtube-cookies-{os.getpid()}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as cookie_file:
        cookie_file.write(raw)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def pot_provider_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.25):
            return True
    except OSError:
        return False


def check_rate_limit(request: Request) -> None:
    # Apply a light request throttle for both local and public deployments.
    forwarded_for = request.headers.get("x-forwarded-for", "")
    forwarded_parts = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    ip = forwarded_parts[-1] if forwarded_parts else (request.client.host if request.client else "local")
    now = time.monotonic()
    bucket = REQUESTS[ip]
    while bucket and now - bucket[0] > RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Zu viele Anfragen in kurzer Zeit. Bitte kurz warten.")
    bucket.append(now)


PLAYER_CLIENT_FALLBACKS: tuple[tuple[str, ...], ...] = (
    # web_safari can expose HLS formats, which may work when an mweb request
    # is challenged. This is a fallback, not a bypass or a guarantee.
    ("web_safari",),
    ("mweb",),
    ("web_embedded",),
    ("android_vr",),
)


def ydl_options(**extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        # Node 22 is installed in Docker and by the Windows setup. yt-dlp does
        # not enable Node automatically: YouTube's EJS challenge solver needs
        # the runtime to be explicitly enabled in the Python API.
        "js_runtimes": {"node": {}},
        "extractor_args": {
            "youtube": {"player_client": ["mweb"]},
            # The BgUtils HTTP provider defaults to localhost:4416, but pass
            # the address explicitly so local and Render deployments agree.
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        },
        # Do not override the per-client User-Agent: mweb/web_embedded need
        # the exact headers yt-dlp associates with their Innertube clients.
        "verbose": os.getenv("YTDLP_VERBOSE", "0") == "1",
    }
    cookie_path = youtube_cookie_file()
    if cookie_path is not None:
        options["cookiefile"] = str(cookie_path)
    options.update(extra)
    return options


def thumbnail_for(info: dict[str, Any]) -> str | None:
    thumbs = info.get("thumbnails") or []
    for item in reversed(thumbs):
        if isinstance(item, dict) and item.get("url"):
            return item["url"]
    return info.get("thumbnail")


def _fetch_json(url: str, timeout: float = 12.0) -> Any:
    """Fetch bounded JSON from a configured HTTPS API endpoint."""
    request = URLRequest(
        url,
        headers={
            "User-Agent": INVIDIOUS_USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(response, 'status', 'Fehler')}")
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError("API-Antwort ist zu groß.")
    return json.loads(raw.decode("utf-8"))


def _valid_https_origin(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 300:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return None
        except ValueError:
            pass
        return f"https://{host}"
    except Exception:
        return None


def invidious_instances() -> list[str]:
    """Return current public Invidious instances that explicitly enable the API."""
    global INVIDIOUS_INSTANCES_CACHE
    now = time.monotonic()
    cached_at, cached = INVIDIOUS_INSTANCES_CACHE
    if cached and now - cached_at < INVIDIOUS_CACHE_SECONDS:
        return list(cached)

    configured = os.getenv("INVIDIOUS_INSTANCES", "").strip()
    candidates: list[str] = []
    if configured:
        for raw in configured.split(","):
            origin = _valid_https_origin(raw.strip())
            if origin and origin not in candidates:
                candidates.append(origin)
    else:
        try:
            registry = _fetch_json(INVIDIOUS_REGISTRY_URL, timeout=3.0)
            ranked: list[tuple[float, str]] = []
            if isinstance(registry, list):
                for entry in registry:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    meta = entry[1]
                    if not isinstance(meta, dict) or meta.get("api") is not True or meta.get("type") != "https":
                        continue
                    monitor = meta.get("monitor")
                    if isinstance(monitor, dict) and monitor.get("down") is True:
                        continue
                    origin = _valid_https_origin(meta.get("uri"))
                    if not origin:
                        continue
                    uptime = 0.0
                    if isinstance(monitor, dict):
                        try:
                            uptime = float(monitor.get("uptime") or 0)
                        except (TypeError, ValueError):
                            pass
                    ranked.append((uptime, origin))
            for _, origin in sorted(ranked, key=lambda item: item[0], reverse=True):
                if origin not in candidates:
                    candidates.append(origin)
        except Exception as exc:
            log.warning("Invidious instances registry unavailable: %s", str(exc)[:180])

        # Use the documented API-enabled instance as a last fallback if the
        # official registry cannot be reached from the hosting environment.
        for fallback in INVIDIOUS_DEFAULTS:
            origin = _valid_https_origin(fallback)
            if origin and origin not in candidates:
                candidates.append(origin)

    candidates = candidates[:5]
    INVIDIOUS_INSTANCES_CACHE = (now, candidates)
    return list(candidates)


def _invidious_json(base: str, endpoint: str) -> Any:
    # endpoint is assembled only from fixed paths and validated IDs/encoded queries.
    return _fetch_json(base.rstrip("/") + endpoint, timeout=18.0)


def _video_height(fmt: dict[str, Any]) -> int:
    for key in ("height", "resolution", "qualityLabel", "quality"):
        value = fmt.get(key)
        if value is None:
            continue
        text = str(value)
        match = re.search(r"(?:x|^)(\d{3,4})(?:p|$)", text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
        match = re.search(r"(2160|1440|1080|720|480|360|240)p?", text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    return 0


def _codec_tokens(fmt: dict[str, Any]) -> list[str]:
    mime = str(fmt.get("type") or "")
    matches = re.findall(r"codecs\s*=\s*[\"']([^\"']+)[\"']", mime, flags=re.IGNORECASE)
    tokens: list[str] = []
    for group in matches:
        tokens.extend(part.strip().lower() for part in group.split(",") if part.strip())
    encoding = str(fmt.get("encoding") or "").strip().lower()
    if encoding and encoding not in tokens:
        tokens.append(encoding)
    return tokens


def _is_video_codec(codec: str) -> bool:
    return codec.startswith(("avc", "h264", "vp8", "vp9", "vp09", "av01", "av1", "hev", "hvc"))


def _is_audio_codec(codec: str) -> bool:
    return codec.startswith(("mp4a", "aac", "opus", "vorbis", "ac-3", "ec-3", "flac"))


def _safe_media_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 12000:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 443):
            return None
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return None
        except ValueError:
            pass
        return value
    except Exception:
        return None


def _invidious_format(fmt: dict[str, Any], *, kind: str, instance: str, video_id: str) -> dict[str, Any] | None:
    media_url = _safe_media_url(fmt.get("url"))
    if not media_url:
        return None
    codecs = _codec_tokens(fmt)
    mime = str(fmt.get("type") or "").lower()
    if kind == "video":
        codec = next((c for c in codecs if _is_video_codec(c)), None)
        if not codec:
            codec = "h264" if "mp4" in str(fmt.get("container") or mime) else "vp9"
        return {
            "url": media_url,
            "format_id": str(fmt.get("itag") or "invidious-video"),
            "height": _video_height(fmt) or None,
            "fps": fmt.get("fps"),
            "vcodec": codec,
            "acodec": "none",
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0", "Referer": "https://www.youtube.com/"},
        }
    if kind == "audio":
        codec = next((c for c in codecs if _is_audio_codec(c)), None)
        if not codec:
            codec = "aac" if "mp4" in str(fmt.get("container") or mime) else "opus"
        return {
            "url": media_url,
            "format_id": str(fmt.get("itag") or "invidious-audio"),
            "vcodec": "none",
            "acodec": codec,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0", "Referer": "https://www.youtube.com/"},
        }
    # A progressive format or HLS manifest contains both streams.
    video_codec = next((c for c in codecs if _is_video_codec(c)), None)
    audio_codec = next((c for c in codecs if _is_audio_codec(c)), None)
    if not video_codec:
        video_codec = "h264" if "mp4" in str(fmt.get("container") or mime) else "vp9"
    if not audio_codec:
        audio_codec = "aac" if "mp4" in str(fmt.get("container") or mime) else "opus"
    return {
        "url": media_url,
        "format_id": str(fmt.get("itag") or "invidious-progressive"),
        "height": _video_height(fmt) or None,
        "fps": fmt.get("fps"),
        "vcodec": video_codec,
        "acodec": audio_codec,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0", "Referer": "https://www.youtube.com/"},
    }


def _tracks_from_invidious(data: dict[str, Any], quality: str, instance: str, video_id: str) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    max_height = QUALITY_HEIGHTS.get(quality)
    adaptive = data.get("adaptiveFormats") or []
    video_candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    audio_candidates: list[tuple[int, dict[str, Any]]] = []
    if isinstance(adaptive, list):
        for fmt in adaptive:
            if not isinstance(fmt, dict) or not fmt.get("url"):
                continue
            mime = str(fmt.get("type") or "").lower()
            codecs = _codec_tokens(fmt)
            if mime.startswith("video/") or (fmt.get("qualityLabel") and not fmt.get("audioQuality")):
                height = _video_height(fmt)
                if max_height is None or not height or height <= max_height:
                    built = _invidious_format(fmt, kind="video", instance=instance, video_id=video_id)
                    if built:
                        try:
                            bitrate = int(fmt.get("bitrate") or 0)
                        except (TypeError, ValueError):
                            bitrate = 0
                        video_candidates.append((height, bitrate, fmt, built))
            elif mime.startswith("audio/") or fmt.get("audioQuality"):
                built = _invidious_format(fmt, kind="audio", instance=instance, video_id=video_id)
                if built:
                    try:
                        bitrate = int(fmt.get("bitrate") or 0)
                    except (TypeError, ValueError):
                        bitrate = 0
                    audio_candidates.append((bitrate, built))

    if video_candidates and audio_candidates:
        video_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        audio_candidates.sort(key=lambda item: item[0], reverse=True)
        height, _, source_fmt, video_track = video_candidates[0]
        audio_track = audio_candidates[0][1]
        return {
            "title": data.get("title") or "Unbekanntes Video",
            "channel": data.get("author") or "YouTube-Kanal",
            "height": height or None,
            "fps": source_fmt.get("fps"),
            "video_track": video_track,
            "audio_track": audio_track,
            "same_input": False,
            "video_codec": video_track.get("vcodec") or "unknown",
            "audio_codec": audio_track.get("acodec") or "unknown",
            "format_id": f"{video_track.get('format_id')}+{audio_track.get('format_id')}",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "player_client": f"Invidious / {urlsplit(instance).hostname}",
            "source": "invidious",
            "invidious_instance": instance,
        }

    # If this instance does not expose adaptive tracks, try the best combined
    # progressive format. It has lower maximum resolution on many videos.
    progressive: list[tuple[int, int, dict[str, Any]]] = []
    formats = data.get("formatStreams") or []
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, dict) or not fmt.get("url"):
                continue
            height = _video_height(fmt)
            if max_height is not None and height and height > max_height:
                continue
            built = _invidious_format(fmt, kind="combined", instance=instance, video_id=video_id)
            if not built:
                continue
            try:
                bitrate = int(fmt.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            progressive.append((height, bitrate, built))
    if progressive:
        progressive.sort(key=lambda item: (item[0], item[1]), reverse=True)
        height, _, combined = progressive[0]
        return {
            "title": data.get("title") or "Unbekanntes Video",
            "channel": data.get("author") or "YouTube-Kanal",
            "height": height or None,
            "video_track": combined,
            "audio_track": combined,
            "same_input": True,
            "video_codec": combined.get("vcodec") or "unknown",
            "audio_codec": combined.get("acodec") or "unknown",
            "format_id": combined.get("format_id") or "invidious-progressive",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "player_client": f"Invidious / {urlsplit(instance).hostname}",
            "source": "invidious",
            "invidious_instance": instance,
        }

    hls_url = _safe_media_url(data.get("hlsUrl"))
    if hls_url:
        manifest = {
            "url": hls_url,
            "format_id": "invidious-hls",
            "height": None,
            "vcodec": "h264",
            "acodec": "aac",
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0", "Referer": "https://www.youtube.com/"},
        }
        return {
            "title": data.get("title") or "Unbekanntes Video",
            "channel": data.get("author") or "YouTube-Kanal",
            "height": None,
            "video_track": manifest,
            "audio_track": manifest,
            "same_input": True,
            "video_codec": "h264",
            "audio_codec": "aac",
            "format_id": "invidious-hls",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "player_client": f"Invidious HLS / {urlsplit(instance).hostname}",
            "source": "invidious",
            "invidious_instance": instance,
        }
    return None


def _search_invidious(query: str) -> dict[str, Any] | None:
    failures: list[str] = []
    for base in invidious_instances():
        try:
            endpoint = f"/api/v1/search?q={quote(query, safe='')}&type=video&hl=de"
            payload = _invidious_json(base, endpoint)
            if not isinstance(payload, list):
                raise RuntimeError("unerwartetes API-Format")
            results: list[dict[str, Any]] = []
            for item in payload:
                if not isinstance(item, dict) or item.get("type") not in (None, "video"):
                    continue
                video_id = item.get("videoId")
                if not video_id or not VIDEO_ID_RE.fullmatch(str(video_id)):
                    continue
                thumbs = item.get("videoThumbnails") or []
                thumb = None
                if isinstance(thumbs, list):
                    usable = [t for t in thumbs if isinstance(t, dict) and t.get("url")]
                    if usable:
                        thumb = max(usable, key=lambda t: int(t.get("width") or 0)).get("url")
                results.append({
                    "id": video_id,
                    "title": item.get("title") or "Unbekanntes Video",
                    "channel": item.get("author") or "YouTube-Kanal",
                    "duration": item.get("lengthSeconds"),
                    "thumbnail": thumb or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    "views": item.get("viewCount"),
                    "source": "invidious",
                })
                if len(results) >= 15:
                    break
            return {"query": query, "results": results, "source": "invidious", "instance": base}
        except Exception as exc:
            failures.append(f"{urlsplit(base).hostname}: {str(exc).strip().splitlines()[0][:140]}")
            log.warning("Invidious search failed on %s: %s", base, str(exc)[:180])
    if failures:
        log.warning("All Invidious search instances failed: %s", " | ".join(failures[-3:]))
    return None


def do_search(query: str) -> dict[str, Any]:
    key = query.casefold().strip()
    cached = SEARCH_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    invidious_result = _search_invidious(query)
    if invidious_result is not None:
        SEARCH_CACHE[key] = (now, invidious_result)
        return invidious_result

    with yt_dlp.YoutubeDL(ydl_options(extract_flat="in_playlist", playlistend=15)) as ydl:
        data = ydl.extract_info(f"ytsearch15:{query}", download=False)

    results: list[dict[str, Any]] = []
    for entry in (data or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not video_id or not VIDEO_ID_RE.fullmatch(str(video_id)):
            continue
        results.append(
            {
                "id": video_id,
                "title": entry.get("title") or "Unbekanntes Video",
                "channel": entry.get("channel") or entry.get("uploader") or "YouTube-Kanal",
                "duration": entry.get("duration"),
                "thumbnail": thumbnail_for(entry) or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "views": entry.get("view_count"),
            }
        )

    payload = {"query": query, "results": results, "source": "yt-dlp"}
    SEARCH_CACHE[key] = (now, payload)
    return payload


def _live_format_selector(quality: str) -> tuple[str, list[str]]:
    """Select one video track at one chosen height plus exactly one audio track."""
    if quality not in QUALITY_HEIGHTS:
        raise ValueError("Ungültige Qualitätsstufe.")
    max_height = QUALITY_HEIGHTS[quality]
    height_filter = f"[height<={max_height}]" if max_height else ""
    selector = f"bestvideo{height_filter}+bestaudio/best{height_filter}"
    sort = ([f"res:{max_height}"] if max_height else ["res"]) + ["fps", "vcodec", "acodec"]
    return selector, sort


def _tracks_from_info(info: dict[str, Any]) -> dict[str, Any] | None:
    selected = info.get("requested_formats")
    if not isinstance(selected, list) or not selected:
        selected = [info]

    video_track = next(
        (fmt for fmt in selected if fmt.get("url") and fmt.get("vcodec") not in (None, "none")),
        None,
    )
    audio_track = next(
        (fmt for fmt in selected if fmt.get("url") and fmt.get("acodec") not in (None, "none")),
        None,
    )
    if video_track is None or audio_track is None:
        return None

    video_codec = str(video_track.get("vcodec") or "unknown").lower()
    audio_codec = str(audio_track.get("acodec") or "unknown").lower()
    same_input = video_track is audio_track or video_track.get("url") == audio_track.get("url")
    return {
        "title": info.get("title") or "Unbekanntes Video",
        "channel": info.get("channel") or info.get("uploader") or "YouTube-Kanal",
        "height": video_track.get("height") or info.get("height"),
        "fps": video_track.get("fps") or info.get("fps"),
        "video_track": video_track,
        "audio_track": audio_track,
        "same_input": same_input,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "format_id": "+".join(str(x.get("format_id") or "") for x in selected if x.get("format_id")),
        "webpage_url": info.get("webpage_url") or "",
    }


def extract_live_tracks(video_id: str, quality: str, use_invidious: bool = True) -> dict[str, Any]:
    """Resolve Invidious first, then retry yt-dlp player clients as a fallback."""
    selector, sort = _live_format_selector(quality)
    url = f"https://www.youtube.com/watch?v={video_id}"
    failures: list[str] = []

    if use_invidious:
        for base in invidious_instances():
            try:
                log.info("Resolving video %s through Invidious API at %s", video_id, base)
                details = _invidious_json(base, f"/api/v1/videos/{video_id}?hl=de")
                tracks = _tracks_from_invidious(details, quality, base, video_id)
                if tracks:
                    return tracks
                failures.append(f"Invidious {urlsplit(base).hostname}: keine passenden Medienformate")
            except Exception as exc:
                detail = str(exc).strip().splitlines()[0][:200] or type(exc).__name__
                failures.append(f"Invidious {urlsplit(base).hostname}: {detail}")
                log.warning("Invidious video lookup failed for %s on %s: %s", video_id, base, detail)

    for clients in PLAYER_CLIENT_FALLBACKS:
        client_label = ",".join(clients)
        extractor_args = {
            "youtube": {"player_client": list(clients)},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        }
        opts = ydl_options(
            skip_download=True,
            format=selector,
            format_sort=sort,
            noplaylist=True,
            check_formats=False,
            extractor_args=extractor_args,
        )
        try:
            log.info(
                "Resolving YouTube video %s at max %sp using player client(s): %s; yt-dlp=%s",
                video_id, quality, client_label, yt_dlp.version.__version__,
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if isinstance(info, dict):
                tracks = _tracks_from_info(info)
                if tracks:
                    tracks["webpage_url"] = info.get("webpage_url") or url
                    tracks["player_client"] = client_label
                    tracks["source"] = "yt-dlp"
                    return tracks
                failures.append(f"{client_label}: keine getrennte Video- und Audiospur gefunden")
            else:
                failures.append(f"{client_label}: YouTube lieferte keine Videoinformationen")
        except yt_dlp.utils.DownloadError as exc:
            detail = str(exc).strip().splitlines()[0][:220] or "Unbekannter yt-dlp-Fehler"
            failures.append(f"{client_label}: {detail}")
            log.warning("YouTube extraction failed for %s using %s: %s", video_id, client_label, detail)
        except Exception as exc:
            detail = str(exc).strip().splitlines()[0][:220] or type(exc).__name__
            failures.append(f"{client_label}: {detail}")
            log.warning("Unexpected extraction error for %s using %s: %s", video_id, client_label, detail)

    reason = " | ".join(failures[-3:])
    if any("Failed to extract any player response" in item for item in failures):
        raise RuntimeError(
            "YouTube hat mit keinem der unterstützten Player-Clients eine Player-Antwort geliefert. "
            "Der Node/EJS-Challenge-Solver ist aktiviert und mehrere Clients wurden versucht. "
            "Mögliche Ursache: YouTube blockiert vorübergehend die IP des Hosting-Anbieters. "
            f"Details: {reason}"
        )
    raise RuntimeError("Weder Invidious noch yt-dlp konnten einen abspielbaren Stream liefern. " + reason)


def _ffmpeg_input_headers(fmt: dict[str, Any]) -> str:
    headers = fmt.get("http_headers") or {}
    blocked = {"host", "content-length", "connection", "accept-encoding", "range"}
    lines = []
    for key, value in headers.items():
        if str(key).lower() in blocked or value is None:
            continue
        lines.append(f"{key}: {value}\r\n")
    return "".join(lines)


def _live_ffmpeg_command(tracks: dict[str, Any]) -> tuple[list[str], str]:
    """Build a copy/mux pipeline that emits browser-playable fragmented media as bytes arrive."""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video = tracks["video_track"]
    audio = tracks["audio_track"]
    video_codec = tracks["video_codec"]
    audio_codec = tracks["audio_codec"]
    same_input = bool(tracks["same_input"])

    args = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-threads", "2"]
    inputs = [video] if same_input else [video, audio]
    for fmt in inputs:
        headers = _ffmpeg_input_headers(fmt)
        if headers:
            args.extend(["-headers", headers])
        args.extend(["-rw_timeout", "30000000", "-i", str(fmt["url"])])

    video_input_index = 0
    audio_input_index = 0 if same_input else 1
    args.extend([
        "-map", f"{video_input_index}:v:0",
        "-map", f"{audio_input_index}:a:0",
        "-max_muxing_queue_size", "2048",
        "-flush_packets", "1",
        "-avoid_negative_ts", "make_zero",
    ])

    if video_codec.startswith(("avc1", "h264")):
        # Keep H.264 as-is; AAC is the browser-friendly MP4 audio codec.
        args.extend(["-c:v", "copy"])
        if audio_codec.startswith(("mp4a", "aac")):
            args.extend(["-c:a", "copy"])
        else:
            args.extend(["-c:a", "aac", "-b:a", "160k"])
        args.extend([
            "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
            "-frag_duration", "1000000",
            "-f", "mp4", "pipe:1",
        ])
        return args, "video/mp4"

    if video_codec.startswith(("vp9", "vp09", "av01", "av1")):
        # VP9/AV1 are delivered in WebM. If the source audio isn't Opus/Vorbis,
        # convert only audio, preserving the high-resolution video bitstream.
        args.extend(["-c:v", "copy"])
        if audio_codec.startswith(("opus", "vorbis")):
            args.extend(["-c:a", "copy"])
        else:
            args.extend(["-c:a", "libopus", "-b:a", "160k"])
        args.extend(["-cluster_time_limit", "1000", "-cluster_size_limit", "1048576", "-f", "webm", "pipe:1"])
        return args, "video/webm"

    # Rare codecs: transcode once to a widely supported H.264/AAC fragmented MP4 stream.
    # This is slower and CPU-heavy, so the UI warns when it happens.
    args.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "24",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
        "-frag_duration", "1000000",
        "-f", "mp4", "pipe:1",
    ])
    return args, "video/mp4"


async def _drain_ffmpeg_stderr(reader: asyncio.StreamReader) -> list[str]:
    tail: deque[str] = deque(maxlen=30)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                tail.append(decoded)
                log.debug("ffmpeg: %s", decoded)
    except (asyncio.CancelledError, OSError):
        pass
    return list(tail)


def _get_cached_tracks(video_id: str, quality: str) -> dict[str, Any] | None:
    key = (video_id, quality)
    cached = TRACKS_CACHE.pop(key, None)
    if not cached:
        return None
    created_at, tracks = cached
    if time.monotonic() - created_at > TRACKS_CACHE_TTL_SECONDS:
        return None
    return tracks


def _cache_tracks(video_id: str, quality: str, tracks: dict[str, Any]) -> None:
    # Keep this cache small: source URLs are signed and should not live long.
    if len(TRACKS_CACHE) > 40:
        oldest = sorted(TRACKS_CACHE.items(), key=lambda item: item[1][0])[:20]
        for key, _ in oldest:
            TRACKS_CACHE.pop(key, None)
    TRACKS_CACHE[(video_id, quality)] = (time.monotonic(), tracks)


@app.get("/api/resolve/{video_id}")
async def resolve_video(request: Request, video_id: str, quality: str = "1080") -> JSONResponse:
    """Preflight extraction so browser users see real YouTube errors, not misleading MIME errors."""
    check_rate_limit(request)
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Ungültige YouTube-Video-ID.")
    if quality not in QUALITY_HEIGHTS:
        raise HTTPException(status_code=400, detail="Qualität muss 720, 1080, 1440, 2160 oder best sein.")
    try:
        async with EXTRACTION_SLOTS:
            tracks = await run_in_threadpool(partial(extract_live_tracks, video_id, quality))
        _cache_tracks(video_id, quality, tracks)
        return JSONResponse({
            "status": "ready",
            "title": tracks["title"],
            "channel": tracks["channel"],
            "height": tracks["height"],
            "video_codec": tracks["video_codec"],
            "audio_codec": tracks["audio_codec"],
            "player_client": tracks.get("player_client", "unknown"),
            "quality_requested": quality,
        })
    except Exception as exc:
        log.exception("YouTube preflight failed for %s (%s)", video_id, quality)
        message = str(exc).strip().splitlines()[0][:700] or "Unbekannter Fehler"
        raise HTTPException(status_code=502, detail=message) from exc




def _download_content_disposition(title: str, extension: str) -> str:
    """Return a safe attachment filename with an ASCII fallback and UTF-8 name."""
    clean_title = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", " ", str(title or "VideoLibre"))
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" .")[:160] or "VideoLibre"
    ascii_title = clean_title.encode("ascii", "ignore").decode("ascii")
    ascii_title = re.sub(r"[^A-Za-z0-9._ -]", "_", ascii_title).strip(" .") or "VideoLibre"
    filename = f"{clean_title}.{extension}"
    ascii_filename = f"{ascii_title}.{extension}"
    return f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _stop_ffmpeg(process: asyncio.subprocess.Process | None, stderr_task: asyncio.Task | None) -> None:
    if process is not None and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    if stderr_task and not stderr_task.done():
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass


async def _start_ffmpeg_stream(tracks: dict[str, Any]) -> tuple[asyncio.subprocess.Process, asyncio.Task, bytes, str]:
    command, media_type = _live_ffmpeg_command(tracks)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    stderr_task = asyncio.create_task(_drain_ffmpeg_stderr(process.stderr))
    try:
        try:
            first_chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), timeout=45)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Der Quellserver hat innerhalb von 45 Sekunden keine Videodaten geliefert.") from exc
        if not first_chunk:
            details = await stderr_task
            raise RuntimeError("FFmpeg konnte den Stream nicht öffnen: " + (details[-1] if details else "keine Mediendaten empfangen"))
        return process, stderr_task, first_chunk, media_type
    except Exception:
        await _stop_ffmpeg(process, stderr_task)
        raise


async def _stream_video(request: Request, video_id: str, quality: str, *, download: bool) -> StreamingResponse:
    """Stream one selected resolution through FFmpeg; browser playback starts before transfer completes."""
    check_rate_limit(request)
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Ungültige YouTube-Video-ID.")
    if quality not in QUALITY_HEIGHTS:
        raise HTTPException(status_code=400, detail="Qualität muss 720, 1080, 1440, 2160 oder best sein.")

    try:
        await asyncio.wait_for(LIVE_STREAM_SEMAPHORE.acquire(), timeout=1.5)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=429, detail="Alle Stream-Plätze sind gerade belegt. Bitte kurz warten.") from exc

    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task | None = None
    try:
        tracks = _get_cached_tracks(video_id, quality)
        if tracks is None:
            tracks = await run_in_threadpool(partial(extract_live_tracks, video_id, quality))
        try:
            process, stderr_task, first_chunk, media_type = await _start_ffmpeg_stream(tracks)
        except Exception as invidious_stream_error:
            if tracks.get("source") != "invidious":
                raise
            log.warning("Invidious returned an unplayable media URL for %s; retrying with yt-dlp: %s", video_id, str(invidious_stream_error)[:200])
            tracks = await run_in_threadpool(partial(extract_live_tracks, video_id, quality, False))
            process, stderr_task, first_chunk, media_type = await _start_ffmpeg_stream(tracks)

        height = tracks.get("height") or "unbekannt"
        codec_line = f"{tracks['video_codec']} / {tracks['audio_codec']}"
        log.info("Live stream started id=%s quality=%sp selected=%sp codec=%s client=%s", video_id, quality, height, codec_line, tracks.get("player_client", "unknown"))

        async def stream_chunks() -> AsyncIterator[bytes]:
            try:
                yield first_chunk
                assert process is not None and process.stdout is not None
                while True:
                    if await request.is_disconnected():
                        break
                    chunk = await process.stdout.read(128 * 1024)
                    if not chunk:
                        break
                    yield chunk
                return_code = await process.wait()
                if return_code != 0:
                    details = await stderr_task if stderr_task else []
                    log.warning("FFmpeg ended with code %s for %s: %s", return_code, video_id, " | ".join(details[-5:]))
            except asyncio.CancelledError:
                raise
            finally:
                if process is not None and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                if stderr_task and not stderr_task.done():
                    stderr_task.cancel()
                    try:
                        await stderr_task
                    except asyncio.CancelledError:
                        pass
                LIVE_STREAM_SEMAPHORE.release()

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
            "Accept-Ranges": "none",
            "X-Video-Height": str(height),
            "X-Video-Codec": codec_line[:120],
            "X-VideoLibre-Mode": "attachment-download" if download else "live-single-resolution",
        }
        if download:
            extension = "mp4" if media_type == "video/mp4" else "webm"
            headers["Content-Disposition"] = _download_content_disposition(tracks.get("title") or f"Video_{video_id}", extension)
        return StreamingResponse(stream_chunks(), media_type=media_type, headers=headers)
    except HTTPException:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
        LIVE_STREAM_SEMAPHORE.release()
        raise
    except Exception as exc:
        log.exception("Live stream setup failed for %s (%s)", video_id, quality)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
        LIVE_STREAM_SEMAPHORE.release()
        message = str(exc).strip().splitlines()[0][:300] or "Unbekannter Fehler"
        raise HTTPException(status_code=502, detail=f"Live-Stream konnte nicht gestartet werden: {message}") from exc


@app.get("/api/live/{video_id}")
async def live_stream(request: Request, video_id: str, quality: str = "1080") -> StreamingResponse:
    """Play the selected video inline in the browser."""
    return await _stream_video(request, video_id, quality, download=False)


@app.get("/api/download/{video_id}")
async def download_video(request: Request, video_id: str, quality: str = "1080") -> StreamingResponse:
    """Send the selected video as a real browser download, without storing it on the server."""
    return await _stream_video(request, video_id, quality, download=True)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    instances = await run_in_threadpool(invidious_instances)
    return {
        "status": "ok",
        "engine": "Invidious API + yt-dlp fallback + FFmpeg",
        "yt_dlp_version": yt_dlp.version.__version__,
        "youtube_js_runtime": "node-enabled",
        "youtube_player_fallbacks": [list(x) for x in PLAYER_CLIENT_FALLBACKS],
        "youtube_cookies_configured": bool(os.getenv("YOUTUBE_COOKIES_B64", "").strip() or os.getenv("YOUTUBE_COOKIES_FILE", "").strip()),
        "preflight_diagnostics": True,
        "po_token_provider": pot_provider_available(),
        "invidious_api_configured": bool(instances),
        "invidious_instances": [urlsplit(base).hostname for base in instances],
        "primary_source": "invidious-api-with-yt-dlp-fallback",
        "local_only": os.getenv("APP_ENV", "local") != "production",
        "public_service": os.getenv("APP_ENV", "local") == "production",
        "live_streaming": True,
        "file_downloads": True,
        "download_mode": "streamed-attachment",
        "stream_mode": "single-resolution-progressive-ffmpeg",
        "max_concurrent_streams": int(os.getenv("MAX_CONCURRENT_STREAMS", "2")),
        "quality_options": ["720", "1080", "1440", "2160", "best"],
    }


@app.get("/api/search")
async def search(request: Request, q: str = "") -> JSONResponse:
    check_rate_limit(request)
    query = q.strip()
    if len(query) < 2 or len(query) > 120:
        raise HTTPException(status_code=400, detail="Bitte gib mindestens zwei und höchstens 120 Zeichen ein.")
    try:
        async with EXTRACTION_SLOTS:
            result = await run_in_threadpool(partial(do_search, query))
        return JSONResponse(result)
    except Exception as exc:
        log.exception("YouTube search failed")
        message = str(exc).strip().splitlines()[0][:280] or "Unbekannter Fehler"
        raise HTTPException(
            status_code=502,
            detail=f"YouTube-Suche fehlgeschlagen: {message}. Prüfe deine Internetverbindung und aktualisiere yt-dlp bei Bedarf.",
        ) from exc


@app.get("/api/video/{video_id}")
async def retired_file_download(video_id: str) -> JSONResponse:
    raise HTTPException(status_code=410, detail="Datei-Download ist in v8 entfernt. Nutze die progressive Wiedergabe über /api/live.")


@app.get("/api/stream/{video_id}")
async def retired_cached_stream(video_id: str) -> JSONResponse:
    raise HTTPException(status_code=410, detail="Der alte Datei-Cache wurde entfernt. Nutze /api/live.")


@app.get("/api/jobs/{job_id}")
async def retired_download_job(job_id: str) -> JSONResponse:
    raise HTTPException(status_code=410, detail="V7 nutzt Live-Streams statt Download-Aufträge.")


@app.get("/robots.txt")
async def robots() -> FileResponse:
    return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain") if (STATIC_DIR / "robots.txt").exists() else FileResponse(STATIC_DIR / "index.html")


@app.get("/{full_path:path}")
async def frontend(full_path: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
