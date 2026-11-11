"""Compression lab: segment encoding, size estimate, frames, the dialog and its link to the main page."""
import shutil
import subprocess
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint

from conftest import requires_ffmpeg, spin
from test_compressor import make_gui  # noqa: F401  (fixture)

BASE = {
    'limit_res': True, 'max_res': 1280, 'limit_fps': True, 'max_fps': 30, 'renderer': 'libx265', 'crf': 28,
    'preset': 'ultrafast', 'vt_quality': 65, 'overwrite': False, 'copy_aac': True, 'dry_run': False,
    'low_priority': False,
}


def make_clip(path, seconds=4, size="640x360"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(path)], check=True)
    return path


def uids(dialog):
    return [c.uid for c in dialog.columns]


def wait_for_thread(qapp, worker, timeout=120):
    import time

    deadline = time.monotonic() + timeout
    while worker.isRunning() and time.monotonic() < deadline:
        spin(20)
    qapp.processEvents()


# --- pure helpers ---------------------------------------------------------------------------------------------------

def test_total_size_is_scaled_from_the_part():
    from archive_helper.compressor.lab import estimate_total_size

    assert estimate_total_size(1_000_000, 10, 100) == 10_000_000
    assert estimate_total_size(1_000_000, 10, 5) == 500_000
    assert estimate_total_size(1_000_000, 0, 100) is None and estimate_total_size(1, 10, None) is None


@pytest.mark.parametrize("duration,expected", [(100, (0, 10)), (30, (0, 10)), (4, (0, 4)), (0.5, (0, 1)), (None, (0, 10))])
def test_default_part_is_the_first_seconds(duration, expected):
    from archive_helper.compressor.lab import default_segment

    assert default_segment(duration) == expected


def test_variants_start_from_the_drawer_with_small_archive_and_high_quality():
    from archive_helper.compressor.lab import variant_defaults

    variants = variant_defaults(BASE)
    assert [v['crf'] for v in variants] == [26, 20, 17] and [v['vt_quality'] for v in variants] == [45, 65, 80]
    assert all(v['max_res'] == 1280 and v['preset'] == 'ultrafast' and not v['dry_run'] and not v['overwrite'] for v in variants)
    assert BASE['crf'] == 28                                       # the drawer's dictionary is not changed


# --- the ffmpeg call ------------------------------------------------------------------------------------------------

@requires_ffmpeg
def test_command_for_a_part_seeks_before_the_input_and_limits_the_length(tmp_path):
    from archive_helper.compressor.workers import ArchiveWorker

    clip = make_clip(tmp_path / "a.mp4")
    worker = ArchiveWorker(tmp_path, tmp_path, 1, [], dict(BASE))
    whole = worker.build_ffmpeg_command(clip, tmp_path / "out.mp4", 25)
    part = worker.build_ffmpeg_command(clip, tmp_path / "out.mp4", 25, segment=(3, 7))
    assert "-ss" not in whole and "-t" not in whole
    assert part.index("-ss") < part.index("-i") and part[part.index("-ss") + 1] == "3"
    assert part[part.index("-t") + 1] == "7" and part.index("-t") > part.index("-i")
    without = [a for i, a in enumerate(part) if part[i - 1:i] != ["-ss"] and a != "-ss"]
    assert [a for a in without if a not in ("-t", "7")] == [a for a in whole]      # nothing else differs


@requires_ffmpeg
def test_the_normal_run_uses_the_same_command_builder(tmp_path):
    from archive_helper.compressor.utils import get_video_info, partial_path
    from archive_helper.compressor.workers import ArchiveWorker

    clip = make_clip(tmp_path / "a.mp4")
    worker = ArchiveWorker(tmp_path, tmp_path / "dst", 1, [], dict(BASE))
    duration, fps, audio = get_video_info(clip)
    proc, *_ = worker.start_video_process(clip, tmp_path / "dst" / "a_archived.mp4", duration, fps, audio)
    expected = worker.build_ffmpeg_command(clip, partial_path(tmp_path / "dst" / "a_archived.mp4"), fps)
    proc.communicate(timeout=60)
    assert proc.returncode == 0 and expected[-1].endswith("a_archived.partial.mp4")
    assert (tmp_path / "dst" / "a_archived.partial.mp4").exists()      # the real run wrote where the builder said


# --- workers --------------------------------------------------------------------------------------------------------

@requires_ffmpeg
def test_lab_worker_encodes_every_variant_for_the_part_only(qapp, tmp_path):
    from archive_helper.compressor.lab import LabWorker, variant_defaults

    clip = make_clip(tmp_path / "a.mp4", seconds=6)
    done, started = [], []
    worker = LabWorker(clip, (1, 2), list(enumerate(variant_defaults(BASE))), tmp_path / "lab", 25)
    worker.variant_started.connect(started.append)
    worker.variant_done.connect(lambda i, path, size, seconds: done.append((i, Path(path), size, seconds)))
    (tmp_path / "lab").mkdir()
    worker.run()
    assert started == [0, 1, 2] and [d[0] for d in done] == [0, 1, 2]
    assert all(path.exists() and size == path.stat().st_size > 0 and seconds >= 0 for _, path, size, seconds in done)
    assert done[0][2] < done[2][2]                                    # CRF 26 gives a smaller file than CRF 17
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(done[0][1])],
                                    capture_output=True, text=True).stdout)
    assert duration == pytest.approx(2, abs=0.3)                     # only the 2 seconds were encoded


@requires_ffmpeg
def test_lab_worker_reports_a_variant_that_fails(qapp, tmp_path):
    from archive_helper.compressor.lab import LabWorker, variant_defaults

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    (tmp_path / "lab").mkdir()
    failed = []
    worker = LabWorker(broken, (0, 2), list(enumerate(variant_defaults(BASE)[:1])), tmp_path / "lab", 25)
    worker.variant_failed.connect(lambda i, message: failed.append((i, message)))
    worker.run()
    assert len(failed) == 1 and failed[0][0] == 0 and failed[0][1]


@requires_ffmpeg
def test_lab_worker_can_be_stopped(qapp, tmp_path):
    from archive_helper.compressor.lab import LabWorker, variant_defaults

    clip = make_clip(tmp_path / "a.mp4", seconds=20, size="1280x720")
    (tmp_path / "lab").mkdir()
    slow = [{**v, 'preset': 'veryslow'} for v in variant_defaults(BASE)]
    worker = LabWorker(clip, (0, 20), list(enumerate(slow)), tmp_path / "lab", 25)
    done = []
    worker.variant_done.connect(lambda *args: done.append(args))
    worker.start()
    spin(300)
    worker.stop()
    wait_for_thread(qapp, worker, 30)
    assert not worker.isRunning() and done == []


@requires_ffmpeg
def test_frame_worker_returns_frames_at_the_requested_size(qapp, tmp_path):
    from archive_helper.compressor.lab import FrameWorker

    clip = make_clip(tmp_path / "a.mp4")
    frames = {}
    worker = FrameWorker([("plain", clip, 1, None, False), ("scaled", clip, 1, (1280, 720), False), ("missing", tmp_path / "none.mp4", 0, None, False)])
    worker.frame_ready.connect(lambda key, image: frames.update({key: image}))
    worker.run()
    assert (frames["plain"].width(), frames["plain"].height()) == (640, 360)
    assert (frames["scaled"].width(), frames["scaled"].height()) == (1280, 720)
    assert "missing" not in frames


# --- the dialog -----------------------------------------------------------------------------------------------------

@pytest.fixture
def lab(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog

    clip = make_clip(tmp_path / "a.mp4", seconds=6)
    applied = []
    dialog = CompressionLabDialog(clip, None, dict(BASE), applied.append)
    dialog.applied = applied
    dialog.clip = clip
    dialog.show()
    yield dialog
    dialog.close()


@requires_ffmpeg
def test_dialog_starts_with_three_variants_and_the_first_seconds(qapp, lab):
    assert [c.spin_quality.value() for c in lab.columns] == [26, 20, 17]
    assert lab.segment() == (0, 6)                                      # the first seconds; a 6 s video is shorter than 10 s
    assert lab.frame_size == (640, 360)


@requires_ffmpeg
def test_encoding_fills_in_sizes_estimates_and_frames(qapp, lab):
    lab.encode()
    assert not lab.btn_encode.isEnabled() and lab.btn_cancel.isEnabled()
    wait_for_thread(qapp, lab.lab_worker)
    wait_for_thread(qapp, lab.frame_worker)
    spin(200)
    assert lab.btn_encode.isEnabled() and not lab.btn_cancel.isEnabled()
    assert set(lab.results) == set(uids(lab)) and set(lab.variant_files) == set(uids(lab))
    for column in lab.columns:
        assert "Ausschnitt:" in column.result.text() and "Ganzes Video: etwa" in column.result.text()
    assert all(view.has_image() for view in lab.views)
    sizes = [lab.results[u][0] for u in uids(lab)]
    assert sizes[0] < sizes[1] < sizes[2]


@requires_ffmpeg
def test_all_views_show_frames_of_the_same_size_so_they_scroll_together(qapp, lab):
    lab.encode()
    wait_for_thread(qapp, lab.lab_worker)
    wait_for_thread(qapp, lab.frame_worker)
    spin(200)
    lab.set_zoom(2.0)
    qapp.processEvents()
    assert all(v.item.pixmap().size() == lab.views[0].item.pixmap().size() for v in lab.views)
    lab.views[2].horizontalScrollBar().setValue(37)
    lab.views[2].verticalScrollBar().setValue(21)
    assert {v.horizontalScrollBar().value() for v in lab.views} == {37}
    assert {v.verticalScrollBar().value() for v in lab.views} == {21}
    lab.set_zoom(None)
    assert all(v.zoom is None for v in lab.views)


@requires_ffmpeg
def test_moving_the_frame_slider_loads_new_frames(qapp, lab):
    lab.encode()
    wait_for_thread(qapp, lab.lab_worker)
    wait_for_thread(qapp, lab.frame_worker)
    before = lab.views[0].item.pixmap().toImage()
    lab.slider_frame.setValue(950)
    assert lab.frame_seconds() == pytest.approx(lab.segment()[1] * 0.95)
    spin(600)
    wait_for_thread(qapp, lab.frame_worker)
    assert lab.views[0].item.pixmap().toImage() != before


@requires_ffmpeg
def test_using_a_variant_hands_its_settings_back(qapp, lab):
    lab.columns[1].spin_quality.setValue(23)
    lab.columns[1].combo_preset.setCurrentText("slow")
    lab.use_variant(1)
    assert len(lab.applied) == 1
    settings = lab.applied[0]
    assert settings['crf'] == 23 and settings['preset'] == 'slow' and settings['renderer'] == 'libx265'
    assert settings['limit_res'] and settings['max_res'] == 1280 and settings['limit_fps'] and settings['max_fps'] == 30
    assert "Variante B" in lab.status.text()


def test_variant_column_follows_the_chosen_encoder(qapp):
    from archive_helper.compressor.lab import VariantColumn

    column = VariantColumn("A", dict(BASE))
    column.show()
    assert column.combo_preset.isVisible() and column.spin_quality.maximum() == 51
    column.combo_encoder.setCurrentIndex(1)
    assert not column.combo_preset.isVisible() and column.spin_quality.maximum() == 100
    column.spin_quality.setValue(77)
    settings = column.settings()
    assert settings['renderer'] == 'videotoolbox' and settings['vt_quality'] == 77 and settings['crf'] == 28
    column.combo_res.setCurrentIndex(0)
    column.combo_fps.setCurrentIndex(0)
    assert column.settings()['limit_res'] is False and column.settings()['limit_fps'] is False


@requires_ffmpeg
def test_closing_the_dialog_removes_the_temporary_files(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog

    clip = make_clip(tmp_path / "a.mp4")
    dialog = CompressionLabDialog(clip, None, dict(BASE), lambda s: None)
    dialog.show()
    dialog.encode()
    wait_for_thread(qapp, dialog.lab_worker)
    assert any(dialog.tmpdir.iterdir())
    tmpdir = dialog.tmpdir
    dialog.close()
    assert not tmpdir.exists()


@requires_ffmpeg
def test_stopping_the_encoding_re_enables_the_encode_button(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog

    clip = make_clip(tmp_path / "a.mp4", seconds=30, size="1280x720")
    settings = {**BASE, 'preset': 'veryslow'}
    dialog = CompressionLabDialog(clip, None, settings, lambda s: None)
    dialog.show()
    dialog.encode()
    spin(300)
    dialog.stop_encoding()
    wait_for_thread(qapp, dialog.lab_worker, 30)
    spin(100)
    assert dialog.btn_encode.isEnabled() and dialog.results == {}
    assert all(c.result.text() == "Abgebrochen" for c in dialog.columns)
    dialog.close()


# --- link to the main page --------------------------------------------------------------------------------------------

def test_drawer_settings_survive_a_round_trip(qapp, make_gui):
    gui = make_gui()
    gui.combo_renderer.setCurrentIndex(1)
    gui.slider_vt.setValue(70)
    gui.combo_res.setCurrentIndex(4)
    gui.combo_fps.setCurrentIndex(0)
    settings = gui.current_run_settings()
    other = make_gui()
    other.apply_run_settings(settings)
    assert other.current_run_settings() == settings
    assert settings['renderer'] == 'videotoolbox' and settings['vt_quality'] == 70 and settings['max_res'] == 1280
    assert settings['limit_fps'] is False


def test_the_detail_bar_offers_the_lab_next_to_process_next(qapp, make_gui, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    from archive_helper.compressor import gui as gui_module
    from archive_helper.compressor.model import VideoRow

    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    gui = make_gui()
    gui.show()
    gui.model.reset([VideoRow(src=video, dst=tmp_path / "a_x.mp4", size=1, duration=12.0, root=tmp_path)])
    gui.scan_items = [{"path": video, "dst_path": tmp_path / "a_x.mp4", "size": 1, "exists_compressed": False}]
    gui.table.selectRow(0)
    qapp.processEvents()
    texts = [b.text() for b in gui.detail_bar.findChildren(QPushButton)]
    assert texts.index("Verschiedene Kompressionen prüfen") == texts.index("Als Nächstes") + 1

    opened = []

    class Stub:
        def __init__(self, *args, **kwargs):
            opened.append((*args, kwargs))

        def show(self):
            pass

    monkeypatch.setattr(gui_module, "CompressionLabDialog", Stub)
    gui.combo_renderer.setCurrentIndex(0)
    gui.slider_crf.setValue(31)
    gui.open_lab(gui.model.rows[0])
    src, duration, settings, callback, parent, kwargs = opened[0]
    assert src == video and duration == 12.0 and settings['crf'] == 31 and parent is gui
    assert kwargs == {'queue': (1, pytest.approx(12.0))}
    callback({**settings, 'crf': 19})
    assert gui.slider_crf.value() == 19


# --- estimate, progress, more variants, HDR ------------------------------------------------------------------------------

def test_next_variant_has_better_quality_and_leaves_the_original_untouched():
    from archive_helper.compressor.lab import next_variant

    base = {**BASE, 'crf': 17, 'vt_quality': 95}
    more = next_variant(base)
    assert more['crf'] == 14 and more['vt_quality'] == 100 and base['crf'] == 17
    assert next_variant({**BASE, 'crf': 1})['crf'] == 0


def test_remaining_time_is_estimated_from_what_was_measured():
    from archive_helper.compressor.lab import estimate_remaining

    assert estimate_remaining([], None, 0, 3) is None                       # nothing measured yet
    assert estimate_remaining([], 0.01, 5, 2) is None                       # too early to tell
    assert estimate_remaining([], 0.25, 5, 2) == pytest.approx(15 + 2 * 20)   # 20 s per variant: 15 left, two more
    assert estimate_remaining([10, 20], None, 0, 1) == pytest.approx(15)
    assert estimate_remaining([10], 0.5, 4, 0) == pytest.approx(4)           # the running one will take 8 s
    assert estimate_remaining([10], 0.0, 3, 0) == pytest.approx(7)           # started, no progress line yet: like the last
    assert estimate_remaining([10], 0.9, 30, 0) == pytest.approx(10 / 3)     # slower than the one before: judged by itself
    assert estimate_remaining([5], 1.0, 9, 0) == 0                            # done, never negative


@requires_ffmpeg
def test_lab_worker_reports_progress_for_each_variant(qapp, tmp_path):
    from archive_helper.compressor.lab import LabWorker, variant_defaults

    clip = make_clip(tmp_path / "a.mp4", seconds=8, size="1280x720")
    (tmp_path / "lab").mkdir()
    progress = []
    worker = LabWorker(clip, (0, 8), list(enumerate(variant_defaults({**BASE, 'preset': 'medium'})[:2])), tmp_path / "lab", 25)
    worker.variant_progress.connect(lambda i, p: progress.append((i, p)))
    worker.run()
    for index in (0, 1):
        values = [p for i, p in progress if i == index]
        assert values and values[-1] == 100 and values == sorted(values)
        assert all(0 <= v <= 100 for v in values) and len(set(values)) > 1


@requires_ffmpeg
def test_progress_bars_and_the_time_estimate_follow_the_encoding(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog

    clip = make_clip(tmp_path / "a.mp4", seconds=8, size="1280x720")
    dialog = CompressionLabDialog(clip, None, {**BASE, 'preset': 'medium'}, lambda s: None)
    dialog.show()
    dialog.encode()
    estimate, partial = None, None
    while dialog.encoding():
        spin(50)
        # ffmpeg's own reports may not fall between two looks on a busy machine, so one is given to the dialog here
        if dialog.running and estimate is None:
            dialog.on_variant_progress(dialog.running[0], 50)
            estimate = dialog.estimate_label.text()
            partial = [c.progress.value() for c in dialog.columns]
    spin(200)
    assert estimate.startswith("noch etwa") or "geschätzt" in estimate
    assert 50 in partial
    assert [c.progress.value() for c in dialog.columns] == [100, 100, 100] and dialog.estimate_label.text() == ""
    dialog.close()


@requires_ffmpeg
def test_more_variants_can_be_added_and_removed_and_the_letters_follow(qapp, lab):
    from archive_helper.compressor.lab import MAX_VARIANTS

    lab.add_variant()
    assert len(lab.columns) == 4 and len(lab.views) == 5
    assert lab.columns[3].spin_quality.value() == 14 and "D" in lab.columns[3].title.text()
    for _ in range(10):
        lab.add_variant()
    assert len(lab.columns) == MAX_VARIANTS and not lab.btn_add.isEnabled()
    while len(lab.columns) > 1:
        lab.remove_variant(0)
    assert len(lab.columns) == 1 and lab.columns[0].title.text().endswith("A")
    assert not lab.columns[0].remove_button.isVisible() or not lab.columns[0].remove_button.isVisibleTo(lab)
    lab.remove_variant(0)                                                     # the last one stays
    assert len(lab.columns) == 1 and len(lab.views) == 2


@requires_ffmpeg
def test_removing_a_variant_keeps_the_results_of_the_others(qapp, lab):
    lab.encode()
    wait_for_thread(qapp, lab.lab_worker)
    wait_for_thread(qapp, lab.frame_worker)
    first, second, third = uids(lab)
    sizes = {u: lab.results[u] for u in (first, second, third)}
    lab.remove_variant(1)
    assert lab.results == {first: sizes[first], third: sizes[third]}
    assert set(lab.variant_files) == {first, third} and uids(lab) == [first, third]


@requires_ffmpeg
def test_variants_cannot_be_added_or_removed_while_encoding(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog

    clip = make_clip(tmp_path / "a.mp4", seconds=20, size="1280x720")
    dialog = CompressionLabDialog(clip, None, {**BASE, 'preset': 'veryslow'}, lambda s: None)
    dialog.show()
    dialog.encode()
    spin(200)
    assert not dialog.btn_add.isEnabled() and not dialog.columns[0].remove_button.isEnabled()
    dialog.add_variant()
    dialog.remove_variant(0)
    assert len(dialog.columns) == 3
    dialog.stop_encoding()
    wait_for_thread(qapp, dialog.lab_worker, 30)
    spin(100)
    assert dialog.btn_add.isEnabled() and dialog.columns[0].remove_button.isEnabled()
    dialog.close()


@requires_ffmpeg
def test_the_part_can_start_anywhere_and_be_as_long_as_the_video(qapp, lab):
    assert lab.spin_start.maximum() == 5 and lab.spin_length.maximum() == 6
    lab.spin_start.setValue(3)
    lab.spin_length.setValue(2)
    lab.encode()
    wait_for_thread(qapp, lab.lab_worker)
    for uid in uids(lab):
        duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", lab.variant_files[uid]],
                                        capture_output=True, text=True).stdout)
        assert duration == pytest.approx(2, abs=0.35)


@requires_ffmpeg
def test_an_hdr_video_stays_hdr_in_every_variant_and_gets_frames(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog
    from test_compressor import make_hlg_clip

    clip = tmp_path / "hdr.mov"
    make_hlg_clip(clip)
    dialog = CompressionLabDialog(clip, None, {**BASE, 'limit_res': False}, lambda s: None)
    dialog.show()
    assert dialog.hdr
    dialog.encode()
    wait_for_thread(qapp, dialog.lab_worker)
    wait_for_thread(qapp, dialog.frame_worker)
    spin(300)
    assert set(dialog.variant_files) == set(uids(dialog))
    for path in dialog.variant_files.values():
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=profile,pix_fmt,color_transfer,color_primaries", "-of", "csv=p=0", path],
                              capture_output=True, text=True).stdout.strip()
        assert "Main 10" in info and "yuv420p10le" in info and "arib-std-b67" in info and "bt2020" in info
    assert all(view.has_image() for view in dialog.views)
    dialog.close()


@requires_ffmpeg
def test_hdr_frames_are_decoded_with_the_bt2020_matrix(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor.lab import FrameWorker
    from test_compressor import make_hlg_clip

    clip = tmp_path / "hdr.mov"
    make_hlg_clip(clip)
    seen = []
    real_run = subprocess.run
    monkeypatch.setattr("archive_helper.compressor.lab.subprocess.run", lambda cmd, **kw: seen.append(cmd) or real_run(cmd, **kw))
    frames = {}
    worker = FrameWorker([("sdr", clip, 0.5, None, False), ("hdr", clip, 0.5, None, True), ("scaled", clip, 0.5, (320, 180), True)])
    worker.frame_ready.connect(lambda key, image: frames.update({key: image}))
    worker.run()
    assert set(frames) == {"sdr", "hdr", "scaled"}
    joined = [" ".join(map(str, cmd)) for cmd in seen]
    assert "bt2020" not in joined[0] and "in_color_matrix=bt2020" in joined[1] and "scale=320:180:flags=bicubic:in_color_matrix=bt2020" in joined[2]
    assert (frames["scaled"].width(), frames["scaled"].height()) == (320, 180)


# --- the HDR gradient sample --------------------------------------------------------------------------------------------

def probe_video(path):
    import json

    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=profile,pix_fmt,color_transfer,color_primaries,color_space,width,height", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


@requires_ffmpeg
def test_the_committed_hdr_sample_is_hlg_bt2020_10_bit():
    from conftest import REPO_ROOT

    info = probe_video(REPO_ROOT / "example_resources" / "hdr-gradient-hlg.mov")
    assert (info["profile"], info["pix_fmt"], info["color_transfer"], info["color_primaries"], info["color_space"]) == \
        ("Main 10", "yuv420p10le", "arib-std-b67", "bt2020", "bt2020nc")
    assert (info["width"], info["height"]) == (1920, 1080)


@requires_ffmpeg
def test_the_sample_generator_builds_the_same_kind_of_video(tmp_path):
    from hdr_samples import make_hdr_gradient_clip

    info = probe_video(make_hdr_gradient_clip(tmp_path / "g.mov", seconds=1))
    assert (info["pix_fmt"], info["color_transfer"], info["color_space"]) == ("yuv420p10le", "arib-std-b67", "bt2020nc")


@requires_ffmpeg
def test_lower_quality_variants_of_the_gradients_are_smaller_and_stay_hlg(qapp):
    from archive_helper.compressor.lab import CompressionLabDialog
    from conftest import REPO_ROOT

    dialog = CompressionLabDialog(REPO_ROOT / "example_resources" / "hdr-gradient-hlg.mov", None,
                                  {**BASE, 'limit_res': False, 'preset': 'ultrafast'}, lambda s: None)
    dialog.show()
    assert dialog.hdr and "HDR" in dialog.original.findChildren(type(dialog.status))[1].text()
    dialog.spin_length.setValue(2)
    dialog.encode()
    wait_for_thread(qapp, dialog.lab_worker)
    wait_for_thread(qapp, dialog.frame_worker)
    spin(300)
    sizes = [dialog.results[u][0] for u in uids(dialog)]
    assert sizes[0] < sizes[1] < sizes[2]
    assert all(probe_video(path)["color_transfer"] == "arib-std-b67" for path in dialog.variant_files.values())
    assert all(view.has_image() for view in dialog.views)
    dialog.close()


# --- only new or changed variants are encoded, view controls, portrait videos, time estimates ----------------------------

def encode_and_wait(qapp, dialog):
    dialog.encode()
    wait_for_thread(qapp, dialog.lab_worker)
    wait_for_thread(qapp, dialog.frame_worker)
    spin(200)


@requires_ffmpeg
def test_adding_a_variant_encodes_only_the_new_one(qapp, lab):
    encode_and_wait(qapp, lab)
    before = {u: (lab.results[u], lab.variant_files[u]) for u in uids(lab)}
    stamps = {u: Path(lab.variant_files[u]).stat().st_mtime_ns for u in uids(lab)}
    lab.add_variant()
    new = uids(lab)[-1]
    encode_and_wait(qapp, lab)
    assert set(lab.results) == set(uids(lab)) and len(uids(lab)) == 4
    for uid in before:
        assert lab.results[uid] == before[uid][0]                                  # untouched, not encoded again
        assert Path(lab.variant_files[uid]).stat().st_mtime_ns == stamps[uid]
    assert new in lab.results


@requires_ffmpeg
def test_a_changed_variant_is_marked_and_encoded_again_alone(qapp, lab):
    encode_and_wait(qapp, lab)
    first, second, third = lab.columns
    kept = {c.uid: lab.results[c.uid] for c in (first, third)}
    nice_text, old_result = second.result.text(), lab.results[second.uid]
    second.spin_quality.setValue(second.spin_quality.value() + 5)
    assert "Geändert" in second.result.text() and "Geändert" not in first.result.text()
    second.spin_quality.setValue(second.spin_quality.value() - 5)                   # changed back: the result is valid again
    assert second.result.text() == nice_text
    second.spin_quality.setValue(second.spin_quality.value() + 5)
    encode_and_wait(qapp, lab)
    assert {c.uid: lab.results[c.uid] for c in (first, third)} == kept
    assert lab.results[second.uid] != old_result and "Geändert" not in second.result.text()


@requires_ffmpeg
def test_a_new_part_makes_every_variant_out_of_date(qapp, lab):
    encode_and_wait(qapp, lab)
    lab.spin_length.setValue(3)
    assert all("Geändert" in c.result.text() for c in lab.columns)
    encode_and_wait(qapp, lab)
    assert all(lab.is_up_to_date(c) for c in lab.columns) and all("Geändert" not in c.result.text() for c in lab.columns)


@requires_ffmpeg
def test_pressing_encode_without_changes_does_nothing(qapp, lab):
    encode_and_wait(qapp, lab)
    lab.encode()
    assert not lab.encoding() and "aktuell" in lab.status.text()


def test_zoom_steps_walk_through_the_levels():
    from archive_helper.compressor.lab import next_zoom

    assert next_zoom(None, +1) == 1.0 and next_zoom(None, -1) == 0.5
    assert next_zoom(1.0, +1) == 2.0 and next_zoom(1.0, -1) == 0.5 and next_zoom(0.25, -1) == 0.25
    assert next_zoom(8.0, +1) == 8.0 and next_zoom(0.5, +1) == 1.0


def test_frame_steps_stay_inside_the_part():
    from archive_helper.compressor.lab import step_position

    assert step_position(1.0, 5, 25, 10) == pytest.approx(1.2)
    assert step_position(1.0, -10, 25, 10) == pytest.approx(0.6)
    assert step_position(0.1, -10, 25, 10) == 0
    assert step_position(9.9, 10, 25, 10) == pytest.approx(9.96)                  # the last frame, not beyond the part
    assert step_position(0, 1, None, 10) == pytest.approx(0.04)                   # unknown frame rate: 25 fps


def test_portrait_clips_are_shown_with_their_displayed_size():
    from archive_helper.compressor.lab import display_size

    assert display_size(3840, 2160, 90) == (2160, 3840) and display_size(3840, 2160, 270) == (2160, 3840)
    assert display_size(3840, 2160, 0) == (3840, 2160) and display_size(3840, 2160, 180) == (3840, 2160)


@requires_ffmpeg
def test_view_buttons_zoom_rotate_and_step_frames(qapp, lab):
    lab.btn_zoom_in.click()
    assert lab.current_zoom == 1.0 and lab.zoom_buttons[1.0].isChecked() and all(v.zoom == 1.0 for v in lab.views)
    lab.btn_zoom_in.click()
    lab.btn_zoom_out.click()
    lab.btn_zoom_out.click()
    assert lab.current_zoom == 0.5 and lab.zoom_buttons[0.5].isChecked() and not lab.zoom_buttons[1.0].isChecked()
    lab.zoom_buttons[None].click()
    assert lab.current_zoom is None and lab.zoom_buttons[None].isChecked()
    lab.btn_rotate.click()
    assert lab.angle == 90 and all(v.angle == 90 for v in lab.views)
    for _ in range(3):
        lab.btn_rotate.click()
    assert lab.angle == 0
    lab.step_buttons[-1].click()                                                     # +10 frames
    assert lab.frame_position == pytest.approx(10 / 25) and "Bild 10" in lab.frame_label.text()
    lab.step_buttons[0].click()
    lab.step_buttons[0].click()
    assert lab.frame_position == 0
    assert lab.slider_frame.value() == 0


@requires_ffmpeg
def test_new_variants_start_with_the_current_zoom_and_rotation(qapp, lab):
    lab.set_zoom(2.0)
    lab.set_angle(90)
    lab.add_variant()
    assert lab.column_views[-1].zoom == 2.0 and lab.column_views[-1].angle == 90


@requires_ffmpeg
def test_rotated_frames_are_fitted_completely_into_the_view(qapp, lab):
    encode_and_wait(qapp, lab)
    lab.resize(1400, 800)
    lab.set_angle(90)
    qapp.processEvents()
    for view in lab.views:
        shown = view.shown_rect()
        port = view.viewport().rect()
        assert port.contains(shown.adjusted(1, 1, -1, -1))                          # nothing is cut off
        assert shown.height() > shown.width() * 1.3                                  # a 16:9 frame turned: tall


@requires_ffmpeg
def test_the_original_shows_its_frame_rate(qapp, lab):
    texts = [label.text() for label in lab.original.findChildren(type(lab.status))]
    assert any("25 fps" in t and "640 × 360" in t for t in texts)


@requires_ffmpeg
def test_portrait_phone_clips_get_upright_frames_in_every_column(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog

    landscape = make_clip(tmp_path / "land.mp4", seconds=3, size="640x360")
    portrait = tmp_path / "portrait.mov"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(landscape), "-c", "copy", str(portrait)],
                   check=True)
    dialog = CompressionLabDialog(portrait, None, {**BASE, 'limit_res': False}, lambda s: None)
    dialog.show()
    assert dialog.rotation in (90, 270) and dialog.frame_size == (360, 640)
    texts = [label.text() for label in dialog.original.findChildren(type(dialog.status))]
    assert any("360 × 640" in t for t in texts)
    encode_and_wait(qapp, dialog)
    sizes = {(v.item.pixmap().width(), v.item.pixmap().height()) for v in dialog.views}
    assert sizes == {(360, 640)}                                                    # all upright and not squeezed
    dialog.close()


@requires_ffmpeg
def test_the_time_of_the_whole_folder_is_estimated_and_remembered(qapp, tmp_path):
    from archive_helper.compressor.lab import CompressionLabDialog
    from archive_helper.compressor.utils import EncodeSpeeds

    clip = make_clip(tmp_path / "a.mp4", seconds=4)
    dialog = CompressionLabDialog(clip, None, dict(BASE), lambda s: None, queue=(40, 7200.0))
    dialog.show()
    encode_and_wait(qapp, dialog)
    for column in dialog.columns:
        assert "Alle 40 Videos: etwa" in column.result.text()
    speeds = EncodeSpeeds()
    for column in dialog.columns:
        settings = column.settings()
        seconds, exact = speeds.estimate(settings, 1, 3600)
        assert exact and seconds > 0
    dialog.close()


@requires_ffmpeg
def test_nothing_starts_after_the_lab_is_closed(qapp, lab):
    # Regression: the frame timer or the end of an encoding started a new ffmpeg thread for a closed dialog;
    # when that dialog was deleted while it ran, Qt aborted the whole app.
    lab.frame_timer.start()
    lab.close()
    before = lab.frame_worker
    lab.load_frames()                  # what the timer or a finished encoding would do now
    lab.encode()
    spin(400)                          # longer than the frame timer
    assert lab.frame_worker is before and (before is None or not before.isRunning())
    assert lab.lab_worker is None or not lab.lab_worker.isRunning()
