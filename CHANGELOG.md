# Changelog

All notable changes to the two apps and the shared core. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are tagged per app:
`archive-v<version>` for Archive Helper, `analysis-v<version>` for Analysis Helper.

## [Unreleased]

### Development
- Tests: the shared Qt test application cleans up its windows and threads when the session ends. Without it the Python
  process crashed on exit (exit code 139) on macOS when coverage was on, after all tests had passed, and failed the CI job.

### Both apps
- Fixed (probably): the update check could fail silently on a Mac where the bundled Python did not find the system's
  certificate list. The check now uses the certificates shipped with the app (certifi), records any failure, and the
  About dialog says how the check went ("this is the newest version (latest release: …)", "failed: …", "switched off").

## [0.0.3] - 2026-10-21

Archive Helper `archive-v0.0.3`. Analysis Helper has no changes of its own and stays at 0.0.2.

### Archive Helper
- New: **Inspect different compressions.** A button next to "Process next" (and an entry in the row menu) opens the
  compression lab for the selected video. Several variants (three at first, starting with Small, Archive and High quality
  and the drawer's other settings; "+ Variant" adds up to eight, the view scrolls sideways) are encoded for a part of the
  video: the first 10 seconds by default, any start and length can be chosen. Each variant has its own progress bar, the
  time left is estimated, and "Cancel all" stops everything. Every variant shows the size of the part, the estimated size
  of the whole video and the encoding time. One frame of the original and of every variant is shown side by side, fitted
  or at 100 % / 200 %, and dragging one moves all views. HDR (HLG) frames are decoded with the BT.2020 matrix; there is no
  tone mapping, so they look a little flat. "Use these settings" puts a variant into the drawer. Not yet: playback.
  Zoom has − / + and 50 % / 100 % / 200 %, a ⟳ button turns all views, and the frame can be moved by −10 … +10 frames.
  Only new or changed variants are encoded again (a changed variant is marked until you press "Encode"); frames of
  portrait phone clips are shown upright and unsqueezed; the original shows its frame rate. With the queue of the main
  page each variant also shows how long the whole folder would take.
- New sample for tests and for trying it out: `archive-helper/example_resources/hdr-gradient-hlg.mov` (HLG, BT.2020,
  10 bit, moving colour gradients and grey ramps where banding shows first; `tests/hdr_samples.py` builds it).
- Internal: the ffmpeg call is built by `ArchiveWorker.build_ffmpeg_command`, used by the real run and the lab alike.
- New: **Estimated time** in the summary: how long the queue will take, from the encodes of earlier runs and of the
  compression lab with the same encoder, preset, resolution and frame-rate limit (a cautious guess if only another number
  of parallel videos was measured). Videos whose length is not known yet count like the average.
- Fixed: in the compare viewer the Rotate button is a small ⟳ in the title row; a turned landscape clip is fitted as the
  portrait picture it now is, and "Reset view" keeps the rotation.

## [0.0.2] - 2026-10-21

Archive Helper `archive-v0.0.2`, Analysis Helper `analysis-v0.0.2`.

### Both apps
- The overview page shows the version at the bottom left. At start the app asks GitHub once (in the background, no
  other data is sent) whether a newer release of the app exists and then shows "New version vX.Y.Z available" in
  orange next to it, linking to the release. Set `VIDEO_HELPER_TOOLS_NO_UPDATE_CHECK=1` to switch the check off.
- The About dialog links to the repository.
- Fixed: the tool names on the overview page are centred below their icons (a wide name pushed the icon aside).
- macOS packages are named `...-macOS-silicon.zip` (Apple Silicon only); the release notes now describe how to open
  an unsigned app on macOS 15 and later (System Settings > Privacy & Security > Open Anyway).

### Archive Helper
- New: **Pause / Resume** for a running compression (the encodes are frozen, paused time does not count for the
  estimate); encodes run with lower priority by default (setting "Low priority" under Advanced).
- New: a checkbox "Keep computer awake" next to Start prevents sleep during a compression or backup.
- New: when a run or backup is finished (not when you stopped it) the Dock icon bounces and a notification appears if the
  window is not in front.
- Changed: the savings read "89 % smaller → 4.36 GB" instead of a negative percentage; long paths in the folder fields
  are shortened in the middle (full path as tooltip).
- New: **source = target is never silent.** Such a video stays in the list with a red "Source = target!" status; before
  the start a warning (confirm button locked for 5 seconds) says that the originals will be replaced. Results are written
  to a `.partial` file first and renamed when complete, so an abort never destroys an existing file; a replacement that is
  not smaller keeps the original.
- Changed: when two sources would get the same target name (`test.mov` and `test.mp4`), both keep their extension in
  the name (`test.mov_archived.mp4`, `test.mp4_archived.mp4`).
- "Keep computer awake" is off by default.
- New: **drag and drop.** Dragging files over the window shows two big areas that do not overlap: Source (the list and
  everything left of the settings) and Target (the settings column), each saying what a drop would do. A folder on
  Source sets the source folder; video files, a single file or several folders on Source become a hand-picked list
  ("Back to folder mode" returns); a folder (or the folder of a file) on Target sets the target. For hand-picked videos
  the target is emptied: every result goes next to its original unless you choose one folder. Same name rules and the
  same source = target warning as in folder mode. Nothing can be dropped while a run is going.
- New: the target folder is checked before the start ("Is the drive connected?"), and a video that cannot be started
  is shown as a red error with the reason instead of "Skipped".
- Fixed: on macOS the native window title was drawn over the back button in the title bar and probably swallowed
  clicks on it. The native title is left empty there.
- Fixed: the quality hint under the Small / Archive / High buttons changed its length and moved the controls below it
  while the CRF slider was dragged. The hint now has a fixed height.

### Analysis Helper
- Fixed: the same title bar problem as in Archive Helper (shared window shell).

### Development
- Tests no longer play audio: the audio output is silent but still reports the volume and mute state that the code sets.
- Tests no longer run again for release tags; every test has a 300 s timeout and every CI job a 25 minute limit,
  after a test hung once on Linux.
- Added this changelog; removed the outdated roadmap.

## [archive-v0.0.1] and [analysis-v0.0.1] - 2026-10-21

The former single app "Video Helper Tools" is split in two apps that share the package `core`
(window shell, translations, styles, app data paths). The repository is a uv workspace with one lock file.
Each app now keeps its own settings and caches (`Archive Helper`, `Analysis Helper` in the per-user folders);
settings of the former app are copied once on first start.

### Archive Helper
- **Compress & Archive**: batch compression with FFmpeg (HEVC tagged `hvc1` for Apple Photos, optional VideoToolbox),
  metadata kept with ExifTool (capture date, GPS, camera data) and the file creation time copied; all audio streams and
  their names are kept; HDR sources stay HDR; resolution and frame-rate limits; results that would be larger than the
  original are skipped.
- Table view with auto-scan, summary, settings drawer, row numbers in processing order, draggable columns,
  "process next", replace / swap / delete actions, CSV export.
- Compare viewer with synchronised players, timeline and zoom; can also show just the original.
- The settings used are stored inside each result and shown in the table.
- Editable result postfix and a list of name endings to skip in the source folder; warning for videos that would get the
  same target name.
- Durations are read in parallel and cached; thumbnails are cached per file version, pruned after 90 days and can be
  cleared from the menu. The start button shows why it is disabled.
- **Backup (rsync)**: GUI around `rsync` with progress.
- German and English interface.

### Analysis Helper
- **Sync Videos**: shift between two recordings from their audio by FFT cross-correlation (linear, so shifts larger than
  half the signal length work), with waveform view and preview. The Info button explains the algorithm and shows its
  measured accuracy; `python -m analysis_helper.sync.benchmark` reproduces it with synthetic ground truth.
- **Transcribe Audio**: Whisper transcription of speech segments found by Silero VAD, written to ELAN `.eaf` files;
  local or remote server, the local server starts from the app bundle.
- German and English interface.

### Releases
- GitHub workflow builds macOS, Linux and Windows packages per app when its tag is pushed; the tag must match the
  version in the app's `pyproject.toml`.

[Unreleased]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.3...HEAD
[0.0.3]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.2...archive-v0.0.3
[0.0.2]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.1...archive-v0.0.2
[archive-v0.0.1]: https://github.com/faheinrich/video-helper-tools/releases/tag/archive-v0.0.1
[analysis-v0.0.1]: https://github.com/faheinrich/video-helper-tools/releases/tag/analysis-v0.0.1
