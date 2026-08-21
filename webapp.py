#!/usr/bin/env python3
"""Web interface listing source albums with a manual full-library conversion button."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request

SOURCE_DIRECTORY = Path(os.environ.get("SOURCE_DIRECTORY", "/music/source"))
CONVERTER_SCRIPT = Path(os.environ.get("CONVERTER_SCRIPT", "/app/audio_convert.py"))
CONVERTER_ARGS = os.environ.get("CONVERTER_ARGS", "")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))
COVER_CACHE_DIRECTORY = Path(os.environ.get("COVER_CACHE_DIRECTORY", "/tmp/converter-covers"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
LOG_LIMIT = 300

AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".ape", ".caf", ".dff", ".dsf", ".flac",
    ".m4a", ".m4b", ".mp2", ".mp3", ".mpc", ".ogg", ".oga", ".opus",
    ".tak", ".tta", ".wav", ".webm", ".wma", ".wv",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PREFERRED_COVER_STEMS = {"cover", "folder", "front", "album", "albumart"}

# Library layout: Artist/[Year] Album/Disc N, where the disc level is optional.
ALBUM_FOLDER_PATTERN = r"^[\[(](\d{4})[\])]\s*(.*)$"
DISC_FOLDER_PATTERN = r"^(?:disc|cd)[\s._-]*(\d+)$|^(\d+)$"

app = Flask(__name__)

lock = threading.Lock()
state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": [],
}


def append_log(line: str) -> None:
    with lock:
        state["log"].append(line)
        del state["log"][:-LOG_LIMIT]


def conversion_worker() -> None:
    command = ["python", str(CONVERTER_SCRIPT), *shlex.split(CONVERTER_ARGS)]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        append_log(f"unable to start converter: {error}")
        with lock:
            state["running"] = False
            state["finished_at"] = time.time()
            state["returncode"] = -1
        return
    assert process.stdout is not None
    for line in process.stdout:
        append_log(line.rstrip("\n"))
    returncode = process.wait()
    append_log(f"Conversion finished (exit {returncode})")
    with lock:
        state["running"] = False
        state["finished_at"] = time.time()
        state["returncode"] = returncode


def parse_album_folder(name: str) -> tuple[str | None, str]:
    """Split an album folder name into (year, title), tolerating a missing year."""
    match = re.match(ALBUM_FOLDER_PATTERN, name)
    if match:
        return match.group(1), match.group(2) or name
    return None, name


def disc_number(name: str) -> str | None:
    """Return the disc number when a folder looks like Disc 1, CD2, 3, ..."""
    match = re.match(DISC_FOLDER_PATTERN, name.strip(), re.IGNORECASE)
    if not match:
        return None
    return (match.group(1) or match.group(2)).lstrip("0") or "0"


def scan_library() -> list[dict]:
    albums: dict[str, dict] = {}
    for root, _directories, filenames in os.walk(SOURCE_DIRECTORY):
        audio_count = sum(1 for f in filenames if Path(f).suffix.lower() in AUDIO_EXTENSIONS)
        if not audio_count:
            continue
        parts = Path(root).relative_to(SOURCE_DIRECTORY).parts
        if len(parts) >= 2:
            artist = parts[0]
            album_folder = parts[1]
        else:
            artist = ""
            album_folder = parts[0]
        year, title = parse_album_folder(album_folder)
        disc = disc_number(parts[-1]) if len(parts) > 2 else None
        key = "/".join(parts[:2])
        entry = albums.setdefault(key, {
            "artist": artist or "Unknown artist",
            "year": year,
            "album": title,
            "path": key,
            "total_tracks": 0,
            "disc_counts": {},
        })
        entry["disc_counts"][disc] = entry["disc_counts"].get(disc, 0) + audio_count
        entry["total_tracks"] += audio_count

    result = []
    for entry in albums.values():
        counts = entry.pop("disc_counts")
        discs = [
            {"disc": disc, "tracks": count}
            for disc, count in sorted(
                counts.items(),
                key=lambda item: (item[0] is None, int(item[0]) if item[0] else 0),
            )
        ]
        result.append({**entry, "discs": discs})
    result.sort(key=lambda item: (item["artist"].lower(), item["year"] or "9999", item["album"].lower()))
    return result


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def find_external_cover(directory: Path) -> Path | None:
    """Prefer conventionally named images in the album folder, then disc folders."""
    try:
        search_directories = [directory] + sorted(
            d for d in directory.iterdir() if d.is_dir()
        )
    except OSError:
        return None
    best: tuple[tuple, Path] | None = None
    for directory_ in search_directories:
        try:
            candidates = list(directory_.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            rank = (
                0 if candidate.stem.lower() in PREFERRED_COVER_STEMS else 1,
                candidate.name.lower(),
            )
            if best is None or rank < best[0]:
                best = (rank, candidate)
    return best[1] if best else None


def extract_embedded_cover(directory: Path, destination: Path) -> bool:
    """Write the first attached picture found in directory's tracks to destination."""
    search_directories = [directory] + sorted(d for d in directory.iterdir() if d.is_dir())
    # The temporary name keeps a .jpg suffix so ffmpeg can infer the muxer.
    temporary = destination.with_name(destination.name + ".tmp.jpg")
    for search_directory in search_directories:
        try:
            tracks = sorted(
                p for p in search_directory.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
            )
        except OSError:
            continue
        for track in tracks:
            command = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", os.fspath(track),
                "-map", "0:v:0", "-frames:v", "1",
                os.fspath(temporary),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    timeout=120,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (subprocess.SubprocessError, OSError):
                temporary.unlink(missing_ok=True)
                continue
            temporary.replace(destination)
            return True
    temporary.unlink(missing_ok=True)
    return False


def cover_cache_paths(relative_key: str) -> tuple[Path, Path]:
    digest = hashlib.sha1(relative_key.encode("utf-8")).hexdigest()
    return COVER_CACHE_DIRECTORY / f"{digest}.none", COVER_CACHE_DIRECTORY / digest


def cover_response(relative_key: str) -> Response:
    base = SOURCE_DIRECTORY.resolve()
    target = (base / relative_key).resolve()
    if not relative_key or not is_within(target, base) or not target.is_dir():
        return Response("not found", status=404)

    negative_marker, cache_file = cover_cache_paths(relative_key)
    if not negative_marker.exists() and not cache_file.exists():
        COVER_CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        image = find_external_cover(target)
        if image is not None:
            cache_file = cache_file.with_suffix(image.suffix.lower())
            shutil.copyfile(image, cache_file)
        elif extract_embedded_cover(target, cache_file.with_suffix(".jpg")):
            cache_file = cache_file.with_suffix(".jpg")
        else:
            negative_marker.touch()

    if cache_file.exists():
        mimetype = mimetypes.guess_type(cache_file.name)[0] or "application/octet-stream"
        return Response(cache_file.read_bytes(), mimetype=mimetype)
    return Response("not found", status=404)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audio Converter</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; }
  #convert { font-size: 1rem; padding: .5rem 1.2rem; cursor: pointer; }
  #convert:disabled { opacity: .5; cursor: not-allowed; }
  #status { margin-left: .8rem; }
  .toolbar { display: flex; align-items: center; gap: .4rem; margin-top: 1.2rem; }
  .toolbar .spacer { flex: 1; }
  .toolbar button.active { font-weight: 700; }
  pre { background: rgba(127,127,127,.12); padding: .8rem; border-radius: 6px;
        max-height: 260px; overflow-y: auto; font-size: .8rem; white-space: pre-wrap; }
  table { border-collapse: collapse; width: 100%; margin-top: .6rem; }
  td, th { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid rgba(127,127,127,.3); }
  td.num, th.num { text-align: right; }
  .hidden { display: none !important; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
          gap: .8rem; margin-top: .6rem; }
  .card { border: 1px solid rgba(127,127,127,.3); border-radius: 8px; overflow: hidden;
          background: rgba(127,127,127,.08); }
  .cover { width: 100%; aspect-ratio: 1/1; object-fit: cover; display: block; }
  .placeholder { width: 100%; aspect-ratio: 1/1; display: flex; align-items: center;
                 justify-content: center; font-size: 2.4rem; opacity: .4; }
  .meta { padding: .5rem .6rem; }
  .title { display: block; font-weight: 600; font-size: .85rem; overflow: hidden;
           text-overflow: ellipsis; white-space: nowrap; }
  .sub { display: block; font-size: .72rem; opacity: .7; overflow: hidden;
         text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
<h1>Audio Converter</h1>
<p>
  <button id="convert">Convert entire library</button>
  <span id="status"></span>
</p>
<pre id="log">No conversion has run in this session yet.</pre>

<div class="toolbar">
  <button id="view-table" class="active">Table</button>
  <button id="view-grid">Grid</button>
  <span class="spacer"></span>
  <span id="summary"></span>
</div>
<table id="album-table">
  <thead><tr><th>Artist</th><th>Year</th><th>Album</th><th>Disc</th><th class="num">Tracks</th></tr></thead>
  <tbody id="albums"></tbody>
</table>
<div id="album-grid" class="grid hidden"></div>

<script>
const convertButton = document.getElementById('convert');
const statusEl = document.getElementById('status');
const logEl = document.getElementById('log');
const summaryEl = document.getElementById('summary');
const tableEl = document.getElementById('album-table');
const gridEl = document.getElementById('album-grid');
let view = 'table';
let albums = [];

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function renderTable() {
  const rows = albums.flatMap(a => a.discs.map(d =>
    `<tr title="${esc(a.path)}"><td>${esc(a.artist)}</td><td>${esc(a.year || '')}</td>` +
    `<td>${esc(a.album)}</td><td>${esc(d.disc || '—')}</td><td class="num">${d.tracks}</td></tr>`));
  document.getElementById('albums').innerHTML = rows.join('');
}

function renderGrid() {
  gridEl.innerHTML = albums.map((a, i) => `
    <div class="card" title="${esc(a.path)}">
      <div data-slot="${i}"><div class="placeholder">♪</div></div>
      <div class="meta">
        <span class="title">${esc(a.album)}</span>
        <span class="sub">${esc(a.artist)}${a.year ? ' · ' + esc(a.year) : ''} · ${a.total_tracks} tracks</span>
      </div>
    </div>`).join('');
  albums.forEach((a, i) => {
    const slot = gridEl.querySelector(`[data-slot="${i}"]`);
    const img = new Image();
    img.className = 'cover';
    img.loading = 'lazy';
    img.alt = a.album;
    img.addEventListener('error', () => slot.innerHTML = '<div class="placeholder">♪</div>');
    slot.replaceChildren(img);
    img.src = '/api/cover?path=' + encodeURIComponent(a.path);
  });
}

function render() {
  tableEl.classList.toggle('hidden', view !== 'table');
  gridEl.classList.toggle('hidden', view !== 'grid');
  summaryEl.textContent = albums.length
    ? `${albums.length} albums, ${albums.reduce((n, a) => n + a.total_tracks, 0)} tracks`
    : '';
  if (view === 'table') renderTable(); else renderGrid();
}

function setView(next) {
  view = next;
  document.getElementById('view-table').classList.toggle('active', view === 'table');
  document.getElementById('view-grid').classList.toggle('active', view === 'grid');
  render();
}

document.getElementById('view-table').addEventListener('click', () => setView('table'));
document.getElementById('view-grid').addEventListener('click', () => setView('grid'));

async function refresh() {
  const [statusRes, albumsRes] = await Promise.all([
    fetch('/api/status'), fetch('/api/albums'),
  ]);
  const status = await statusRes.json();
  const fetched = await albumsRes.json();
  if (JSON.stringify(fetched) !== JSON.stringify(albums)) {
    albums = fetched;
    render();
  }
  convertButton.disabled = status.running;
  if (status.running) {
    const started = new Date(status.started_at * 1000).toLocaleTimeString();
    statusEl.textContent = `Running since ${started}`;
  } else if (status.returncode !== null) {
    const finished = new Date(status.finished_at * 1000).toLocaleTimeString();
    statusEl.textContent = `Last run finished at ${finished} (exit ${status.returncode})`;
  } else {
    statusEl.textContent = 'Idle';
  }
  if (status.log.length) {
    logEl.textContent = status.log.join('\\n');
    logEl.scrollTop = logEl.scrollHeight;
  }
}

convertButton.addEventListener('click', async () => {
  const res = await fetch('/api/convert', {method: 'POST'});
  const data = await res.json();
  if (!res.ok) alert(data.error);
  await refresh();
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/api/albums")
def api_albums():
    if not SOURCE_DIRECTORY.is_dir():
        return jsonify({"error": f"source directory not found: {SOURCE_DIRECTORY}"}), 500
    return jsonify(scan_library())


@app.get("/api/status")
def api_status():
    with lock:
        snapshot = dict(state)
        snapshot["log"] = list(state["log"])
    return jsonify(snapshot)


@app.post("/api/convert")
def api_convert():
    with lock:
        if state["running"]:
            return jsonify({"error": "a conversion is already running"}), 409
        state["running"] = True
        state["started_at"] = time.time()
        state["finished_at"] = None
        state["returncode"] = None
        state["log"] = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_log(f"[{stamp}] Manual conversion started")
    threading.Thread(target=conversion_worker, daemon=True).start()
    return jsonify({"started": True})


@app.get("/api/cover")
def api_cover():
    return cover_response(request.args.get("path", ""))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT)
