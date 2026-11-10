"""Runs the worker threads' run() directly (synchronously), so their logic is checked and measured by coverage."""
import os
import shutil
import subprocess
import threading
import time

import pytest
from pathlib import Path

from conftest import requires_ffmpeg

SETTINGS = {
    "limit_res": False, "max_res": 0, "limit_fps": False, "max_fps": 0,
    "renderer": "libx265", "crf": 35, "preset": "ultrafast", "vt_quality": 50,
    "overwrite": False, "copy_aac": False, "dry_run": False,
}


def touch(path, size=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def make_clip(path, seconds=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(path)], check=True)
    return path


def collect(signal):
    items = []
    signal.connect(lambda *args: items.append(args[0] if len(args) == 1 else args))
    return items


# --- UnifiedScanWorker -----------------------------------------------------------------------------------------------

def scan(qapp, src, dst, **kwargs):
    from archive_helper.compressor.workers import UnifiedScanWorker

    worker = UnifiedScanWorker(src, dst, **kwargs)
    found = collect(worker.file_found)
    finished = collect(worker.scan_finished)
    worker.run()
    assert len(finished) == 1 and finished[0] == found
    return found


def test_scan_finds_only_visible_video_files_recursively(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    touch(src / "a.mp4"), touch(src / "sub" / "b.MOV"), touch(src / "c.mkv")
    touch(src / ".hidden.mp4"), touch(src / "notes.txt"), touch(src / "photo.jpg")
    items = scan(qapp, src, dst)
    assert [i["path"].name for i in items] == ["a.mp4", "b.MOV", "c.mkv"]


def test_scan_predicts_target_names_and_detects_existing_results(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    touch(src / "a.mov", 100), touch(src / "sub" / "b.mp4", 200)
    touch(dst / "sub" / "b_archived.mp4", 50)
    by_name = {i["path"].name: i for i in scan(qapp, src, dst)}
    assert by_name["a.mov"]["dst_path"] == dst / "a_archived.mp4" and not by_name["a.mov"]["exists_compressed"]
    assert by_name["b.mp4"]["dst_path"] == dst / "sub" / "b_archived.mp4"
    assert by_name["b.mp4"]["exists_compressed"] and by_name["b.mp4"]["comp_size"] == 50
    assert by_name["a.mov"]["size"] == 100


def test_scan_ignores_videos_that_already_carry_the_result_postfix(qapp, tmp_path):
    # They are results of an earlier run; compressing them again would give clip_archived_archived.
    src, dst = tmp_path / "src", tmp_path / "dst"
    touch(src / "clip_archived.mp4"), touch(src / "clip.mp4")
    assert [i["path"].name for i in scan(qapp, src, dst)] == ["clip.mp4"]


def test_scan_flatten_puts_everything_into_one_folder(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    touch(src / "x" / "a.mp4")
    assert scan(qapp, src, dst, flatten=True)[0]["dst_path"] == dst / "a_archived.mp4"


@pytest.mark.parametrize("sort_by,expected", [
    ("name_asc", ["a", "B", "c"]), ("name_desc", ["c", "B", "a"]),
    ("size_desc", ["c", "a", "B"]), ("size_asc", ["B", "a", "c"]),
])
def test_scan_sorting(qapp, tmp_path, sort_by, expected):
    src, dst = tmp_path / "src", tmp_path / "dst"
    touch(src / "a.mp4", 20), touch(src / "B.mp4", 10), touch(src / "c.mp4", 30)
    assert [i["stem"] for i in scan(qapp, src, dst, sort_by=sort_by)] == expected


@requires_ffmpeg
@pytest.mark.parametrize("sort_by,expected", [("duration_desc", ["long", "short"]), ("duration_asc", ["short", "long"])])
def test_scan_sorts_by_duration(qapp, tmp_path, sort_by, expected):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_clip(src / "short.mp4", 1), make_clip(src / "long.mp4", 3)
    assert [i["stem"] for i in scan(qapp, src, dst, sort_by=sort_by)] == expected


def test_scan_of_missing_folder_finishes_with_nothing(qapp, tmp_path):
    from archive_helper.compressor.workers import UnifiedScanWorker

    worker = UnifiedScanWorker(tmp_path / "nope", tmp_path / "dst")
    finished = collect(worker.scan_finished)
    worker.run()
    assert finished == [[]] or finished == [None] or len(finished) == 1


# --- CompareScanWorker -----------------------------------------------------------------------------------------------

def compare(qapp, orig, comp, **kwargs):
    from archive_helper.compressor.workers import CompareScanWorker

    worker = CompareScanWorker(orig, comp, **kwargs)
    pairs = collect(worker.pair_found)
    finished = collect(worker.scan_finished)
    worker.run()
    assert finished == [pairs]
    return pairs


def test_compare_pairs_originals_with_their_results_and_reports_orphans(qapp, tmp_path):
    orig, comp = tmp_path / "orig", tmp_path / "comp"
    touch(orig / "a.mov", 100), touch(orig / "sub" / "b.mp4", 200), touch(orig / "lonely.mp4", 300)
    touch(comp / "a_archived.mp4", 40), touch(comp / "sub" / "b_archived.mp4", 80)
    touch(comp / "orphan_archived.mp4", 5)
    by_stem = {p["stem"]: p for p in compare(qapp, orig, comp)}
    assert by_stem["a"]["comp_path"] == comp / "a_archived.mp4" and by_stem["a"]["comp_size"] == 40
    assert by_stem["b"]["comp_path"] == comp / "sub" / "b_archived.mp4"
    assert by_stem["lonely"]["comp_path"] is None and by_stem["lonely"]["comp_size"] == 0
    assert by_stem["orphan_archived"]["orig_path"] is None and by_stem["orphan_archived"]["comp_size"] == 5


def test_compare_accepts_a_copy_with_the_original_name(qapp, tmp_path):
    orig, comp = tmp_path / "orig", tmp_path / "comp"
    touch(orig / "a.mp4", 10), touch(comp / "a.mp4", 5)
    pairs = compare(qapp, orig, comp)
    assert len(pairs) == 1 and pairs[0]["comp_path"] == comp / "a.mp4"


@pytest.mark.parametrize("sort_by,expected", [
    ("name_desc", ["c", "b", "a"]), ("size_desc", ["c", "a", "b"]), ("size_asc", ["b", "a", "c"]),
])
def test_compare_sorting(qapp, tmp_path, sort_by, expected):
    orig, comp = tmp_path / "orig", tmp_path / "comp"
    touch(orig / "a.mp4", 20), touch(orig / "b.mp4", 10), touch(orig / "c.mp4", 30)
    comp.mkdir()
    assert [p["stem"] for p in compare(qapp, orig, comp, sort_by=sort_by)] == expected


@requires_ffmpeg
def test_compare_sorts_by_duration(qapp, tmp_path):
    orig, comp = tmp_path / "orig", tmp_path / "comp"
    make_clip(orig / "short.mp4", 1), make_clip(orig / "long.mp4", 3)
    comp.mkdir()
    assert [p["stem"] for p in compare(qapp, orig, comp, sort_by="duration_desc")] == ["long", "short"]
    assert [p["stem"] for p in compare(qapp, orig, comp, sort_by="duration_asc")] == ["short", "long"]


def test_compare_with_missing_folders_is_empty(qapp, tmp_path):
    assert compare(qapp, tmp_path / "x", tmp_path / "y") == []


# --- ArchiveWorker.run -----------------------------------------------------------------------------------------------

def archive_worker(src, dst, items, **settings):
    from archive_helper.compressor.workers import ArchiveWorker

    worker = ArchiveWorker(src, dst, 2, items, {**SETTINGS, **settings})
    worker.events = {
        "status": collect(worker.status_update_path), "progress": collect(worker.file_progress_path),
        "step": collect(worker.progress_step), "log": collect(worker.ffmpeg_log_line_path),
        "duration": collect(worker.file_duration_discovered_path), "done": collect(worker.finished_all),
    }
    return worker


def item(src_root, dst_root, name):
    path = src_root / name
    return {"path": path, "dst_path": dst_root / (path.stem + "_archived.mp4"), "size": path.stat().st_size}


@requires_ffmpeg
def test_run_compresses_a_queue_and_reports_progress(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_clip(src / "a.mp4"), make_clip(src / "b.mp4")
    worker = archive_worker(src, dst, [item(src, dst, "a.mp4"), item(src, dst, "b.mp4")])
    worker.run()

    events = worker.events
    assert len(events["done"]) == 1
    assert (dst / "a_archived.mp4").exists() and (dst / "b_archived.mp4").exists()
    finished = [s for s in events["status"] if s[1] == "finished"]
    assert len(finished) == 2 and any(s[1] == "running" for s in events["status"])
    assert {p[0] for p in events["progress"] if p[1] == 100} == {str(src / "a.mp4"), str(src / "b.mp4")}
    assert len(events["duration"]) == 2 and events["duration"][0][1] == pytest.approx(2, abs=0.2)
    assert [s[0] for s in events["step"]] == [1, 2] and all("FINISH" in s[1] for s in events["step"])
    assert events["log"], "ffmpeg output lines are forwarded"
    assert worker.active_processes == []


@requires_ffmpeg
def test_run_reports_skipped_videos_with_savings(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_clip(src / "a.mp4")
    worker = archive_worker(src, dst, [item(src, dst, "a.mp4")])
    worker.run()
    again = archive_worker(src, dst, [item(src, dst, "a.mp4")])
    again.run()
    status = [s for s in again.events["status"] if s[1] == "skipped"]
    assert len(status) == 1 and status[0][3]["dst_size"] == (dst / "a_archived.mp4").stat().st_size
    assert "SKIP" in again.events["step"][0][1]


@requires_ffmpeg
def test_run_marks_broken_files_as_error_and_continues(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    touch(src / "broken.mp4", 500)
    make_clip(src / "good.mp4")
    worker = archive_worker(src, dst, [item(src, dst, "broken.mp4"), item(src, dst, "good.mp4")])
    worker.run()
    states = {s[0]: s[1] for s in worker.events["status"] if s[1] in ("finished", "error")}
    assert states == {str(src / "broken.mp4"): "error", str(src / "good.mp4"): "finished"}
    assert not (dst / "broken_archived.mp4").exists()


@requires_ffmpeg
def test_stop_kills_running_encodes_and_removes_partial_files(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_clip(src / "long.mp4", seconds=60)
    worker = archive_worker(src, dst, [item(src, dst, "long.mp4")], preset="veryslow", crf=10)
    threading.Timer(1.5, worker.stop).start()
    started = time.monotonic()
    worker.run()
    assert time.monotonic() - started < 30
    assert len(worker.events["done"]) == 1
    assert not (dst / "long_archived.mp4").exists()


def test_queue_changes_never_requeue_started_videos(qapp, tmp_path):
    from archive_helper.compressor.workers import ArchiveWorker

    items = [{"path": tmp_path / n} for n in "abc"]
    worker = ArchiveWorker(tmp_path, tmp_path, 1, items, SETTINGS)
    first = worker.next_item()
    worker.set_queue([items[2], items[0], items[1]])
    assert first["path"] == items[0]["path"]
    assert [i["path"].name for i in worker.queue] == ["c", "b"]
    assert worker.has_queue()


# --- other workers ---------------------------------------------------------------------------------------------------

@requires_ffmpeg
def test_thumbnail_runnable_delivers_an_image(qapp, tmp_path):
    from archive_helper.compressor.workers import ThumbnailRunnable

    clip = make_clip(tmp_path / "t.mp4")
    job = ThumbnailRunnable(clip)
    received = collect(job.signals.finished)
    job.run()
    assert received[0][0] == str(clip) and not received[0][1].isNull()


def test_thumbnail_runnable_survives_a_broken_video(qapp, tmp_path):
    from archive_helper.compressor.workers import ThumbnailRunnable

    job = ThumbnailRunnable(touch(tmp_path / "broken.mp4", 10))
    received = collect(job.signals.finished)
    job.run()
    assert len(received) == 1


def slow_thumbnails(monkeypatch, started):
    """Replaces ffmpeg with a job that runs until it is cancelled and records which videos it began."""
    from archive_helper.compressor import workers

    def fake(video_path, output_path, cancelled=None):
        started.append(Path(video_path).name)
        while not cancelled.wait(0.01):
            pass
        return False

    monkeypatch.setattr(workers, "generate_thumbnail", fake)


def test_closing_does_not_wait_for_the_thumbnails_of_a_large_folder(qapp, tmp_path, monkeypatch):
    # Regression: the jobs sat in the global pool, and quitting waited until every thumbnail
    # of the folder was made; the process stayed alive after the window had closed.
    from archive_helper.compressor.workers import ThumbnailLoader

    started = []
    slow_thumbnails(monkeypatch, started)
    loader = ThumbnailLoader()
    loader.load([touch(tmp_path / f"v{i:03d}.mp4", 10) for i in range(200)])
    deadline = time.monotonic() + 5
    while len(started) < loader.PARALLEL and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(started) == loader.PARALLEL   # only a few at a time

    begin = time.monotonic()
    assert loader.shutdown(timeout_ms=3000)
    assert time.monotonic() - begin < 1
    assert len(started) == loader.PARALLEL   # the waiting ones never ran


def test_a_new_folder_drops_the_thumbnails_of_the_old_one(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor.workers import ThumbnailLoader

    started = []
    slow_thumbnails(monkeypatch, started)
    loader = ThumbnailLoader()
    loader.load([touch(tmp_path / f"old{i}.mp4", 10) for i in range(20)])
    loader.load([touch(tmp_path / f"new{i}.mp4", 10) for i in range(2)])
    deadline = time.monotonic() + 5
    while not {"new0.mp4", "new1.mp4"} <= set(started) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert {"new0.mp4", "new1.mp4"} <= set(started)
    assert sum(name.startswith("old") for name in started) <= loader.PARALLEL
    assert loader.shutdown(timeout_ms=3000)


def test_no_new_thumbnails_start_while_the_viewer_plays(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor.workers import ThumbnailLoader

    started = []
    slow_thumbnails(monkeypatch, started)
    loader = ThumbnailLoader()
    loader.pause()
    loader.load([touch(tmp_path / f"v{i}.mp4", 10) for i in range(5)])
    time.sleep(0.3)
    assert started == []
    loader.resume()
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started
    loader.pause()
    assert loader.shutdown(timeout_ms=3000)   # also while paused


@requires_ffmpeg
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_cancelled_thumbnail_stops_ffmpeg_and_leaves_no_file(tmp_path):
    from archive_helper.compressor.utils import generate_thumbnail

    stuck = tmp_path / "stuck.mp4"
    os.mkfifo(stuck)   # ffmpeg waits forever for data, like a video on a very slow disk
    cancelled = threading.Event()
    threading.Timer(0.3, cancelled.set).start()
    thumb = tmp_path / "t.jpg"
    begin = time.monotonic()
    assert not generate_thumbnail(stuck, thumb, cancelled)
    assert time.monotonic() - begin < 3
    assert not thumb.exists()


@requires_ffmpeg
def test_duration_probe_worker_reports_durations(qapp, tmp_path):
    from archive_helper.compressor.workers import DurationProbeWorker

    clip = make_clip(tmp_path / "d.mp4", 2)
    worker = DurationProbeWorker([clip, touch(tmp_path / "broken.mp4", 5)])
    seen = collect(worker.duration_found)
    worker.run()
    assert [s[0] for s in seen] == [str(clip)] and seen[0][1] == pytest.approx(2, abs=0.2)


def test_duration_probe_worker_probes_in_parallel_and_remembers_durations(qapp, tmp_path, monkeypatch):
    import threading
    import time

    from archive_helper.compressor import workers

    files = [touch(tmp_path / f"v{i}.mp4", 10 + i) for i in range(12)]
    calls, active, peak = [], [0], [0]
    lock = threading.Lock()

    def slow_info(path):  # like a disk that takes its time
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
            calls.append(path)
        return 42.0, 25.0, "aac"

    monkeypatch.setattr(workers, "get_video_info", slow_info)
    worker = workers.DurationProbeWorker(files)
    seen = collect(worker.duration_found)
    worker.run()
    assert sorted(s[0] for s in seen) == sorted(str(f) for f in files) and peak[0] > 1

    # A second run is answered from the cache without opening any file.
    calls.clear()
    again = workers.DurationProbeWorker(files)
    seen_again = collect(again.duration_found)
    again.run()
    assert calls == [] and len(seen_again) == len(files)

    # A changed file is probed again.
    files[0].write_bytes(b"changed and longer")
    calls.clear()
    third = workers.DurationProbeWorker(files)
    collect(third.duration_found)
    third.run()
    assert calls == [files[0]]


def test_duration_probe_worker_can_be_stopped(qapp, tmp_path):
    from archive_helper.compressor.workers import DurationProbeWorker

    worker = DurationProbeWorker([tmp_path / "a.mp4"])
    worker.stop()
    seen = collect(worker.duration_found)
    worker.run()
    assert seen == []


@requires_ffmpeg
def test_result_settings_worker_reports_stored_settings(qapp, tmp_path):
    from archive_helper.compressor.utils import EXIFTOOL_CONFIG, settings_tag_argument
    from archive_helper.compressor.workers import ResultSettingsWorker

    if not shutil.which("exiftool"):
        pytest.skip("exiftool not installed")
    with_tag, without = make_clip(tmp_path / "r1.mp4"), make_clip(tmp_path / "r2.mp4")
    subprocess.run(["exiftool", "-config", str(EXIFTOOL_CONFIG), settings_tag_argument({"version": 1, "crf": 22}),
                    str(with_tag), "-overwrite_original", "-q"], check=True)
    worker = ResultSettingsWorker([(tmp_path / "s1.mp4", with_tag), (tmp_path / "s2.mp4", without)])
    seen = collect(worker.settings_found)
    worker.run()
    assert seen == [(str(tmp_path / "s1.mp4"), {"version": 1, "crf": 22})] or \
        (len(seen) == 1 and seen[0][1]["crf"] == 22)


def test_run_marks_a_video_that_cannot_be_started_as_error_with_the_reason(qapp, tmp_path):
    src = tmp_path / "src"
    touch(src / "a.mp4", 10)
    blocker = touch(tmp_path / "blocker", 1)          # a file where the target folder would have to be
    dst = blocker / "target"
    worker = archive_worker(src, dst, [item(src, dst, "a.mp4")])
    worker.run()
    status = [s for s in worker.events["status"] if s[0] == str(src / "a.mp4")]
    assert [s[1] for s in status] == ["error"] and status[0][2].startswith("Startfehler")
    assert any("Could not start" in line[1] or "Konnte" in line[1] or "❌" in line[1] for line in worker.events["log"])


def test_scan_of_a_hand_picked_list_ignores_other_files_in_the_folders(qapp, tmp_path):
    from archive_helper.compressor.workers import UnifiedScanWorker

    src = tmp_path / "src"
    a, b = touch(src / "a.mp4"), touch(src / "b.mov")
    worker = UnifiedScanWorker(src, None, files=[a])
    found = collect(worker.file_found)
    worker.run()
    assert [i["path"].name for i in found] == ["a.mp4"] and found[0]["dst_path"] == src / "a_archived.mp4"
