"""Checks the compressed files themselves (resolution, fps, audio tracks, metadata, colour), without the GUI.

Runs the encoder through ArchiveWorker.start_video_process / finalize_video_process and inspects the result with ffprobe.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import EXAMPLE_VIDEOS, requires_exiftool, requires_ffmpeg

pytestmark = requires_ffmpeg

BASE_SETTINGS = {
    "limit_res": False, "max_res": 0, "limit_fps": False, "max_fps": 0,
    "renderer": "libx265", "crf": 30, "preset": "ultrafast", "vt_quality": 50,
    "overwrite": False, "copy_aac": False, "dry_run": False,
}


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def streams(path, kind):
    return [s for s in ffprobe(path)["streams"] if s["codec_type"] == kind]


def make_clip(path, size="1920x1080", fps=30, seconds=2, extra=()):
    """Synthetic H.264 clip with one tone as audio; fast and independent of the example videos."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", *extra, str(path)],
        check=True)
    return path


def compress(qapp, src_root, name, **overrides):
    """Compress src_root/name into a dst folder; returns (dst_path, finalize message, data)."""
    from archive_helper.compressor.utils import get_video_info
    from archive_helper.compressor.workers import ArchiveWorker

    src_root = src_root.resolve()
    src = src_root / name
    dst = src_root.parent / "dst" / (src.stem + "_archived.mp4")
    worker = ArchiveWorker(src_root, dst.parent, 1, [], {**BASE_SETTINGS, **overrides})
    duration, fps, audio = get_video_info(src)
    proc, error, message, _ = worker.start_video_process(src, dst, duration, fps, audio)
    assert proc is not None, f"{error}: {message}"
    proc.stdout.close()
    try:
        os.set_blocking(proc.stderr.fileno(), True)
        proc.stderr.read()
    finally:
        proc.wait(timeout=180)
    message, data = worker.finalize_video_process(proc, src, dst)
    return dst, message, data


@pytest.mark.parametrize("limit,size,expected", [
    (1280, "1920x1080", (1280, 720)),     # landscape is scaled down
    (1280, "1080x1920", (720, 1280)),     # portrait: the long side is limited
    (1920, "1280x720", (1280, 720)),      # never upscaled
    (720, "1000x562", (718, 404)),        # aspect ratio kept, result rounded to even is rounded to even
])
def test_resolution_limit(qapp, tmp_path, limit, size, expected):
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "clip.mp4", size=size)
    out, _, _ = compress(qapp, src, "clip.mp4", limit_res=True, max_res=limit)
    video = streams(out, "video")[0]
    assert (video["width"], video["height"]) == expected
    assert video["width"] % 2 == 0 and video["height"] % 2 == 0


def test_resolution_is_kept_without_limit(qapp, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "clip.mp4", size="1280x720")
    out, _, _ = compress(qapp, src, "clip.mp4")
    video = streams(out, "video")[0]
    assert (video["width"], video["height"]) == (1280, 720)


@pytest.mark.parametrize("source_fps,max_fps,expected", [(60, 30, 30), (24, 30, 24), (30, 30, 30)])
def test_fps_limit(qapp, tmp_path, source_fps, max_fps, expected):
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "clip.mp4", size="640x360", fps=source_fps)
    out, _, _ = compress(qapp, src, "clip.mp4", limit_fps=True, max_fps=max_fps)
    num, den = streams(out, "video")[0]["r_frame_rate"].split("/")
    assert int(num) / int(den) == pytest.approx(expected, abs=0.01)


def test_output_is_hevc_hvc1_8bit_with_duration_kept(qapp, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "clip.mp4", size="640x360", seconds=3)
    out, message, data = compress(qapp, src, "clip.mp4")
    video = streams(out, "video")[0]
    assert (video["codec_name"], video["codec_tag_string"], video["pix_fmt"]) == ("hevc", "hvc1", "yuv420p")
    assert float(ffprobe(out)["format"]["duration"]) == pytest.approx(3, abs=0.2)
    assert message.startswith("✅")
    assert data["dst_size"] == out.stat().st_size and data["src_size"] == (src / "clip.mp4").stat().st_size


def test_dry_run_encodes_only_one_second(qapp, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "clip.mp4", size="640x360", seconds=4)
    out, _, _ = compress(qapp, src, "clip.mp4", dry_run=True)
    assert float(ffprobe(out)["format"]["duration"]) == pytest.approx(1, abs=0.2)


def make_two_track_clip(path):  # noqa: E302
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
         "-map", "0:v", "-map", "1:a", "-map", "2:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(path)],
        check=True)


@requires_exiftool
def test_all_audio_tracks_and_their_names_are_kept(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor import workers

    # ffmpeg cannot write the recorder-style "name" tag itself, so the probe result is faked.
    monkeypatch.setattr(workers, "get_audio_streams", lambda path: [("aac", "game"), ("aac", "voice")])
    src = tmp_path / "src"
    src.mkdir()
    make_two_track_clip(src / "rec.mp4")
    out, _, _ = compress(qapp, src, "rec.mp4")
    audio = streams(out, "audio")
    assert len(audio) == 2
    names = [(a.get("tags") or {}).get("title") or (a.get("tags") or {}).get("handler_name") for a in audio]
    assert names == ["game", "voice"]


def test_aac_audio_is_copied_when_requested_and_reencoded_otherwise(qapp, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "clip.mp4", size="640x360")
    original = streams(src / "clip.mp4", "audio")[0]
    copied, _, _ = compress(qapp, src, "clip.mp4", copy_aac=True)
    assert streams(copied, "audio")[0]["bit_rate"] == original["bit_rate"]
    reencoded, _, _ = compress(qapp, src, "clip.mp4", copy_aac=False, overwrite=True)
    assert streams(reencoded, "audio")[0]["codec_name"] == "aac"
    assert int(streams(reencoded, "audio")[0]["bit_rate"]) == pytest.approx(128_000, rel=0.15)


def test_video_without_audio_is_compressed(qapp, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", str(src / "mute.mp4")], check=True)
    out, message, _ = compress(qapp, src, "mute.mp4")
    assert message.startswith("✅") and streams(out, "audio") == []


@requires_exiftool
def test_capture_date_gps_and_file_times_are_carried_over(qapp, tmp_path):
    from archive_helper.compressor.utils import read_compression_settings

    src = tmp_path / "src"
    src.mkdir()
    clip = make_clip(src / "IMG_1.mov", size="640x360")
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-Keys:CreationDate=2025:06:15 19:20:57+09:00",
         "-Keys:GPSCoordinates=35.1623 N, 129.1577 E, 8.3", str(clip)], check=True)
    os.utime(clip, (1_750_000_000, 1_750_000_000))

    out, _, data = compress(qapp, src, "IMG_1.mov", limit_res=True, max_res=480)

    tags = json.loads(subprocess.run(
        ["exiftool", "-j", "-n", "-Keys:CreationDate", "-Keys:GPSCoordinates", str(out)],
        capture_output=True, text=True, check=True).stdout)[0]
    assert tags["CreationDate"].startswith("2025:06:15 19:20:57")
    assert tags["GPSCoordinates"].startswith("35.1623 129.1577")
    assert out.stat().st_mtime == pytest.approx(1_750_000_000, abs=2)
    # the settings used are stored inside the result
    record = read_compression_settings([out])[str(out)]
    assert (record["encoder"], record["max_res"], record["crf"]) == ("libx265", 480, 30)
    assert data["settings"] == record


def test_portrait_phone_clips_come_out_upright(qapp, tmp_path):
    from archive_helper.compressor.utils import get_display_rotation

    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "plain.mp4", size="640x360")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(src / "plain.mp4"),
                    "-c", "copy", str(src / "turned.mp4")], check=True)
    if get_display_rotation(src / "turned.mp4") != 90:
        pytest.skip("this ffmpeg cannot write a display matrix")
    out, _, _ = compress(qapp, src, "turned.mp4")
    # ffmpeg applies the rotation while re-encoding: stored upright, no display matrix left
    video = streams(out, "video")[0]
    assert (video["width"], video["height"]) == (360, 640)
    assert get_display_rotation(out) == 0


def test_video_is_kept_with_a_warning_when_exiftool_fails(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor import workers
    from video_helper_core import i18n

    i18n.set_language("en")
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[0] == "exiftool":
            raise FileNotFoundError("exiftool")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(workers.subprocess, "run", run)
    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "a.mp4", size="320x180", seconds=1)
    out, message, data = compress(qapp, src, "a.mp4")
    assert out.exists() and "metadata could not be copied" in message
    assert data["settings"] is None


def test_failed_encode_leaves_no_result_file(qapp, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "broken.mp4").write_bytes(b"this is not a video")
    out, message, data = compress_expecting_failure(qapp, src, "broken.mp4")
    assert not out.exists()
    assert message.startswith("❌") and data == {}


def compress_expecting_failure(qapp, src_root, name):
    from archive_helper.compressor.workers import ArchiveWorker

    src = src_root / name
    dst = src_root.parent / "dst" / (src.stem + "_archived.mp4")
    worker = ArchiveWorker(src_root, dst.parent, 1, [], dict(BASE_SETTINGS))
    proc, error, message, _ = worker.start_video_process(src, dst, None, None, None)
    assert proc is not None, f"{error}: {message}"
    proc.communicate(timeout=60)
    assert proc.returncode != 0
    message, data = worker.finalize_video_process(proc, src, dst)
    return dst, message, data


def test_existing_result_is_skipped_unless_overwrite(qapp, tmp_path):
    from archive_helper.compressor.utils import get_video_info
    from archive_helper.compressor.workers import ArchiveWorker
    from video_helper_core.i18n import tr

    src_root = tmp_path / "src"
    src_root.mkdir()
    make_clip(src_root / "clip.mp4", size="640x360")
    out, _, _ = compress(qapp, src_root, "clip.mp4")
    duration, fps, audio = get_video_info(src_root / "clip.mp4")

    worker = ArchiveWorker(src_root, out.parent, 1, [], dict(BASE_SETTINGS))
    proc, reason, _, _ = worker.start_video_process(src_root / "clip.mp4", out, duration, fps, audio)
    assert proc is None and reason == tr("Already exists")

    worker.settings["overwrite"] = True
    proc, _, _, _ = worker.start_video_process(src_root / "clip.mp4", out, duration, fps, audio)
    assert proc is not None
    proc.communicate()


def test_example_video_is_smaller_and_equally_long(qapp, tmp_path):
    from archive_helper.compressor.utils import get_video_info

    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(EXAMPLE_VIDEOS[0], src / "ex.mp4")
    out, message, data = compress(qapp, src, "ex.mp4", limit_res=True, max_res=480, crf=34)
    assert get_video_info(out)[0] == pytest.approx(get_video_info(src / "ex.mp4")[0], abs=0.3)
    assert data["ratio"] < 100
    assert streams(out, "video")[0]["height"] <= 480


@pytest.mark.skipif(sys.platform == "win32", reason="reads the POSIX nice value")
@pytest.mark.parametrize("low_priority", [True, False])
def test_encoder_runs_with_lower_priority_when_asked(qapp, tmp_path, low_priority):
    from archive_helper.compressor.utils import get_video_info
    from archive_helper.compressor.workers import ArchiveWorker

    src = tmp_path / "src"
    src.mkdir()
    clip = make_clip(src / "a.mp4", size="1280x720", seconds=8)
    # a slow preset keeps ffmpeg running long enough to look at it, even on a busy machine
    worker = ArchiveWorker(src, tmp_path / "dst", 1, [], {**BASE_SETTINGS, "low_priority": low_priority, "preset": "veryslow"})
    duration, fps, audio = get_video_info(clip)
    proc, error, message, _ = worker.start_video_process(clip, tmp_path / "dst" / "a_archived.mp4", duration, fps, audio)
    assert proc is not None, f"{error}: {message}"
    nice = int(subprocess.run(["ps", "-o", "nice=", "-p", str(proc.pid)], capture_output=True, text=True).stdout.strip() or "-99")
    proc.kill()
    proc.communicate(timeout=30)
    # relative to this process: CI runners do not start at nice 0
    base = os.nice(0)
    assert nice == (min(19, base + 10) if low_priority else base)


# --- results are written to a partial file first ----------------------------------------------------------------------

def run_worker(qapp, src, dst, replace_source=False, **overrides):
    from archive_helper.compressor.utils import get_video_info
    from archive_helper.compressor.workers import ArchiveWorker

    worker = ArchiveWorker(src.parent, dst.parent, 1, [], {**BASE_SETTINGS, **overrides})
    duration, fps, audio = get_video_info(src)
    proc, error, message, _ = worker.start_video_process(src, dst, duration, fps, audio, replace_source=replace_source)
    assert proc is not None, f"{error}: {message}"
    proc.communicate(timeout=180)
    return worker.finalize_video_process(proc, src, dst, replace_source=replace_source)


def video_codec(path):
    return streams(path, "video")[0]["codec_name"]


def test_source_equals_target_replaces_the_original_with_the_smaller_compressed_file(qapp, tmp_path, trash):
    from archive_helper.compressor.utils import partial_path

    clip = make_clip(tmp_path / "clip.mp4", size="1920x1080", seconds=3)
    before = clip.stat().st_size
    original = clip.read_bytes()
    assert video_codec(clip) == "h264"
    message, data = run_worker(qapp, clip, clip, replace_source=True)
    assert message.startswith("✅") and video_codec(clip) == "hevc"
    assert clip.stat().st_size < before and data["src_size"] == before and data["dst_size"] == clip.stat().st_size
    assert not partial_path(clip).exists()
    assert (trash / "clip.mp4").read_bytes() == original   # the original is in the trash, not gone


def test_a_replacement_that_is_not_smaller_keeps_the_original(qapp, tmp_path):
    clip = make_clip(tmp_path / "tiny.mp4", size="64x64", seconds=1, extra=["-b:v", "20k"])
    original = clip.read_bytes()
    message, data = run_worker(qapp, clip, clip, replace_source=True, crf=5)
    assert message.startswith("✅") and "ORIGINAL BEHALTEN" in message
    assert clip.read_bytes() == original and data["dst_size"] == data["src_size"]
    assert not list(tmp_path.glob("*.partial*"))


def test_a_failed_run_leaves_an_existing_result_untouched(qapp, tmp_path):
    from archive_helper.compressor.utils import partial_path

    src = tmp_path / "src"
    src.mkdir()
    (src / "broken.mp4").write_bytes(b"this is not a video")
    dst = tmp_path / "dst" / "broken_archived.mp4"
    dst.parent.mkdir()
    dst.write_bytes(b"earlier result")
    from archive_helper.compressor.workers import ArchiveWorker

    worker = ArchiveWorker(src, dst.parent, 1, [], {**BASE_SETTINGS, "overwrite": True})
    proc, error, message, _ = worker.start_video_process(src / "broken.mp4", dst, None, None, None)
    assert proc is not None
    proc.communicate(timeout=60)
    message, data = worker.finalize_video_process(proc, src / "broken.mp4", dst)
    assert message.startswith("❌") and dst.read_bytes() == b"earlier result" and not partial_path(dst).exists()


def test_aborting_a_run_removes_only_the_unfinished_file(qapp, tmp_path):
    from archive_helper.compressor.utils import partial_path
    from archive_helper.compressor.workers import ArchiveWorker

    (tmp_path / "src").mkdir()
    clip = make_clip(tmp_path / "src" / "clip.mp4", size="1920x1080", seconds=6)
    dst = tmp_path / "dst" / "clip_archived.mp4"
    dst.parent.mkdir()
    dst.write_bytes(b"earlier result")
    from archive_helper.compressor.utils import get_video_info

    worker = ArchiveWorker(clip.parent, dst.parent, 1, [], {**BASE_SETTINGS, "overwrite": True, "preset": "veryslow"})
    duration, fps, audio = get_video_info(clip)
    proc, error, message, _ = worker.start_video_process(clip, dst, duration, fps, audio)
    assert proc is not None
    worker.active_processes = [(proc, clip, dst, duration, b"")]
    worker.kill_all_processes()
    proc.wait(timeout=30)
    assert dst.read_bytes() == b"earlier result" and not partial_path(dst).exists()


def test_a_video_that_cannot_be_started_is_an_error_with_the_reason_not_a_skip(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor import workers
    from archive_helper.compressor.workers import ArchiveWorker

    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    worker = ArchiveWorker(tmp_path, tmp_path, 1, [], dict(BASE_SETTINGS))
    proc, reason, message, data = worker.start_video_process(src, blocker / "sub" / "a_archived.mp4", None, None, None)
    assert proc is None and reason.startswith("Startfehler") and data == {"failed": True} and "a.mp4" in message
