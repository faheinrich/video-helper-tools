# Analysis Helper

Desktop app (PySide6) for working with the content of recordings.

- **Sync Videos** — aligns two recordings by their audio (FFT cross-correlation), with waveform view and preview.
  The Info button explains the algorithm and shows its measured accuracy
  (`python -m analysis_helper.sync.benchmark`, results in `docs/sync-benchmark.json`).
- **Transcribe Audio** — Whisper transcription of speech segments found by Silero VAD, written to ELAN `.eaf` files.
  Runs against a local or remote FastAPI server.

See [docs/VIDEO_SYNC_README.md](docs/VIDEO_SYNC_README.md) and [docs/TRANSCRIPTION_README.md](docs/TRANSCRIPTION_README.md).

## Run

```bash
uv sync --package analysis-helper    # from the repository root
uv run --package analysis-helper python analysis-helper/main.py
```

The transcription workflow also has command-line entry points:

```bash
uv run --package analysis-helper run-whisper-server
uv run --package analysis-helper annotate-to-elan --video_path YOURVIDEOFILE
```

FFmpeg must be installed. Because of PyTorch and OpenCV the app is large.

Settings are stored per user, e.g. `~/Library/Application Support/Analysis Helper/settings.json` on macOS. The
transcriber keeps its own settings (server, model, language, VAD and tier name) in `~/.whisper_gui_settings.json`.

## Example media

`example_resources/` holds two short sample videos (also used by the tests) and audio for the shift benchmark:
`wilson-address-1913.ogg` is a public-domain recording of a speech by Woodrow Wilson (1913), from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Wilson_-_Address_to_the_American_Indians_edit.ogg);
`longer-test-audio.flac` was recorded for this project.

## Tests

```bash
uv run --package analysis-helper pytest analysis-helper/tests
```

## Build a standalone app

```bash
cd analysis-helper && ./build.sh
```

Releases are built by the GitHub workflow when a tag `analysis-v<version>` is pushed.
