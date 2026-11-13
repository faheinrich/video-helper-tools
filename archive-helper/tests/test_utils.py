import os
import subprocess
import sys

import pytest

from conftest import EXAMPLE_VIDEOS, requires_ffmpeg
from archive_helper.compressor.utils import format_duration, format_size, get_video_info, parse_ffmpeg_time


@pytest.mark.parametrize(
    "size, expected",
    [(0, "0.00 B"), (1023, "1023.00 B"), (1024, "1.00 KB"), (1536 * 1024, "1.50 MB"), (-2048, "-2.00 KB")],
)
def test_format_size(size, expected):
    assert format_size(size) == expected


def test_parse_ffmpeg_time():
    line = "frame=  120 fps= 60 q=28.0 size=512kB time=00:01:02.50 bitrate=67.1kbits/s speed=2.5x"
    assert parse_ffmpeg_time(line) == pytest.approx(62.5)
    assert parse_ffmpeg_time("no timestamp here") is None


def test_format_duration():
    assert format_duration(75) == "⏱️ 01:15"
    assert format_duration(None) == "⏱️ --:--"


@requires_ffmpeg
def test_get_video_info_reads_example_video():
    duration, fps, audio_codec = get_video_info(EXAMPLE_VIDEOS[0])
    assert duration == pytest.approx(26.9, abs=0.1)
    assert fps == pytest.approx(24)
    assert audio_codec == "aac"


@requires_ffmpeg
def test_get_video_info_without_audio_track(tmp_path):
    import subprocess

    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-an", "-c", "copy", str(silent)], check=True)
    duration, fps, audio_codec = get_video_info(silent)
    assert duration == pytest.approx(26.9, abs=0.1)
    assert fps == pytest.approx(24)
    assert audio_codec is None


@requires_ffmpeg
def test_get_video_info_ignores_cover_art(tmp_path):
    # Cover art shows up as an extra video stream (90000 fps); using it would make the
    # compressor add an fps filter and upsample a 24 fps clip to the configured limit.
    import subprocess

    cover, with_cover = tmp_path / "cover.jpg", tmp_path / "with_cover.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-frames:v", "1", str(cover)], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-i", str(cover),
         "-map", "0", "-map", "1", "-c", "copy", "-disposition:v:1", "attached_pic", str(with_cover)],
        check=True,
    )
    assert get_video_info(with_cover)[1] == pytest.approx(24)


def test_get_video_info_missing_file(tmp_path):
    assert get_video_info(tmp_path / "does-not-exist.mp4") == (None, None, None)


@requires_ffmpeg
@pytest.mark.parametrize("seconds", [0.5, 1.02, 5])
def test_thumbnail_is_created_for_short_and_normal_clips(tmp_path, seconds):
    # Regression: the frame at 1 s was used unconditionally, so sub-second clips got none.
    import subprocess
    from archive_helper.compressor.utils import generate_thumbnail

    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-t", str(seconds), str(clip)], check=True)
    thumb = tmp_path / "thumb.jpg"
    assert generate_thumbnail(clip, thumb)
    assert thumb.stat().st_size > 0


@pytest.mark.skipif(not hasattr(os.stat_result, "st_birthtime"), reason="creation time only on macOS")
def test_copy_file_times_copies_creation_time(tmp_path):
    from archive_helper.compressor.utils import copy_file_times

    src, dst = tmp_path / "src.mov", tmp_path / "dst.mp4"
    src.write_bytes(b"a")
    old = 1_700_000_000  # November 2023
    os.utime(src, (old, old))  # on macOS this moves the creation time back, too
    if sys.platform == "win32":
        from archive_helper.compressor.utils import set_windows_creation_time
        set_windows_creation_time(src, old)   # Windows keeps it, so it is set directly
    later = old + 86400 * 30
    os.utime(src, (later, later))  # a later modification keeps the creation time
    dst.write_bytes(b"b")
    copy_file_times(src, dst)
    assert int(dst.stat().st_birthtime) == int(src.stat().st_birthtime) == old
    assert int(dst.stat().st_mtime) == later


def test_thumbnail_path_changes_when_the_video_changes(tmp_path):
    from archive_helper.compressor.utils import get_thumbnail_path

    video = tmp_path / "a.mp4"
    video.write_bytes(b"1")
    first = get_thumbnail_path(video)
    video.write_bytes(b"22")
    assert get_thumbnail_path(video) != first


def test_old_thumbnails_are_pruned_and_the_cache_can_be_cleared(tmp_path):
    from archive_helper.compressor.utils import clear_caches, prune_thumbnails
    from video_helper_core.paths import duration_cache_file, thumbnail_dir

    old, fresh = thumbnail_dir() / "old.jpg", thumbnail_dir() / "fresh.jpg"
    old.write_bytes(b"x" * 10)
    fresh.write_bytes(b"x" * 20)
    os.utime(old, (1, 1))
    assert prune_thumbnails() == 1 and not old.exists() and fresh.exists()
    duration_cache_file().write_text("{}")
    assert clear_caches() == 22 and not fresh.exists() and not duration_cache_file().exists()


def test_stored_settings_are_found_under_the_name_they_were_asked_for(tmp_path, monkeypatch):
    # Regression (Windows): exiftool writes the names with "/", so a lookup with the path as given ("\") found
    # nothing and the settings column stayed empty. Here exiftool answers with an equivalent spelling of the path.
    import json
    import subprocess
    from types import SimpleNamespace
    from archive_helper.compressor import utils

    video = tmp_path / "a_archived.mp4"
    answer = [{"SourceFile": str(tmp_path / "sub" / ".." / "a_archived.mp4"), "CompressionSettings": '{"crf": 20}'}]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(answer)))
    assert utils.read_compression_settings([video]) == {str(video): {"crf": 20}}


@pytest.mark.parametrize("preset", ["ultrafast", "fast", "medium", "slow"])
def test_settings_are_read_from_a_video_without_the_settings_tag(tmp_path, preset):
    # results made by another program or an early version: x265 writes its options into the video
    import shutil
    from archive_helper.compressor.model import describe_settings
    from archive_helper.compressor.utils import encoding_from_file

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / f"{preset}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=0.4",
                    "-c:v", "libx265", "-preset", preset, "-crf", "23", "-x265-params", "log-level=error",
                    "-tag:v", "hvc1", str(path)], check=True)

    settings = encoding_from_file(path)

    assert settings == {"read_from_file": True, "codec": "hevc", "max_res": 320, "max_fps": 25.0, "hdr": False,
                        "encoder": "libx265", "crf": 23, "preset": preset}
    assert describe_settings(settings) == f"≈ CPU · CRF 23 · {preset} · 320 px · 25 fps"


def test_another_encoders_video_shows_its_codec(tmp_path):
    import shutil
    from archive_helper.compressor.model import describe_settings
    from archive_helper.compressor.utils import encoding_from_file

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "h264.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=0.4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-fflags", "+bitexact", "-map_metadata", "-1", str(path)],
                   check=True)
    assert describe_settings(encoding_from_file(path)) == "≈ H264 · 320 px · 30 fps"
