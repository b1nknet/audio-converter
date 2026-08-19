# Audio Converter

`audio_convert.py` recursively converts a music library to FLAC, ALAC, or MP3 with ffmpeg. It recreates the source directory tree and copies stream metadata, chapters, and embedded cover artwork.

It uses Python `Path` objects and `subprocess` argument lists (never a shell command), explicitly reading process text as UTF-8. That preserves Korean, Japanese, and other Unicode filenames safely; ffmpeg transfers source tags directly. MP3 output is written as ID3v2.3, which represents non-Latin tags as UTF-16 for broad player compatibility.

For WAV files that contain both an old `LIST/INFO` chunk and ID3v2.3 metadata, the script explicitly gives ID3v2.3 values priority. This prevents a legacy-encoded `LIST/INFO` value from replacing a correct Korean or Japanese ID3 tag during FLAC/ALAC conversion.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on `PATH`

Windows users can install an FFmpeg build, add its `bin` folder to the `PATH` environment variable, then reopen their terminal. Confirm both commands work before running the script:

```powershell
ffmpeg -version
ffprobe -version
```

## Usage

Set the two paths near the top of `audio_convert.py` before running it:

```python
SOURCE_DIRECTORY = Path("/path/to/original-music")
OUTPUT_DIRECTORY = Path("/path/to/converted-music")
```

`SOURCE_DIRECTORY` is the folder holding original files. `OUTPUT_DIRECTORY` is a separate destination root; the script creates `FLAC`, `ALAC`, and `MP3` folders below it. Both paths can be changed independently and are not supplied on the command line.

On Windows, use a raw string for a drive-letter path so backslashes are interpreted safely:

```python
SOURCE_DIRECTORY = Path(r"D:\Music\Original")
OUTPUT_DIRECTORY = Path(r"E:\Converted Music")
```

```bash
python3 audio_convert.py
```

The portable default configuration uses your home Music folder (for example, `C:\Users\your-name\Music` on Windows):

```text
~/Music/
├── Original/  # source files (including nested artist/album folders)
└── Converted/
    ├── FLAC/  # generated FLAC files
    ├── ALAC/  # generated ALAC-in-M4A files
    └── MP3/   # generated MP3 files
```

The script creates the three output folders as needed and recreates every folder below `Original` in each one. Before every conversion it checks whether the exact target file already exists *and* is readable as an audio file. Valid existing files are reported as `SKIP already converted` and are not re-encoded; a corrupt or incomplete result is automatically converted again. This also makes it safe to run again after an interruption.

ALAC is stored in the standard `.m4a` container. Its metadata is written as standard MP4/M4A (`ilst`) tags, rather than FFmpeg-specific `mdta` tags, so conventional music players and tag editors can read it. Use `-o` or `--overwrite` to replace existing results, `-d` or `--dry-run` to inspect planned work, or `--format mp3` to generate only MP3. `--format` may be repeated, for example `--format flac --format alac`.

While it runs, the converter shows elapsed time, the current track out of the total, album, disc, track, output format, and status. It never prints full source or destination paths in progress messages:

```text
[00:00:03] 12/99 | Example Album | Disc 1 | Track 12 | ALAC | CONVERT
```

Use `-s` or `--silent` to display only elapsed time and track progress. Short flags may be combined, so `-ods` is equivalent to `-o -d -s`.

## Docker scheduling

The included `docker-compose.yml` runs the converter on a five-field cron schedule. It contains FFmpeg, Python, and the script; your music stays on the host and is mounted into the container. By default it converts the project's `music_original` folder into `music`, runs once at container startup, and then runs daily at 03:00 in `Asia/Seoul`.

For a real library, copy `.env.example` to `.env` and set the two host paths. Use forward slashes on Windows:

```dotenv
HOST_SOURCE_DIRECTORY=C:/Users/your-name/Music
HOST_OUTPUT_DIRECTORY=D:/Music_Compressed
CRON_SCHEDULE=0 3 * * *
TZ=Asia/Seoul
CONVERTER_ARGS=--format alac
```

Start the scheduled container with:

```powershell
docker compose up -d --build
```

Watch its logs with `docker compose logs -f`, and stop it with `docker compose down`. Set `RUN_ON_START=false` if the first conversion should wait for its scheduled time. Docker Desktop must be allowed to access the selected Windows drives.

The converter selects the first audio stream from each input and carries over every attached-picture stream. It intentionally does not carry ordinary video streams from music-video files.
