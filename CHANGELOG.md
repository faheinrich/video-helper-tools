# Changelog

All notable changes to the two apps and the shared core. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are tagged per app:
`archive-v<version>` for Archive Helper, `analysis-v<version>` for Analysis Helper.

## [Unreleased]

## [0.0.6] and [analysis-v0.0.4] - 2026-11-13

Archive Helper `archive-v0.0.6` and Analysis Helper `analysis-v0.0.4`.

### Archive Helper
- Added: undo and redo for the file actions (replace, swap, move to the trash): Edit menu, Cmd+Z / Shift+Cmd+Z
  (Ctrl+Z / Ctrl+Y on Windows and Linux). The files come back from the trash; when that is no longer possible
  (trash emptied, name taken), nothing changes and the app says why.
- Added: menus in the macOS menu bar while the compressor is open: Edit and Videos (the right-click menu's actions
  for the selected videos, and Scan again, Export list as CSV, Clear cache).
- Added: more status filters: "Done in this session", "Already archived" (both only when the finished videos are of
  both kinds) and "⚠ With problems" (metadata missing, visible blocks, larger than the original). Sorting by status
  puts the videos with problems together, right after the errors, grouped by problem.
- Added: sorting by the preview column goes by brightness (videos without a preview yet at the end); while the
  previews load, a counter next to the search shows how many are there.
- Added: results without stored settings (made by another program or an early version) show what the video itself
  tells, marked with "≈": codec, resolution, frame rate, HDR, and for x265 the CRF and the preset from the options
  x265 writes into the video.
- Fixed: results whose metadata was added later showed "Mac GPU · quality None": that record holds no settings.
- Fixed: a new scan and closing the app no longer wait until the settings of every earlier result are read.
- Fixed: the compression lab showed the result of a variant changed while it was being encoded as current.
- Fixed: when the result could not be loaded, the compare viewer did not play the original either.
- Fixed: when rsync could not be started (not installed, as usually on Windows), the backup page stayed at
  "Running" forever. A stopped backup now shows "Stopped" instead of "Error".
- Changed: the metadata check also compares boxes exiftool has no name for, so metadata of a kind not seen before
  is reported when it is lost.
- Added: a metadata viewer with original and result side by side (right-click > Metadata, or double-click on
  "⚠ Metadaten?"). It marks what is missing, changed or new, hides what compressing changes by its nature, and
  opens on the differences when something is missing; with a search over fields and values. It lists also the
  boxes exiftool has no name for, and on request the data stored with every frame.
- Fixed: iPhone videos filmed in Cinematic mode lost their two Cinematic keys (`cinematic-video`,
  `cinematic-video-intent`) and showed "⚠ Metadaten?". exiftool does not know these keys and so cannot write them;
  movie-level keys it leaves out are now copied byte for byte.

### Analysis Helper
- Fixed: Stop during a batch transcription stopped only the current file; the batch went on with the next one.
  Stopping a single transcription no longer shows an error.
- Fixed: a video without a sound track (or a broken file) did nothing visible in the sync tool on loading, finding
  the shift or saving; it is now reported.
- Fixed: the sync tool's players could freeze the app when they were stopped or moved while playing.
- Fixed: the transcriber could crash right after a file was transcribed (most likely in a batch), because the finished
  transcription was freed while its thread was still ending.

### Development
- The compressor page is split: gui.py (layout, scan, summary), drawer.py, run_control.py, row_actions.py, and
  file_actions.py for the file operations without widgets.
- Core: tool pages can offer menus (`menus()`); move_to_trash returns where the file is in the trash.
- Docs: features, architecture and the state machines with every transition and its tests (`docs/`).
- The coverage badge is back in the README.
- GitHub also tests on Windows.
- Core: `MediaPlayer`, a QMediaPlayer whose play, pause, stop and setPosition let go of Python's GIL. With the plain
  one, Qt's audio thread and the call could wait for each other forever (the test runs that hung on Linux).
- Tests: a hang that holds the GIL, which pytest-timeout cannot end, now ends after 5½ minutes with the stack of
  every thread (faulthandler), shown as annotation on GitHub.

## [analysis-v0.0.3] - 2026-11-11

Analysis Helper `analysis-v0.0.3`. Archive Helper is not released this time.

### Analysis Helper
- Changed: when a transcription replaces an existing EAF file, the earlier one goes to the trash.
- Fixed: dark mode on macOS: the transcriber's panels glared bright grey, and its section titles and the sync tool's waveform
  captions were hard to read.
- Also contains the update check fix of Archive Helper 0.0.4 and the shared dark mode fixes.

### Development
- GitHub: Linux runners are fixed to Ubuntu 24.04 (ubuntu-latest moved to Ubuntu 26, where the release build failed).
  Linux dependencies no longer come from a plain apt run, whose mirrors at times hung for many minutes: ffmpeg from a
  ready-made build, exiftool from its repository, and only the Qt libraries that are missing from GitHub's image.
  Failed tests show their reason as annotations on the run.

## [0.0.5] - 2026-11-11

Archive Helper `archive-v0.0.5`: camera metadata reaches the compressed result. Analysis Helper is not released this time.

### Archive Helper
- New: a video without a capture date of its own (some cameras, screen recordings, messenger videos) gets one written into
  the result: from another date field of the original if it has one, otherwise from the file date. Before, the date only
  lived in the file system and was lost with the first plain copy. Existing capture dates are never changed.
- New: the result records what it was made from (file name, size and format of the original), when, and with which app
  version. The tooltip of the settings column shows it.
- Changed: ffmpeg's container type fields (MajorBrand, MinorVersion, CompatibleBrands) are no longer copied into the
  result as if they were metadata of the camera.
- New: the camera makers' own metadata (Nikon, Olympus, Panasonic, Fujifilm, Canon, Sony's XML and others: camera and lens, focal
  length, aperture, exposure, serial number, picture profile) is copied into the result byte for byte. exiftool can
  read it but cannot write it into videos, so up to now it was lost: the results of Nikon and Olympus videos held
  little more than the date.
- New: after each video the metadata of original and result are compared. What did not come over is listed in the
  status tooltip with "⚠ metadata?" and kept in the result, so the warning is still there later.
- New: tracks that are neither picture nor sound are carried over unchanged: the per-frame metadata of iPhone videos
  (detected faces, orientation, Live Photo data), subtitles, and likewise the GPS and telemetry tracks of other
  cameras. ffmpeg cannot write most of them, so they were lost. A timecode track is written by ffmpeg as before. A
  one-second test run carries no tracks over.
- New: the metadata a picture or sound track carries itself is copied too: newer iPhones keep lens, focal length,
  aperture and maker notes there.
- New: old formats (AVI, AVCHD), whose camera metadata MP4 has no room for: make, model, dates, aperture, exposure,
  ISO, focal length, software and copyright go into standard fields, and all their metadata (with small images such
  as the camera's thumbnail) is kept as text in the result's settings tag.
- New: results made by an earlier version can get the metadata that version did not carry over, from their original,
  without compressing again (`add_missing_metadata`). The picture stays bit for bit the same; the work is done on a
  hidden copy that replaces the result in one step, and the result keeps its file date.
- New: sidecar files of a video (IMG_1.xmp or IMG_1.MOV.xmp, .AAE, Sony's C0001M01.XML, .THM) are copied next to
  the result under its name; an XMP sidecar is also written into the result itself.
- The XMP packet of the original is copied as a whole, so structures exiftool cannot write field by field (e.g. the
  editing history of Adobe programs) come along. Boxes and tracks also go into files whose media data follows the
  moov box (as exiftool writes them); the sample offsets move along.
- The metadata comparison leaves out what compressing changes by its nature: stream details of AVI and AVCHD (codec,
  rates, counts), positions inside the file, and the Photos library signature of iPhone videos, which a compressed
  copy deliberately does not take over so the Photos app does not mistake it for the original.

### Archive Helper: added on 2026-11-13
The 0.0.5 packages were built again from a later commit, with these fixes:
- Fixed: "Stop" in the backup page did nothing while rsync was still starting.
- Fixed: closing the compression lab while a frame was about to load (or an encoding was ending) started ffmpeg for
  the closed window; quitting the app at that moment could make it crash.
- Fixed: cancelling in the compression lab right as an encoding was about to start let that encoding run to its end.
- Fixed (Windows): the settings column stayed empty and missing metadata was not flagged, because exiftool names the
  files with "/" and the stored settings were looked up with "\".
- Fixed (Windows): a folder dropped onto a path field showed "/" instead of the system's "\".
- Fixed (Windows): stopping a run failed with an error, because the unfinished file was still locked by the ffmpeg
  that had just been stopped.
- Fixed (Windows): results kept today's date as creation date; they now get the original's, as on macOS.
- Fixed: the settings drawer grows when its options need more room (with Windows' fonts they were cut off at the
  right).
- Fixed: the comparison player could freeze the app when it was closed, stopped or moved while a video played.

## [0.0.4] - 2026-11-09

Archive Helper `archive-v0.0.4`. Analysis Helper is not released this time and stays at 0.0.2.

### Archive Helper
- New: after a video is finished, its result is checked for visible blocks in flat areas (ffmpeg's `blockdetect` on three
  short parts, compared with the original, at low priority). Results that look worse show "⚠ Blöcke?" in the status with
  the scores in the tooltip, and the end of the run names how many there are.
- New: a checkbox "Ignore hidden files/folders" (on by default) skips everything whose name starts with a dot, and
  hidden folders such as `.hermes` are not even entered. macOS `._` metadata files are always skipped.
- New: several videos can be selected with Cmd/Ctrl-click and Shift-click. The bar below the table and the right-click
  menu then apply "Process next" and the file actions (replace, swap, delete) to all of them, with one confirmation;
  videos an action does not fit (no result yet, being compressed) are left out, and the menu shows how many it affects.
- New: the status of a running video shows in its tooltip how long it has been compressing and about how long it still
  needs (paused time left out); a finished one shows how long it took.
- New: the preview picture has its own column. Drag its edge to make the previews bigger (up to 320 px); the rows grow
  with it, and the width is kept for the next start. Previews are now made at 320 px, so they are made anew once.
- Changed: files the app removes go to the trash instead of being deleted for good: "Move original/result to trash"
  (formerly "Delete"), the original when "Replace original" is used, and the original or an earlier result that a new
  result replaces. If a file cannot be moved to the trash, it is left where it is and the action reports an error.
- Fixed: "Replace original" overwrote the original directly, without the trash, when the result had the same name
  (empty name ending, other folder); another file with the result's name next to the original was overwritten too.
  Both now go to the trash, and only once the copy of the result is complete.
- New: the comparison of original and result can show them in three ways (buttons next to "Reset view", the choice is
  kept): side by side as before; "◧ Slider", one picture with a dividing line to drag, the original left of it and the
  compressed version right of it; and "⇄ Flip", one picture that changes between the two every 2 seconds (also while
  paused), framed in blue for the original and red for the compressed version. Zoom and panning work as before and keep both pictures aligned.
- Fixed: zooming out with the mouse wheel below the starting size only zoomed one of the two compared videos.
- Changed: sorting by file name now goes by sub folder first and then by name, so the videos of a folder stay together
  (and a folder's own videos come before those of its sub folders).
- Changed: the settings drawer groups its options into Search, Encoding and Output, and the search options are always visible.
- Fixed: the number after the savings arrow is now the total size after compression; it showed the amount saved.
- Fixed: dark mode was hard to read on macOS (glaring summary card, invisible captions and placeholders, dim status colours,
  an unavailable Start button that looked like plain text, nearly invisible empty thumbnails).
- Changed: a result that is not smaller no longer gets a `_source` copy of the original when it lies in the same folder as
  the original, and `_source` is now one of the name endings skipped by default, so these copies are not compressed
  again. If you saved your own defaults, add `_source` to the list yourself. Existing `_source` files are left alone.
- Fixed: with FFmpeg 8 or newer the compression lab showed no frames for HDR videos (the colour matrix name `bt2020nc` is no
  longer accepted by the scale filter; `bt2020` works).
- Fixed: the name of a selected video was white on a pale selection, and so unreadable, when the window was not in front.
- Fixed: closing the window while the previews of a large folder were still being made left the process running until
  all of them were done. Waiting previews are now dropped and running ones stopped when the window closes or another
  folder is loaded.
- Changed: previews are made three at a time at low priority, the visible rows first, and none start while a video
  plays in the comparison, so playback is no longer slowed down by them.
- Fixed: sorted by duration, a run started before all durations were read processed the videos without a known
  duration first, so it seemed to skip around in the table. The waiting videos are now put in order again as the
  durations come in.
- Fixed: "Replace original" failed with "are the same file" when the result was written next to the original; it now
  just removes the original there.
- Fixed (probably): the update check could fail silently on a Mac where the bundled Python did not find the system's
  certificate list. The check now uses the certificates shipped with the app (certifi), records any failure, and the
  About dialog says how the check went ("this is the newest version (latest release: …)", "failed: …", "switched off").

### Development
- README: screenshot of Archive Helper.
- Tests: the shared Qt test application cleans up its windows and threads when the session ends. Without it the Python
  process crashed on exit (exit code 139) on macOS when coverage was on, after all tests had passed, and failed the CI job.

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

[Unreleased]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.6...HEAD
[0.0.6]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.5...archive-v0.0.6
[analysis-v0.0.4]: https://github.com/faheinrich/video-helper-tools/compare/analysis-v0.0.3...analysis-v0.0.4
[0.0.3]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.2...archive-v0.0.3
[0.0.2]: https://github.com/faheinrich/video-helper-tools/compare/archive-v0.0.1...archive-v0.0.2
[archive-v0.0.1]: https://github.com/faheinrich/video-helper-tools/releases/tag/archive-v0.0.1
[analysis-v0.0.1]: https://github.com/faheinrich/video-helper-tools/releases/tag/analysis-v0.0.1
