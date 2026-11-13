# Features

Two desktop apps (macOS, Windows, Linux), each with a start page and two tools. Both apps:
- run in German or English;
- follow light and dark mode;
- check GitHub for a newer release of themselves at start;
- ask before quitting while a job runs.

- [Archive Helper](#archive-helper)
  - [Compress & Archive](#compress--archive)
  - [Backup (rsync)](#backup-rsync)
- [Analysis Helper](#analysis-helper)
  - [Sync Videos](#sync-videos)
  - [Transcribe Audio](#transcribe-audio)
- [Both apps](#both-apps)

---

## Archive Helper

### Compress & Archive

Compresses a folder of videos (from a camera, a phone, an Apple Photos export) into HEVC files that take a
fraction of the space. It keeps all the metadata, so the results can replace the originals in an archive.

#### Choosing videos

- **Source and target folder**: the target gets the same subfolders, or one flat folder ("Don't keep subfolders").
  - Without a target, each result goes next to its original.
  - Fields accept `~` and dropped folders.
- **Drop files or folders** onto the window. A drop zone tells whether they become the source or the target.
  Dropped single files form a hand-picked list ("Back to folder mode" returns to the folder).
- **Result names**: the postfix (default `_archived`), editable. Rules:
  - Names that would collide (same name, other extension; upper and lower case) get their own name.
  - A collision that cannot be resolved is marked "Duplicate target name".
  - A target that would be the source itself is marked "Source = target!". Starting then needs a confirmation
    with a delayed button, and the original is replaced and goes to the trash.
- **Excludes** (patterns) and "Ignore hidden files/folders".
- The folder is scanned again automatically when the folders or name settings change.

#### The list

- **Columns**:

  | Column | Shows |
  |---|---|
  | Preview | a picture of the video; sorting by it goes by brightness |
  | File | the file name and its folder |
  | Duration | the length |
  | Original | the size of the original |
  | Result | the size of the result and the saving ("38 % smaller"); larger results in orange |
  | Settings | how the result was made |
  | Status | colored labels |

- Every column sorts, and the processing order follows the sort order.
- Columns can be dragged wider, and the preview column makes the pictures bigger.
- **Settings column**:
  - It shows the settings stored in the result.
  - For results made by other programs or early versions, it shows what the video itself tells, marked "≈":
    codec, resolution, frame rate, HDR, and for x265 the CRF and the preset.
- **Status filters** (chips with counts):
  - All, Planned, Running, Done.
  - "Done in this session" and "Already archived", shown only when both kinds exist.
  - "⚠ With problems": metadata missing, visible blocks, or larger than the original.
  - Skipped, Errors.
  - A search box for file names and folders.
- **Sorting by status** puts errors first, then the videos with problems grouped by problem.
- **Previews and durations** load in the background (a counter shows how many previews are there) and are cached.
- **Tooltips** of the status: running time and time left, how long it took, why it was skipped or failed, what
  metadata is missing, the block check's score.
- **Summary**: number and size of the videos, the saving so far, an estimated duration (learned from earlier
  runs with the same settings), and the free space at the target.
- **Export list as CSV**.

#### Settings (the drawer)

- **Encoder**:
  - CPU (libx265): CRF and preset ultrafast … veryslow.
  - Mac GPU (VideoToolbox): quality value.
  - Presets "small", "archive" (CRF 20 / quality 65), "high", with a description of what the value means.
- **Limits**: resolution (long side 3840, 2560, 1920, 1280, 720 px) and frame rate (60, 50, 30, 25, 24 fps).
  They only ever reduce.
- **Audio**: AAC copied 1:1 when every track is AAC, otherwise AAC 128 kbit/s. All audio tracks are kept, with
  their names.
- **HDR** stays HDR: 10-bit HEVC with the original's color tags (HLG, PQ). SDR stays 8-bit.
- **Output** is always `hvc1`-tagged HEVC in MP4, which Apple Photos imports.
- Parallel videos, Low priority, Test run (first second only), Overwrite existing results, Keep computer awake.
- **Save as default** remembers the settings.

#### A run

- **Start / Pause / Stop**:
  - Pause freezes the running encodes; they continue where they stopped.
  - Stop kills them and deletes their unfinished files.
  - Errors are retried at the next start.
- **"Process next"** moves videos to the front of the queue, also while running.
- **Safety**:
  - A result is written as `NAME.partial.mp4` and only gets its name when complete.
  - An existing file is never overwritten: it goes to the trash.
  - A result is never deleted for good.
- **Larger than the original**: the result is kept, and the original is copied next to it as `NAME_source.ext`
  when Apple Photos can import it.
- **Block check** after each video: compares flat areas of the result with the original and warns about visible
  compression blocks ("⚠ blocks?").
- **Notification** when a run ends by itself.

#### Metadata

The result keeps the metadata of the original, so an archive loses nothing but resolution:
- **Everything exiftool can copy**: EXIF, XMP (as a whole, including editing history), QuickTime keys and user
  data, GPS, dates.
- **Camera makers' boxes, byte for byte**: Nikon, Olympus, Panasonic, Sony (XML), Canon, Fujifilm, GoPro and any
  other box of the camera.
- **Data tracks**:
  - Apple's per-frame camera data and Cinematic depth track, GoPro GPS, timecode, subtitles.
  - They are moved over with their samples.
- **The video track's own keys** (iPhone lens, focal length, aperture) and movie keys exiftool does not know
  (Cinematic mode).
- **Old formats** (AVI, AVCHD):
  - Their values go into standard fields where there is one.
  - All of it is also kept as text in the result.
- **Capture date**: when the original has none inside, the file date is written in (and the record says so).
- **File times** of the original; **sidecar files** (`.xmp`, `.aae`, `.xml`, `.thm`) are copied under the
  result's name.
- **A record of how the result was made** (stored in the result as XMP):
  - encoder and settings, the app version, the date;
  - the original's name, size and format;
  - what metadata could not be carried over.
- **The check**: afterwards the original and the result are compared field by field, including boxes exiftool
  has no name for. Anything missing marks the video "⚠ Metadaten?".
- **Metadata viewer** (right-click > Metadata, or double-click on "⚠ Metadaten?"):
  - shows the original and the result side by side;
  - marks what is missing, changed, new, kept elsewhere or changed by compressing;
  - has a search and "Only differences", and can also read the data stored with every frame.

#### Per video (right-click, or the bar under the list)

- **Compare**: original and result played in sync.
  - Views: side by side, a slider over both, or flipping every two seconds.
  - Zoom and pan (in step), rotate, scrub, play from the start.
- **Inspect different compressions** (the compression lab):
  - Encodes a part of the video with several settings side by side.
  - Shows each variant's size, time, and an estimate for the whole video and the whole folder.
  - Frames can be compared at any zoom and position.
  - A changed variant is marked and encoded again alone.
  - "Use these settings" hands a variant to the drawer.
- **Metadata**, **Log** (ffmpeg's output), **Show original / result** in the file manager.
- **File actions**, also for several videos at once, always via the trash:
  - replace the original with the result;
  - swap original and result;
  - move the original or the result to the trash.

### Backup (rsync)

Mirrors a folder to a backup location with
`rsync -av --del --partial --progress --modify-window=1 --size-only`:
- It asks first, because files no longer in the source are deleted at the target.
- It shows the number of files, the progress, the elapsed time and rsync's output.
- Stop, Keep computer awake, and a notification at the end.
- It reports when rsync is not installed.

---

## Analysis Helper

### Sync Videos

Finds the time shift between two recordings of the same scene by their sound (FFT cross-correlation), shows
both waveforms, and plays the two videos in sync:
- The shift can be adjusted by hand.
- The scrubber moves both videos.
- Volume and mute.
- **Save** trims the video that starts earlier, so both start at the same moment.

A benchmark (`python -m analysis_helper.sync.benchmark`) measures the shift finder against a known truth under
noise, filters, reverb, codecs and drift.

### Transcribe Audio

Transcribes the speech of a video into an ELAN annotation file (`.eaf`):
1. Finds the speech segments (Silero VAD).
2. Sends each segment to a Whisper server.
3. Writes the text into a tier.

- **Waveform** with the speech segments and the text as it arrives.
- **Settings**: padding, VAD threshold, minimum speech length, tier name, where the `.eaf` goes. An existing file
  goes to the trash.
- **Whisper server**:
  - A running server, or a local one started by the app: Whisper model tiny … large-v3, or any Hugging Face
    model; and the language.
  - Its log can be shown.
- **Batch**: all or the selected media files of a folder.
  - Each is marked Done, Failed or Stopped.
  - A failed file does not stop the batch; Stop does.

---

## Both apps

- **Start page** with the tools, the version, the language, and a hint when a newer release exists.
- **About** dialog with the result of the update check.
- **Settings and caches** per app in the system's places (see [architecture](architecture.md#data-on-disk)).
- **Releases** for macOS (Apple Silicon), Windows and Linux. The builds are unsigned (see the release notes for the
  first start).
