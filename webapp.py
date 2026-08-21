#!/usr/bin/env python3
"""Web interface listing source albums with a manual full-library conversion button."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

SOURCE_DIRECTORY = Path(os.environ.get("SOURCE_DIRECTORY", "/music/source"))
CONVERTER_SCRIPT = Path(os.environ.get("CONVERTER_SCRIPT", "/app/audio_convert.py"))
CONVERTER_ARGS = os.environ.get("CONVERTER_ARGS", "")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))
LOG_LIMIT = 300

AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".ape", ".caf", ".dff", ".dsf", ".flac",
    ".m4a", ".m4b", ".mp2", ".mp3", ".mpc", ".ogg", ".oga", ".opus",
    ".tak", ".tta", ".wav", ".webm", ".wma", ".wv",
}

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


def list_albums() -> list[dict]:
    albums = []
    for root, _directories, filenames in os.walk(SOURCE_DIRECTORY):
        root_path = Path(root)
        tracks = [f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTENSIONS]
        if not tracks:
            continue
        relative = root_path.relative_to(SOURCE_DIRECTORY)
        albums.append({
            "album": relative.as_posix() or "(root)",
            "tracks": len(tracks),
        })
    albums.sort(key=lambda item: item["album"].lower())
    return albums


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audio Converter</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; }
  #convert { font-size: 1rem; padding: .5rem 1.2rem; cursor: pointer; }
  #convert:disabled { opacity: .5; cursor: not-allowed; }
  #status { margin-left: .8rem; }
  pre { background: rgba(127,127,127,.12); padding: .8rem; border-radius: 6px;
        max-height: 320px; overflow-y: auto; font-size: .8rem; white-space: pre-wrap; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  td, th { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid rgba(127,127,127,.3); }
  td:last-child, th:last-child { text-align: right; }
</style>
</head>
<body>
<h1>Audio Converter</h1>
<p>
  <button id="convert">Convert entire library</button>
  <span id="status"></span>
</p>
<pre id="log">No conversion has run in this session yet.</pre>
<table>
  <thead><tr><th>Album</th><th>Tracks</th></tr></thead>
  <tbody id="albums"></tbody>
</table>
<script>
const button = document.getElementById('convert');
const statusEl = document.getElementById('status');
const logEl = document.getElementById('log');

async function refresh() {
  const [statusRes, albumsRes] = await Promise.all([
    fetch('/api/status'), fetch('/api/albums'),
  ]);
  const status = await statusRes.json();
  const albums = await albumsRes.json();
  document.getElementById('albums').innerHTML = albums.map(a =>
    `<tr><td>${a.album}</td><td>${a.tracks}</td></tr>`).join('');
  button.disabled = status.running;
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

button.addEventListener('click', async () => {
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
    return jsonify(list_albums())


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT)
