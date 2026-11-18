from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import socket
import time
from collections import defaultdict, deque
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("videolibre")

app = FastAPI(title="VideoLibre Local", docs_url=None, redoc_url=None)
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


def do_search(query: str) -> dict[str, Any]:
    key = query.casefold().strip()
    cached = SEARCH_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

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

    payload = {"query": query, "results": results}
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


def extract_live_tracks(video_id: str, quality: str) -> dict[str, Any]:
    """Resolve one chosen resolution, retrying alternate YouTube player clients."""
    selector, sort = _live_format_selector(quality)
    url = f"https://www.youtube.com/watch?v={video_id}"
    failures: list[str] = []

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
    raise RuntimeError("YouTube konnte keinen abspielbaren Stream liefern. " + reason)


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
        command, media_type = _live_ffmpeg_command(tracks)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.create_task(_drain_ffmpeg_stderr(process.stderr))
        try:
            first_chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), timeout=45)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Der Quellserver hat innerhalb von 45 Sekunden keine Videodaten geliefert.") from exc
        if not first_chunk:
            details = await stderr_task
            raise RuntimeError("FFmpeg konnte den Stream nicht öffnen: " + (details[-1] if details else "keine Mediendaten empfangen"))

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
    return {
        "status": "ok",
        "engine": "yt-dlp + FFmpeg live stream",
        "yt_dlp_version": yt_dlp.version.__version__,
        "youtube_js_runtime": "node-enabled",
        "youtube_player_fallbacks": [list(x) for x in PLAYER_CLIENT_FALLBACKS],
        "youtube_cookies_configured": bool(os.getenv("YOUTUBE_COOKIES_B64", "").strip() or os.getenv("YOUTUBE_COOKIES_FILE", "").strip()),
        "preflight_diagnostics": True,
        "po_token_provider": pot_provider_available(),
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
