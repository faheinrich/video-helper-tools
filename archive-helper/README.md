# Archive Helper

Desktop app (PySide6) to shrink a video collection for the archive and to back up folders.

- **Compress & Archive** — batch-compress videos with FFmpeg and keep their metadata (ExifTool): capture date, GPS, camera data, file times.
  - HEVC output tagged `hvc1`, so it imports into Apple Photos
  - optional hardware encoding, resolution and frame-rate limits, HDR sources stay HDR
  - a result larger than the original is kept, and the original is copied next to it as `NAME_source.ext` when
    Apple Photos can import it; a result that would replace the original only does so when it is smaller
  - live progress, side-by-side comparison of original and result, CSV export
  - editable result postfix (default `_archived`) and a list of name endings to skip in the source folder
- **Backup (rsync)** — a GUI around `rsync` for mirroring folders to a drive, with progress.

See [docs/COMPRESSION_README.md](docs/COMPRESSION_README.md) for details.

## Run

```bash
uv sync --package archive-helper     # from the repository root
uv run --package archive-helper python archive-helper/main.py
```

FFmpeg, ffprobe and ExifTool must be installed (`brew install ffmpeg exiftool` on macOS;
`sudo apt install ffmpeg libimage-exiftool-perl` on Debian/Ubuntu).

Settings and caches are stored per user, e.g. `~/Library/Application Support/Archive Helper/settings.json` and
`~/Library/Caches/Archive Helper/` on macOS. Settings of the former "Video Helper Tools" app are copied on first start.

## Tests

```bash
uv run --package archive-helper pytest archive-helper/tests
```

## Build a standalone app

```bash
cd archive-helper && ./build.sh        # or: uv run --package archive-helper python build.py
```

The app ends up in `archive-helper/dist`. Releases are built by the GitHub workflow when a tag `archive-v<version>` is pushed.
