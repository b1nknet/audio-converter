#!/usr/bin/env python3
"""Convert a music library with ffmpeg while preserving its tree and metadata.

The script deliberately invokes ffmpeg without a shell.  This makes filenames and
metadata containing spaces, Korean, Japanese, or other Unicode characters safe on
all platforms supported by Python 3.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Add formats here when needed.  Extensions are case-insensitive.
AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".ape", ".caf", ".dff", ".dsf", ".flac",
    ".m4a", ".m4b", ".mp2", ".mp3", ".mpc", ".ogg", ".oga", ".opus",
    ".tak", ".tta", ".wav", ".webm", ".wma", ".wv",
}

FORMATS = {
    "flac": {"extension": ".flac", "audio_args": ["-c:a", "flac", "-compression_level", "8"]},
    # ALAC is conventionally stored in an M4A (MP4) container, not in a .alac file.
    # Leave ``use_metadata_tags`` disabled: it writes FFmpeg's mdta atoms,
    # which some players and tag editors do not display.  The MOV/MP4 muxer
    # therefore writes its standard MP4/M4A ``ilst`` tags instead.
    "alac": {"extension": ".m4a", "audio_args": ["-c:a", "alac"]},
    "mp3": {
        "extension": ".mp3",
        "audio_args": ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3", "-write_id3v1", "1"],
    },
}
# Library locations. Environment variables make the same script usable in
# Docker; local values remain the defaults when no variables are supplied.
# Results are created as FLAC/, ALAC/, and MP3/ directly below OUTPUT_DIRECTORY.
SOURCE_DIRECTORY = Path(os.environ.get("SOURCE_DIRECTORY", Path.home() / "Music"))
OUTPUT_DIRECTORY = Path(os.environ.get("OUTPUT_DIRECTORY", "D:/Music_Compressed"))

# ID3v2.3 text-frame names understood by the output muxers. Unknown text frames
# are retained under their frame ID, and TXXX frames retain their description.
ID3_TEXT_TAGS = {
    "TALB": "album", "TBPM": "bpm", "TCOM": "composer", "TCOP": "copyright",
    "TCON": "genre", "TDRC": "date", "TENC": "encoded_by", "TIT2": "title",
    "TPE1": "artist", "TPE2": "album_artist", "TPOS": "disc", "TPUB": "publisher",
    "TRCK": "track", "TSOA": "album_sort", "TSOP": "artist_sort", "TSOT": "title_sort",
    "TYER": "date",
}


def run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, encoding="utf-8", errors="replace", check=True)


def probe(path: Path, ffprobe: str) -> tuple[list[dict], dict[str, str]]:
    """Return streams and container tags, or empty values when inspection fails."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", os.fspath(path)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        return [], {}
    try:
        details = json.loads(result.stdout)
        return details.get("streams", []), details.get("format", {}).get("tags", {})
    except json.JSONDecodeError:
        return [], {}


def _decode_id3_text(data: bytes, encoding: int, utf16_codec: str | None = None) -> str:
    """Decode one ID3 text value without relying on the system locale."""
    codecs = {0: "latin-1", 1: utf16_codec or "utf-16", 2: "utf-16-be", 3: "utf-8"}
    try:
        return data.decode(codecs[encoding], errors="replace").rstrip("\x00")
    except (KeyError, UnicodeError):
        return ""


def _split_id3_text(data: bytes, encoding: int) -> tuple[bytes, bytes, str | None]:
    """Split an ID3 description/value pair on its encoding-aware terminator."""
    if encoding in (0, 3):
        left, separator, right = data.partition(b"\x00")
        return left, right if separator else b"", None

    codec = "utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be"
    # UTF-16 terminators must be considered at two-byte character boundaries.
    for index in range(0, len(data) - 1, 2):
        if data[index:index + 2] == b"\x00\x00":
            return data[:index], data[index + 2:], codec
    return data, b"", codec


def wav_id3v23_metadata(path: Path) -> dict[str, str]:
    """Read a WAV ``id3 `` chunk and return its ID3v2.3 metadata.

    Some WAVs contain both legacy LIST/INFO chunks (often in a local legacy
    encoding) and a correct Unicode ID3v2.3 chunk. ffmpeg exposes duplicate
    keys from those chunks and maps the first one, which can select mojibake.
    This parser supplies the ID3 values explicitly after ``-map_metadata``.
    """
    try:
        with path.open("rb") as file:
            if file.read(12)[:4] != b"RIFF":
                return {}
            tag_data = b""
            while header := file.read(8):
                if len(header) != 8:
                    return {}
                chunk_id = header[:4]
                chunk_size = int.from_bytes(header[4:], "little")
                if chunk_id.lower() == b"id3 ":
                    tag_data = file.read(chunk_size)
                    break
                file.seek(chunk_size + (chunk_size & 1), os.SEEK_CUR)
    except OSError:
        return {}

    if len(tag_data) < 10 or tag_data[:3] != b"ID3" or tag_data[3] != 3:
        return {}
    tag_size = sum(byte << shift for byte, shift in zip(tag_data[6:10], (21, 14, 7, 0)))
    payload = tag_data[10:10 + tag_size]
    if tag_data[5] & 0x80:  # ID3v2.3 unsynchronisation
        payload = payload.replace(b"\xff\x00", b"\xff")

    position = 0
    if tag_data[5] & 0x40 and len(payload) >= 4:  # extended header
        position = 4 + int.from_bytes(payload[:4], "big")

    metadata: dict[str, str] = {}
    while position + 10 <= len(payload):
        frame_id = payload[position:position + 4].decode("ascii", errors="ignore")
        frame_size = int.from_bytes(payload[position + 4:position + 8], "big")
        frame_flags = payload[position + 8:position + 10]
        position += 10
        if not frame_id.strip("\x00") or frame_size == 0 or position + frame_size > len(payload):
            break
        frame_data = payload[position:position + frame_size]
        position += frame_size
        # Compressed/encrypted frames require codecs or keys and cannot be read safely.
        if frame_flags[1] & 0xC0 or not frame_data:
            continue
        encoding = frame_data[0]
        content = frame_data[1:]
        if frame_id == "TXXX":
            description, value, utf16_codec = _split_id3_text(content, encoding)
            key = _decode_id3_text(description, encoding, utf16_codec)
            value = _decode_id3_text(value, encoding, utf16_codec)
            if key and value:
                metadata[key] = value
        elif frame_id == "COMM" and len(content) >= 4:
            _description, value, utf16_codec = _split_id3_text(content[3:], encoding)
            value = _decode_id3_text(value, encoding, utf16_codec)
            if value:
                metadata["comment"] = value
        elif frame_id.startswith("T"):
            value = _decode_id3_text(content, encoding)
            if value:
                metadata[ID3_TEXT_TAGS.get(frame_id, frame_id)] = value
    # A few WAV encoders place UTF-16 terminator bytes inside text values.
    # Windows rejects a subprocess argument containing NUL, and a leading BOM
    # is not part of the tag's displayed value. Remove both before using these
    # values in ffmpeg's ``-metadata key=value`` arguments.
    return {
        key.replace("\x00", "").lstrip("\ufeff"): value.replace("\x00", "").lstrip("\ufeff")
        for key, value in metadata.items()
        if key.replace("\x00", "").lstrip("\ufeff")
    }


def conversion_command(
    source: Path,
    destination: Path,
    output_format: str,
    streams: list[dict],
    ffmpeg: str,
    metadata_overrides: dict[str, str],
) -> list[str]:
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("audio stream not found")

    # Map the first audio stream and every embedded cover image, but not ordinary video.
    mapped = [str(audio["index"])]
    mapped.extend(
        str(s["index"])
        for s in streams
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic")
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", os.fspath(source)]
    for stream_index in mapped:
        command += ["-map", f"0:{stream_index}"]
    command += ["-map_metadata", "0", "-map_chapters", "0"]
    # Some WAV files carry both a standard date and a legacy/custom YEAR tag.
    # Keeping both makes tag editors such as Mp3tag display ``2020\\2020``.
    # The standard date tag is retained; the duplicate YEAR key is cleared.
    command += ["-metadata", "YEAR="]
    command += FORMATS[output_format]["audio_args"]
    # Copy artwork byte-for-byte.  It is mapped only when it is an attached picture.
    if len(mapped) > 1:
        command += ["-c:v", "copy"]
    # These appear after -map_metadata, making the Unicode ID3v2.3 values win
    # over any duplicate legacy WAV LIST/INFO values.
    for key, value in metadata_overrides.items():
        command += ["-metadata", f"{key}={value}"]
    command += [os.fspath(destination)]
    return command


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def elapsed_timestamp(start_time: float) -> str:
    """Format elapsed conversion time as an HH:MM:SS timestamp."""
    elapsed = int(time.monotonic() - start_time)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"


def compact_track_label(tags: dict[str, str], output_format: str) -> str:
    """Return the small, player-oriented label used in progress messages."""
    album = tags.get("album", "Unknown album")
    disc = tags.get("disc", "?").split("/", 1)[0]
    track = tags.get("track", "?").split("/", 1)[0]
    return f"{album} | Disc {disc} | Track {track} | {output_format.upper()}"


def log_progress(
    start_time: float,
    track_number: int,
    total_tracks: int,
    label: str,
    status: str,
    silent: bool,
) -> None:
    """Print progress without exposing full source or destination paths."""
    prefix = f"{elapsed_timestamp(start_time)} {track_number}/{total_tracks}"
    print(prefix if silent else f"{prefix} | {label} | {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a music library and retain folders, tags, chapters, and cover art.")
    parser.add_argument(
        "--format",
        choices=FORMATS,
        action="append",
        help="convert only this format; repeat to select more than one (default: all)",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: ffmpeg)")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable (default: ffprobe)")
    parser.add_argument("-o", "--overwrite", action="store_true", help="replace existing converted files")
    parser.add_argument("-d", "--dry-run", action="store_true", help="show planned conversions without writing files")
    parser.add_argument("-s", "--silent", action="store_true", help="show only elapsed time and track progress")
    args = parser.parse_args()

    if shutil.which(args.ffmpeg) is None or shutil.which(args.ffprobe) is None:
        parser.error("ffmpeg and ffprobe must both be installed and available on PATH")

    source = SOURCE_DIRECTORY.expanduser().resolve()
    output_directory = OUTPUT_DIRECTORY.expanduser().resolve()
    if not source.is_dir():
        parser.error(f"source directory not found: {source}")
    if source == output_directory:
        parser.error("SOURCE_DIRECTORY and OUTPUT_DIRECTORY must be different")
    selected_formats = args.format or list(FORMATS)
    destinations = {output_format: output_directory / output_format.upper() for output_format in selected_formats}

    input_paths: list[Path] = []
    for root, directories, filenames in os.walk(source):
        root_path = Path(root)
        # Avoid reading converted files as new source if OUTPUT_DIRECTORY was
        # intentionally configured inside SOURCE_DIRECTORY.
        if is_within(output_directory, source):
            directories[:] = [d for d in directories if not is_within((root_path / d).resolve(), output_directory)]
        for filename in filenames:
            input_path = root_path / filename
            if input_path.suffix.lower() in AUDIO_EXTENSIONS and not input_path.is_symlink():
                input_paths.append(input_path)

    converted = skipped = failed = 0
    start_time = time.monotonic()
    for track_number, input_path in enumerate(input_paths, start=1):
        relative = input_path.relative_to(source)
        streams, tags = probe(input_path, args.ffprobe)
        if not any(s.get("codec_type") == "audio" for s in streams):
            log_progress(start_time, track_number, len(input_paths), "", "SKIP unreadable/no audio", args.silent)
            skipped += 1
            continue
        metadata_overrides = wav_id3v23_metadata(input_path) if input_path.suffix.lower() == ".wav" else {}
        tags.update(metadata_overrides)
        for output_format in selected_formats:
            output_path = (destinations[output_format] / relative).with_suffix(FORMATS[output_format]["extension"])
            label = compact_track_label(tags, output_format)
            # Validate an existing output rather than trusting its path alone:
            # a truncated file left by an interrupted ffmpeg run is re-created.
            if output_path.exists() and not args.overwrite:
                existing_streams, _ = probe(output_path, args.ffprobe)
                if any(s.get("codec_type") == "audio" for s in existing_streams):
                    log_progress(start_time, track_number, len(input_paths), label, "SKIP already converted", args.silent)
                    skipped += 1
                    continue
                log_progress(start_time, track_number, len(input_paths), label, "RECONVERT invalid existing file", args.silent)
            command = conversion_command(
                input_path, output_path, output_format, streams, args.ffmpeg, metadata_overrides
            )
            log_progress(
                start_time, track_number, len(input_paths), label,
                "PLAN" if args.dry_run else "CONVERT", args.silent,
            )
            if args.dry_run:
                converted += 1
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                run_checked(command)
                converted += 1
            except (subprocess.CalledProcessError, OSError, ValueError) as error:
                failed += 1
                return_code = getattr(error, "returncode", "unable to start")
                log_progress(start_time, track_number, len(input_paths), label, f"FAILED ({return_code})", args.silent)

    if not args.silent:
        print(f"{elapsed_timestamp(start_time)} Done: {converted} converted, {skipped} skipped, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
