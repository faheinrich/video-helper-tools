import os
import sys
import json
import subprocess
import re
import hashlib
import time
from pathlib import Path

from video_helper_core.i18n import tr
from video_helper_core.paths import duration_cache_file, speed_cache_file, thumbnail_dir

def format_size(size_bytes):
    is_negative = size_bytes < 0
    size_bytes = abs(size_bytes)
    
    unit_found = 'B'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            unit_found = unit
            break
        size_bytes /= 1024.0
    else:
        unit_found = 'TB'
    
    prefix = "-" if is_negative else ""
    return f"{prefix}{size_bytes:.2f} {unit_found}"

DEFAULT_POSTFIX = "_archived"
DEFAULT_EXCLUDES = "_archived, _compressed, _source"


def clean_postfix(text):
    """The text appended to result names; path separators would let it escape the target folder."""
    return re.sub(r'[\\/:\x00-\x1f]', "", text).strip()


VIDEO_SUFFIXES = (".mov", ".mp4", ".avi", ".mts", ".ogv", ".m4v", ".mkv")


def collect_videos(paths):
    """Video files among dropped paths: files are taken as they are, folders are searched. Returns
    (sorted unique videos, number of dropped files that are no videos)."""
    videos, others = {}, 0
    for path in map(Path, paths):
        if path.is_dir():
            for found in path.rglob("*"):
                if found.is_file() and not found.name.startswith(".") and found.suffix.lower() in VIDEO_SUFFIXES:
                    videos[path_key(found)] = found
        elif path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.startswith("."):
            videos[path_key(path)] = path
        else:
            others += 1
    return sorted(videos.values(), key=lambda p: str(p).lower()), others


def common_parent(paths):
    """The deepest folder containing all the given files."""
    paths = [Path(p) for p in paths]
    if not paths:
        return Path(".")
    return Path(os.path.commonpath([str(p.parent) for p in paths]))


def path_key(path):
    """Paths with the same key name the same file on a case-insensitive file system (macOS, Windows)."""
    return os.path.normcase(str(path)).lower()


def same_file(a, b):
    """True when both paths are the same file: equal after case folding, or the same inode (links)."""
    if path_key(a) == path_key(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def partial_path(dst):
    """Where a result is written first; it only gets its real name once it is complete."""
    dst = Path(dst)
    return dst.with_name(f"{dst.stem}.partial{dst.suffix}")


def parse_excludes(text):
    """Name endings (without extension, case-insensitive) of source videos to skip."""
    return tuple(part.lower() for part in re.split(r"[,;\s]+", text) if part)


THUMBNAIL_WIDTH = 320   # sharp up to the widest preview column; part of the cache key, so older small ones are made anew


def get_thumbnail_path(video_path):
    # Size and mtime are part of the key so a replaced file does not keep showing the old picture.
    try:
        stat = Path(video_path).stat()
        stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        stamp = ""
    video_hash = hashlib.md5(f"{video_path}|{stamp}|{THUMBNAIL_WIDTH}".encode()).hexdigest()
    return thumbnail_dir() / f"{video_hash}.jpg"


THUMBNAIL_MAX_AGE_DAYS = 90


def prune_thumbnails(max_age_days=THUMBNAIL_MAX_AGE_DAYS):
    """Deletes thumbnails not written for a long time (changed or deleted videos leave orphans); returns the count."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for thumb in thumbnail_dir().glob("*.jpg"):
        try:
            if thumb.stat().st_mtime < cutoff:
                thumb.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def clear_caches():
    """Deletes all thumbnails and the duration cache; returns the number of bytes freed."""
    freed = 0
    for path in [*thumbnail_dir().glob("*.jpg"), duration_cache_file()]:
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
        except OSError:
            pass
    return freed

def generate_thumbnail(video_path, output_path, cancelled=None):
    """Writes one frame as JPEG; returns True on success. Runs at low priority so playback and
    the GUI stay smooth, and stops at once when `cancelled` (a threading.Event) is set."""
    # One second in avoids black first frames; clips shorter than that fall back to the
    # first frame. Seeking before -i jumps to a keyframe instead of decoding up to it.
    output_path = Path(output_path)
    low_priority = {"creationflags": 0x00004000} if sys.platform == "win32" else {"preexec_fn": lambda: os.nice(10)}
    for offset in ("1", "0"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", offset, "-i", str(video_path),
               "-frames:v", "1", "-an", "-sn", "-vf", f"scale={THUMBNAIL_WIDTH}:-2", str(output_path)]
        if cancelled is not None and cancelled.is_set():
            return False
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **low_priority)
        except OSError:
            return False
        deadline = time.monotonic() + 60
        while True:
            try:
                process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if (cancelled is not None and cancelled.is_set()) or time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    output_path.unlink(missing_ok=True)  # may be half written
                    return False
        if output_path.exists() and output_path.stat().st_size > 0:
            return True
    return False

def format_duration(seconds):
    if not seconds or seconds == "wird geladen...": return "⏱️ --:--"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"⏱️ {mins:02d}:{secs:02d}"

def check_dependencies():
    missing = []
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception:
        missing.append("ffmpeg")
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
    except Exception:
        missing.append("ffprobe")
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except Exception:
        missing.append("exiftool")
    return missing

def open_in_finder(file_path):
    path = Path(file_path)
    if path.exists():
        subprocess.run(["open", "-R", str(path)])
    else:
        if path.parent.exists():
            subprocess.run(["open", str(path.parent)])

def get_resolution_and_fps(file_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0", str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = result.stdout.strip()
        if not output: return None, None, None
        
        parts = output.split(',')
        if len(parts) >= 3:
            width = parts[0]
            height = parts[1]
            fps_str = parts[2]
            fps = None
            if '/' in fps_str:
                num, den = fps_str.split('/')
                if float(den) > 0:
                    fps = round(float(num) / float(den), 2)
            else:
                fps = round(float(fps_str), 2)
            return width, height, fps
        return None, None, None
    except Exception:
        return None, None, None

def _probe(file_path):
    """Return (format_info, first real video stream, first audio stream), or None if unreadable."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,codec_tag_string,r_frame_rate,"
        "color_transfer,color_primaries,color_space:stream_disposition=attached_pic",
        "-of", "json", str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    streams = info.get("streams", [])
    # Cover art is reported as a video stream; skip it.
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return info.get("format", {}), video, audio


def get_audio_streams(file_path):
    """(codec_name, name tag) for every audio stream, in file order; [] if unreadable or without audio.

    The name tag is the stream name some recorders write (e.g. ``simple_aac_recording0``); it is not kept when a file is
    re-muxed unless it is set again."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_name:stream_tags=name",
        "-of", "json", str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return []
    return [(s.get("codec_name"), (s.get("tags") or {}).get("name")) for s in streams]


# Codec/tag combinations Apple Photos imports (hev1-tagged HEVC is rejected).
PHOTOS_VIDEO = {("hevc", "hvc1"), ("h264", "avc1")}
PHOTOS_AUDIO = {"aac", "alac"}


def is_photos_compatible(file_path):
    probe = _probe(file_path)
    if probe is None:
        return False
    _, video, audio = probe
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".mov", ".mp4", ".m4v") or not video:
        return False
    if (video.get("codec_name"), video.get("codec_tag_string")) not in PHOTOS_VIDEO:
        return False
    if audio is None:
        return True
    codec = audio.get("codec_name", "")
    # Uncompressed PCM (iPhone "lpcm") is only valid in a QuickTime container.
    return codec in PHOTOS_AUDIO or (codec.startswith("pcm_") and suffix == ".mov")


def get_video_info(file_path):
    """Return (duration_s, fps, audio_codec) of the first real video/audio stream; None where unknown."""
    probe = _probe(file_path)
    if probe is None:
        return None, None, None
    fmt, video, audio = probe

    duration = fmt.get("duration")
    duration = float(duration) if duration not in (None, "N/A") else None

    fps = None
    if video:
        num, _, den = video.get("r_frame_rate", "0/0").partition("/")
        if den and float(den) > 0:
            fps = float(num) / float(den)

    return duration, fps, audio.get("codec_name") if audio else None

def parse_ffmpeg_time(log_line):
    match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", log_line)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        return hours * 3600 + minutes * 60 + seconds
    return None


# Settings a result was compressed with are stored in the file itself (custom XMP tag),
# so they survive moving the file and restarting the app.
EXIFTOOL_CONFIG = Path(__file__).with_name("exiftool_vht.config")
SETTINGS_TAG = "XMP-vht:CompressionSettings"


def settings_tag_argument(record):
    return f"-{SETTINGS_TAG}={json.dumps(record, sort_keys=True)}"


def read_compression_settings(paths):
    """Returns {str(path): settings dict} for the files that carry the tag; one exiftool call for all."""
    paths = [str(p) for p in paths]
    if not paths:
        return {}
    cmd = ["exiftool", "-config", str(EXIFTOOL_CONFIG), "-j", f"-{SETTINGS_TAG}", "-charset", "filename=utf8", "-@", "-"]
    try:
        result = subprocess.run(cmd, input="\n".join(paths), capture_output=True, text=True)
        entries = json.loads(result.stdout or "[]")
    except (OSError, json.JSONDecodeError):
        return {}
    # exiftool names the files its own way (with "/" on Windows): back to the names as they were given
    given = {os.path.normcase(os.path.normpath(p)): p for p in paths}
    found = {}
    for entry in entries:
        raw = entry.get("CompressionSettings")
        if not raw:
            continue
        name = given.get(os.path.normcase(os.path.normpath(entry.get("SourceFile", ""))), entry.get("SourceFile"))
        try:
            found[name] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
    return found


# x265's presets by the options it writes into every video it makes (me: 0 dia, 1 hex, 3 star):
# (me, subme, ref, rd, rc-lookahead) -> preset
X265_PRESET_OPTIONS = {
    (0, 0, 1, 2, 5): "ultrafast", (1, 1, 1, 2, 10): "superfast", (1, 1, 2, 2, 15): "veryfast",
    (1, 2, 2, 2, 15): "faster", (1, 2, 3, 2, 15): "fast", (1, 2, 3, 3, 20): "medium", (3, 3, 4, 4, 25): "slow",
    (3, 4, 4, 6, 40): "slower", (3, 4, 5, 6, 40): "veryslow", (3, 5, 5, 6, 60): "placebo",
}
X265_INFO = b"x265 (build"
X265_SEARCH_BYTES = 8 * 1024 * 1024   # the options come with the first frame, near the start of the media data


def x265_options(path):
    """The options x265 wrote into the video ({"crf": "20.0", "me": "3", ...}), or None for another encoder."""
    from .boxes import top_level_boxes

    # ffmpeg also puts the message into the track's decoder setup (hvcC, in moov), x265 into the first frame
    try:
        mdat = next((b for b in top_level_boxes(path) if b[0] == b"mdat"), None)
        with open(path, "rb") as f:
            data = f.read(X265_SEARCH_BYTES)
            start = data.find(X265_INFO)
            if start < 0 and mdat is not None and mdat[1] > X265_SEARCH_BYTES - 1024:
                f.seek(mdat[1])
                data = f.read(X265_SEARCH_BYTES)
                start = data.find(X265_INFO)
    except OSError:
        return None
    if start < 0:
        return None
    end = start
    while end < len(data) and 0x20 <= data[end] < 0x7F:   # the text ends where the SEI message ends
        end += 1
    text = data[start:end].decode("ascii")
    return dict(word.split("=", 1) for word in text.partition(" - options: ")[2].split() if "=" in word)


def x265_preset(options):
    try:
        key = tuple(int(options[name]) for name in ("me", "subme", "ref", "rd", "rc-lookahead"))
    except (KeyError, ValueError):
        return None
    return X265_PRESET_OPTIONS.get(key)


def encoding_from_file(path):
    """What can be read from a video about how it was encoded, for results without the settings tag (made by
    another program or an early version): a settings dict like the stored one, with "read_from_file": True."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,width,height,avg_frame_rate,color_transfer:stream_tags=encoder", "-of", "json", str(path)]
    try:
        streams = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout).get("streams", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not streams:
        return None
    stream = streams[0]
    settings = {"read_from_file": True, "codec": stream.get("codec_name")}
    if stream.get("width") and stream.get("height"):
        settings["max_res"] = max(stream["width"], stream["height"])
    try:
        numerator, denominator = (int(n) for n in stream.get("avg_frame_rate", "0/0").split("/"))
        if numerator and denominator:
            settings["max_fps"] = round(numerator / denominator, 2)
    except ValueError:
        pass
    settings["hdr"] = stream.get("color_transfer") in ("smpte2084", "arib-std-b67")
    encoder = (stream.get("tags") or {}).get("encoder", "")
    options = x265_options(path) if stream.get("codec_name") == "hevc" else None
    if options is not None:
        settings["encoder"] = "libx265"
        if options.get("rc") == "crf" and options.get("crf"):
            crf = float(options["crf"])
            settings["crf"] = int(crf) if crf.is_integer() else crf
        settings["preset"] = x265_preset(options)
    elif "libx265" in encoder:
        settings["encoder"] = "libx265"
    elif "videotoolbox" in encoder:
        settings["encoder"] = "hevc_videotoolbox"
    return settings


_DATE_RE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?")


def parse_exif_date(value):
    """An aware datetime from an exiftool date ("2025:06:15 19:20:57+09:00"); local time when it has no zone."""
    import datetime as dt
    match = _DATE_RE.match(str(value or ""))
    if not match or match.group(1) == "0000":
        return None
    try:
        moment = dt.datetime(*(int(g) for g in match.groups()[:6]))
    except ValueError:
        return None
    zone = match.group(7)
    if zone == "Z":
        return moment.replace(tzinfo=dt.timezone.utc)
    if zone:
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        return moment.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))))
    return moment.astimezone()


def file_capture_time(path):
    """The older of creation and modification time: what the Finder shows as the date of a video."""
    stat = Path(path).stat()
    birth = getattr(stat, "st_birthtime", None)
    return birth if birth is not None and birth < stat.st_mtime else stat.st_mtime


def capture_date_arguments(src_path):
    """exiftool arguments that give the result a capture date when the original has none of its own in the
    usual video fields (some cameras, screen recordings, messenger videos): then only the file date knew it,
    and that is lost with the first careless copy. Returns (arguments, where the date came from or None)."""
    import datetime as dt
    cmd = ["exiftool", "-j", "-a", "-G1", "-Keys:CreationDate", "-QuickTime:CreateDate", "-DateTimeOriginal",
           "-charset", "filename=utf8", str(src_path)]
    try:
        tags = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout or "[{}]")[0]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        return [], None
    if parse_exif_date(tags.get("Keys:CreationDate")) or parse_exif_date(tags.get("QuickTime:CreateDate")):
        return [], None
    origin, moment = None, None
    for key, value in tags.items():
        if key.endswith(":DateTimeOriginal") and parse_exif_date(value):
            origin, moment = key.split(":")[0] + ":DateTimeOriginal", parse_exif_date(value)
            break
    if moment is None:
        try:
            moment = dt.datetime.fromtimestamp(file_capture_time(src_path)).astimezone()
        except OSError:
            return [], None
        origin = "file date"
    moment = moment.replace(microsecond=0)
    local = moment.strftime("%Y:%m:%d %H:%M:%S") + moment.strftime("%z")[:3] + ":" + moment.strftime("%z")[3:]
    utc = moment.astimezone(dt.timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    # Keys:CreationDate (with its zone) is what Apple Photos reads; CreateDate is stored in UTC by the format.
    return [f"-Keys:CreationDate={local}", f"-QuickTime:CreateDate={utc}"], origin


# What a compressed copy is expected to change: the container and its tracks (codec, sizes, durations, the
# encoder's own name), exiftool's bookkeeping, and our settings tag. Everything else should come over unchanged.
IGNORED_METADATA_GROUPS = re.compile(r"^(ExifTool|System|File|Composite|QuickTime|Track\d+|Video\d*|Audio\d*)$")
IGNORED_METADATA_TAGS = {"Encoder", "XMPToolkit", "MajorBrand", "MinorVersion", "CompatibleBrands", "CompressorName",
                         "HandlerType", "HandlerVendorID", "HandlerDescription", "CompressionSettings",
                         # ties a file to one asset of the Photos library: a compressed copy must not claim to be it
                         "ApplePhotosOriginatingSignature"}
# The stream description of other containers (AVI, AVCHD): codec, rates, sizes, counts. Their camera metadata
# (dates, make, exposure) lives in the same groups and is still compared.
TECHNICAL_STREAM_TAGS = {"FrameRate", "MaxDataRate", "FrameCount", "StreamCount", "StreamType", "StreamName",
                         "VideoCodec", "VideoFrameCount", "Quality", "SampleSize", "VendorName", "AudioCodec",
                         "AudioSampleRate", "AudioSampleCount", "Encoding", "NumChannels", "SampleRate",
                         "AvgBytesPerSec", "BitsPerSample", "VideoFrameRate", "VideoStreamType", "AudioStreamType", "Duration",
                         "AudioBitrate", "SurroundMode", "ImageWidth", "ImageHeight", "AudioChannels"}
STREAM_GROUPS = {"RIFF", "M2TS", "AC3", "H264", "MPEG"}


def expected_to_change(group, tag):
    """Whether compressing changes this tag by its nature (container, stream, positions in the file)."""
    return (IGNORED_METADATA_GROUPS.match(group) is not None or tag in IGNORED_METADATA_TAGS
            or (group in STREAM_GROUPS and tag in TECHNICAL_STREAM_TAGS)
            or tag.endswith("Offset"))   # where something sits in the file, e.g. ThumbnailOffset
_ARG_RE = re.compile(r"^-([^:=]+):([^=]+)=(.*)$")


def metadata_entries(path, unknown=False, embedded=False):
    """[(group, tag, value)] of everything exiftool reads from path, duplicates included. unknown: also the boxes
    exiftool has no name for (as Unknown_xxxx); embedded: also the data per frame in the media data (GPS tracks,
    Apple's per-frame camera data), which takes a while on long videos."""
    cmd = ["exiftool", "-config", str(EXIFTOOL_CONFIG), "-a", "-G1", "-s", "-args", "-api", "LargeFileSupport=1",
           "-charset", "filename=utf8", *(["-u"] if unknown else []), *(["-ee"] if embedded else []), str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)
    return [match.groups() for match in map(_ARG_RE.match, result.stdout.splitlines()) if match]


def side_tracks(path):
    """Codec tags of the tracks that are neither picture nor sound, e.g. ["tmcd", "mebx"]."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_tag_string,codec_name",
           "-of", "json", str(path)]
    try:
        streams = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout).get("streams", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    tracks = []
    for stream in streams:
        if stream.get("codec_type") in ("data", "subtitle"):
            tag = stream.get("codec_tag_string") or ""
            tracks.append(tag if tag and not tag.startswith("[") else stream.get("codec_name") or stream["codec_type"])
    return tracks


def missing_metadata(src_path, dst_path, kept_as_text=()):
    """Metadata of the original that the result lacks or has with another value, as "Group:Tag" names.
    A value counts as kept when the same tag holds it in any group (exiftool may file a copy elsewhere), or when
    its name is in kept_as_text (see metadata_as_text)."""
    kept_as_text = set(kept_as_text)
    src_entries, dst_entries = metadata_entries(src_path), metadata_entries(dst_path)
    exact = set(dst_entries)
    by_name = {(tag, value) for _, tag, value in dst_entries}
    missing = []
    for group, tag, value in src_entries:
        if expected_to_change(group, tag) or not value.strip() or value.startswith("(Binary data 0 bytes"):
            continue   # an empty field holds nothing to lose (exiftool does not write it)
        if (group, tag, value) in exact or (tag, value) in by_name or f"{group}:{tag}" in kept_as_text:
            continue
        name = f"{group}:{tag}"
        if name not in missing:
            missing.append(name)
    # tracks of their own: timecode (tmcd), per-frame camera data (Apple mebx, Sony rtmd, GoPro gpmd), subtitles
    src_tracks, dst_tracks = side_tracks(src_path), side_tracks(dst_path)
    for tag in sorted(set(src_tracks)):
        lost = src_tracks.count(tag) - dst_tracks.count(tag)
        if lost > 0:
            missing.append(f"Track:{tag}" + (f" ×{lost}" if lost > 1 else ""))
    # the movie header's date is checked although the rest of that group is technical
    created = next((v for g, t, v in src_entries if (g, t) == ("QuickTime", "CreateDate")), None)
    if parse_exif_date(created) and ("QuickTime", "CreateDate", created) not in exact \
            and "QuickTime:CreateDate" not in kept_as_text:
        missing.append("QuickTime:CreateDate")
    return missing


# Old formats (AVI, AVCHD) keep their camera metadata in structures MP4 has no room for. The values that have a
# standard field go there, so other programs show them; all of it is also kept as text in the settings tag.
LEGACY_TAG_COPIES = [
    "-Keys:Make<Make", "-Keys:Model<Model", "-XMP-tiff:Make<Make", "-XMP-tiff:Model<Model",
    "-XMP-exif:DateTimeOriginal<DateTimeOriginal", "-XMP-exif:FNumber<FNumber", "-XMP-exif:ExposureTime<ExposureTime",
    "-XMP-exif:ISO<ISO", "-XMP-exif:FocalLength<FocalLength", "-XMP-tiff:Software<Software", "-XMP-dc:Rights<Copyright",
]
MAX_TEXT_COPY_VALUE = 256 * 1024   # larger embedded files (e.g. preview videos) are left out of the text copy


def is_quicktime(path):
    """Whether path is an MP4/MOV (box structure with a moov), whose metadata can be copied as it is."""
    from .boxes import top_level_boxes
    try:
        return any(box[0] == b"moov" for box in top_level_boxes(path))
    except (OSError, ValueError):
        return False


def metadata_as_text(path):
    """{"Group:Tag": value} of all metadata of path that a compressed copy cannot keep as it is; binary values
    (e.g. a thumbnail) as "base64:..." strings."""
    cmd = ["exiftool", "-config", str(EXIFTOOL_CONFIG), "-j", "-a", "-G1", "-b", "-api", "LargeFileSupport=1",
           "-charset", "filename=utf8", str(path)]
    try:
        entries = json.loads(subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300).stdout)[0]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        return {}
    kept = {}
    for name, value in entries.items():
        group, _, tag = name.partition(":")
        if not tag or expected_to_change(group, tag):
            continue
        if isinstance(value, str) and len(value) > MAX_TEXT_COPY_VALUE:
            continue
        kept[name] = value
    return kept


SIDECAR_SUFFIXES = {".xmp", ".aae", ".xml", ".thm"}


def find_sidecars(src_path):
    """Files next to the original that belong to it: IMG_1.xmp, IMG_1.MOV.xmp (Lightroom, darktable), IMG_1.AAE
    (Photos edits), C0001M01.XML (Sony), GOPR0001.THM. [(sidecar path, the part of its name after the video's)]."""
    src_path = Path(src_path)
    found = []
    try:
        neighbours = list(src_path.parent.iterdir())
    except OSError:
        return []
    stem, name = src_path.stem.lower(), src_path.name.lower()
    for path in neighbours:
        lower = path.name.lower()
        if path == src_path or path.suffix.lower() not in SIDECAR_SUFFIXES or lower.startswith("._") or not path.is_file():
            continue
        for prefix in (name + ".", stem + "m01.", stem + "."):   # longest first: IMG_1.MOV.xmp before IMG_1.xmp
            if lower.startswith(prefix):
                found.append((path, path.name[len(prefix) - 1:] if prefix != stem + "m01." else path.name[len(stem):]))
                break
    return sorted(found)


def copy_sidecars(src_path, dst_path):
    """Copies the original's sidecars next to the result under the result's name (an existing file of that name is
    kept). XMP sidecars are also written into the result itself. Returns the sidecars written."""
    src_path, dst_path = Path(src_path), Path(dst_path)
    written = []
    for sidecar, ending in find_sidecars(src_path):
        if sidecar.name.lower().startswith(src_path.name.lower() + "."):
            target = dst_path.with_name(dst_path.name + ending)       # IMG_1.MOV.xmp -> IMG_1_archived.mp4.xmp
        else:
            target = dst_path.with_name(dst_path.stem + ending)       # IMG_1.xmp -> IMG_1_archived.xmp
        if sidecar.suffix.lower() == ".xmp":
            subprocess.run(["exiftool", "-config", str(EXIFTOOL_CONFIG), "-tagsFromFile", str(sidecar), "-XMP:all",
                            "--XMP-vht:all", str(dst_path), "-overwrite_original", "-q", "-q"], check=False)
        if target.exists() or target == sidecar:
            continue
        import shutil
        shutil.copy2(sidecar, target)
        written.append(target)
    return written


def source_description(src_path):
    """What the result was made from, for the settings record."""
    path = Path(src_path)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {"name": path.name, "size": size, "format": describe_streams(path)}


def describe_streams(file_path):
    """Short codec summary for error messages, e.g. "hevc (Main 10), 3840x2160, 59.94 fps · audio pcm_s24le"."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,profile,width,height,r_frame_rate,pix_fmt", "-of", "json", str(file_path)]
    try:
        streams = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout).get("streams", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return ""
    parts = []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video":
            text = stream.get("codec_name", "?")
            if stream.get("profile"):
                text += f" ({stream['profile']})"
            if stream.get("width"):
                text += f", {stream['width']}x{stream['height']}"
            num, _, den = stream.get("r_frame_rate", "0/0").partition("/")
            if den and float(den) > 0:
                text += f", {float(num) / float(den):.2f} fps"
            if stream.get("pix_fmt"):
                text += f", {stream['pix_fmt']}"
            parts.append(text)
        elif kind == "audio":
            parts.append(f"audio {stream.get('codec_name', '?')}")
        elif kind:
            parts.append(f"{kind} {stream.get('codec_name') or ''}".strip())
    return " · ".join(parts)


def get_display_rotation(file_path):
    """Rotation from the display matrix (e.g. 90 for portrait phone clips stored landscape); 0, 90, 180 or 270."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream_side_data=rotation", "-of", "json", str(file_path)]
    try:
        streams = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout).get("streams", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return 0
    for stream in streams:
        for side_data in stream.get("side_data_list", []):
            if "rotation" in side_data:
                return int(round(float(side_data["rotation"]))) % 360
    return 0


# HLG (iPhone HDR) and PQ (HDR10); everything else is treated as SDR.
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


def get_hdr_color(file_path):
    """Colour tags to carry over for HDR sources, or None for SDR."""
    probe = _probe(file_path)
    if probe is None or probe[1] is None:
        return None
    video = probe[1]
    if video.get("color_transfer") not in HDR_TRANSFERS:
        return None
    return {
        "color_trc": video["color_transfer"],
        "color_primaries": video.get("color_primaries") or "bt2020",
        "colorspace": video.get("color_space") or "bt2020nc",
    }


def copy_file_times(src_path, dst_path):
    """Gives dst the access, modification and (on macOS and Windows) creation time of src. The Finder and the
    Explorer show the creation time, so without this a compressed copy looks as if it had been made today."""
    stat = Path(src_path).stat()
    birth = getattr(stat, "st_birthtime", None)
    if sys.platform == "win32":
        os.utime(dst_path, (stat.st_atime, stat.st_mtime))
        if birth is not None:
            set_windows_creation_time(dst_path, birth)
        return
    if birth is not None and birth < stat.st_mtime:
        # macOS moves a file's creation time back whenever the modification time is set to an earlier moment.
        os.utime(dst_path, (stat.st_atime, birth))
    os.utime(dst_path, (stat.st_atime, stat.st_mtime))


def set_windows_creation_time(path, timestamp):
    """Sets a file's creation time on Windows (os.utime cannot)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), wintypes.LPVOID, wintypes.LPVOID]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    FILE_WRITE_ATTRIBUTES, SHARE_ALL, OPEN_EXISTING, BACKUP_SEMANTICS = 0x0100, 0x7, 3, 0x02000000
    handle = kernel32.CreateFileW(str(path), FILE_WRITE_ATTRIBUTES, SHARE_ALL, None, OPEN_EXISTING, BACKUP_SEMANTICS, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ticks = int(timestamp * 10_000_000) + 116_444_736_000_000_000   # 100 ns steps since 1601
        created = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        if not kernel32.SetFileTime(handle, ctypes.byref(created), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


class DurationCache:
    """Remembers video durations between runs, keyed by file size and modification time, so a rescan of
    hundreds of videos on a slow disk does not have to open every file again."""

    def __init__(self, path=None):
        from video_helper_core.paths import duration_cache_file
        self.path = Path(path) if path else duration_cache_file()
        try:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = {}
        self.unsaved = 0

    @staticmethod
    def signature(file_path):
        stat = Path(file_path).stat()
        return [stat.st_size, stat.st_mtime_ns]

    def get(self, file_path):
        entry = self.entries.get(str(file_path))
        try:
            if entry and entry[:2] == self.signature(file_path):
                return entry[2]
        except OSError:
            pass
        return None

    def put(self, file_path, duration):
        try:
            self.entries[str(file_path)] = [*self.signature(file_path), duration]
            self.unsaved += 1
        except OSError:
            pass

    def save(self):
        if not self.unsaved:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries), encoding="utf-8")
            os.replace(tmp, self.path)
            self.unsaved = 0
        except OSError:
            pass


def format_duration_long(seconds):
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return tr("{days} d {hours} h", days=days, hours=hours)
    if hours:
        return tr("{hours} h {minutes} min", hours=hours, minutes=minutes)
    return tr("{minutes} min", minutes=minutes)


class EncodeSpeeds:
    """How long encoding takes for a given kind of settings, so the time of a whole folder can be estimated before it
    starts. Stored per user (from real runs and from the compression lab) as: seconds of work per second of video."""

    def __init__(self, path=None):
        self.path = Path(path) if path else speed_cache_file()
        try:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = {}

    @staticmethod
    def key(settings, jobs):
        cpu = settings.get('renderer') == "libx265"
        return "|".join(str(part) for part in (
            settings.get('renderer'), settings.get('preset') if cpu else "-",
            settings.get('max_res') if settings.get('limit_res') else 0,
            settings.get('max_fps') if settings.get('limit_fps') else 0, jobs))

    def record(self, settings, jobs, video_seconds, wall_seconds):
        """One finished encode: video_seconds of video took wall_seconds, with `jobs` encodes running side by side."""
        if not video_seconds or wall_seconds <= 0:
            return
        ratio = wall_seconds / video_seconds
        entry = self.entries.get(self.key(settings, jobs), {"ratio": ratio, "n": 0})
        weight = min(entry["n"], 4)                       # recent measurements count more than old ones
        self.entries[self.key(settings, jobs)] = {"ratio": (entry["ratio"] * weight + ratio) / (weight + 1), "n": entry["n"] + 1}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def estimate(self, settings, jobs, video_seconds):
        """(seconds, exact) for encoding video_seconds of video, or None if nothing is known for these settings.
        exact: measured with the same number of parallel encodes. Otherwise the result is a cautious guess: a
        measurement with other settings of parallelism counts as if the encodes ran one after the other."""
        entry = self.entries.get(self.key(settings, jobs))
        if entry:
            return video_seconds * entry["ratio"] / jobs, True
        prefix = self.key(settings, "").rstrip("|") + "|"
        others = [(int(k.rsplit("|", 1)[1]), v) for k, v in self.entries.items() if k.startswith(prefix)]
        if not others:
            return None
        known_jobs, entry = min(others, key=lambda item: item[0])
        return video_seconds * entry["ratio"] / known_jobs, False


# ffmpeg's blockdetect score. Measured: original and x265 CRF 20 about 1.1-1.3, VideoToolbox 8 bit with visible blocks
# 2.4-10 (same clips). The original counts too, so a source that is blocky itself raises no warning.
BLOCK_WARN_SCORE = 2.0
BLOCK_WARN_RATIO = 1.5
BLOCK_SAMPLE_SECONDS = 2


def is_blocky(result, original=None):
    return result >= BLOCK_WARN_SCORE and (original is None or result >= original * BLOCK_WARN_RATIO)


def block_sample_starts(duration):
    """Where to measure: three short parts, or the whole video if it is short."""
    if not duration or duration <= BLOCK_SAMPLE_SECONDS * 3:
        return [0]
    return [round(duration * part, 2) for part in (0.15, 0.5, 0.85)]


def blockiness_command(path, start):
    return ["ffmpeg", "-hide_banner", "-threads", "2", "-ss", str(start), "-t", str(BLOCK_SAMPLE_SECONDS), "-i", str(path),
            "-an", "-vf", "format=yuv420p,blockdetect", "-f", "null", "-"]


def parse_blockiness(output):
    match = re.search(r"block mean:\s*([0-9.]+)", output)
    return float(match.group(1)) if match else None
