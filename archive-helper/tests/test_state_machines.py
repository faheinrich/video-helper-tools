"""The transitions of the compressor's state machines (see docs/state-machines.md) that no other test covers.

Each test names its transition as in that document, e.g. R3 (row: running -> done)."""
import shutil
import subprocess

import pytest

from conftest import requires_ffmpeg
from test_compressor import fake_worker, make_gui, started_gui, wait_idle   # noqa: F401 (fixtures)
from test_workers import archive_worker, collect, item, make_clip


# --- R: the status of a row ---------------------------------------------------------------------------------------

def row_after(gui, start, event, data):
    path = str(gui.model.rows[0].src)
    gui.model.update(path, status=start)
    gui.on_status_update(path, event, "reason" if event in ("error", "skipped") else "", data)
    return gui.model.rows[0]


@pytest.mark.parametrize("name, start, event, data, status, out_size", [
    ("R2 planned -> running", "planned", "running", {}, "running", None),
    ("R3 running -> done", "running", "finished", {"src_size": 100, "dst_size": 40, "settings": {"crf": 20}}, "done", 40),
    ("R4 running -> error (ffmpeg failed)", "running", "error", {}, "error", None),
    ("R5 planned -> skipped (result already there)", "planned", "skipped", {"src_size": 100, "dst_size": 40}, "skipped", 40),
    ("R6 planned -> skipped (result of another length)", "planned", "skipped", {}, "skipped", None),
    ("R7 planned -> error (could not start)", "planned", "error", {"failed": True}, "error", None),
])
def test_row_status_follows_the_worker(qapp, make_gui, tmp_path, fake_worker, name, start, event, data, status, out_size):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    row = row_after(gui, start, event, data)
    assert row.status == status, name
    assert row.out_size == out_size
    if status == "done":
        assert row.settings == {"crf": 20}
    if status in ("error", "skipped"):
        assert row.note == "reason"


def test_r8_a_stopped_run_puts_its_running_videos_back_to_planned(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.start_archiving()
    path = str(gui.model.rows[0].src)
    gui.on_status_update(path, "running", "", {})
    gui.on_file_progress(path, 40)
    gui.stop_archiving()
    wait_idle(qapp, gui)
    row = gui.model.rows[0]
    assert (row.status, row.progress, row.speed) == ("planned", 0, "")


def test_r9_errors_are_queued_again_but_finished_videos_only_with_overwrite(qapp, make_gui, tmp_path, fake_worker,
                                                                            monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.model.update(str(gui.model.rows[0].src), status="error", note="FFmpeg error")
    gui.model.update(str(gui.model.rows[1].src), status="done", out_size=40)
    assert [item["path"].name for item in gui.queue_items()] == ["a.mp4"]
    gui.start_archiving()
    assert [r.status for r in gui.model.rows] == ["planned", "done"]   # R9: error -> planned, its note gone
    assert gui.model.rows[0].note == ""
    gui.stop_archiving()
    wait_idle(qapp, gui)

    # R10: with "overwrite", a finished video is planned again (after the question)
    gui.cb_overwrite.setChecked(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    gui.start_archiving()
    assert [r.status for r in gui.model.rows] == ["planned", "planned"]
    gui.stop_archiving()
    wait_idle(qapp, gui)


def test_r10_saying_no_to_overwrite_starts_nothing(qapp, make_gui, tmp_path, fake_worker, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.model.update(str(gui.model.rows[1].src), status="done", out_size=40)
    gui.cb_overwrite.setChecked(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    gui.start_archiving()
    assert not gui.is_archiving()
    assert gui.model.rows[1].status == "done"


# --- A: the app (scanning, archiving, paused) -----------------------------------------------------------------------

def test_a_scan_asked_for_while_scanning_runs_once_the_first_ends(qapp, make_gui, tmp_path):
    from video_helper_core.testing import wait_for

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a.mp4").write_bytes(b"x")
    gui = make_gui()
    gui.txt_src.setText(str(src))
    gui.txt_dst.setText(str(dst))
    gui.start_unified_scan()
    first = gui.scan_worker
    gui.start_unified_scan()   # A2: while scanning, only remembered
    assert gui.rescan_pending and gui.scan_worker is first
    wait_for(first.scan_finished)
    qapp.processEvents()
    assert not gui.rescan_pending and gui.scan_worker is not first   # A3: the second scan followed
    wait_for(gui.scan_worker.finished, 30_000)


def test_a_scan_is_not_started_while_archiving(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.start_archiving()
    gui.start_unified_scan()
    assert gui.scan_worker is None
    gui.stop_archiving()
    wait_idle(qapp, gui)


def test_pause_and_stop_do_nothing_when_no_run_is_going(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.toggle_pause()
    gui.stop_archiving()
    assert not gui.is_archiving() and gui.worker is None


def test_stopping_a_paused_run_ends_it(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.start_archiving()
    gui.toggle_pause()
    assert gui.is_paused()
    gui.stop_archiving()
    wait_idle(qapp, gui)
    assert not gui.is_archiving() and gui.btn_pause.isHidden()


# --- E: one encode in the worker -------------------------------------------------------------------------------------

@requires_ffmpeg
def test_e_a_result_of_another_length_is_skipped_and_kept(qapp, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_clip(src / "a.mp4", seconds=3)
    make_clip(dst / "a_archived.mp4", seconds=1)   # e.g. a test run's second
    before = (dst / "a_archived.mp4").read_bytes()
    worker = archive_worker(src, dst, [item(src, dst, "a.mp4")])
    worker.run()
    skipped = [s for s in worker.events["status"] if s[1] == "skipped"]
    assert len(skipped) == 1 and skipped[0][3] == {}
    assert (dst / "a_archived.mp4").read_bytes() == before


@requires_ffmpeg
def test_e_with_overwrite_the_earlier_result_goes_to_the_trash(qapp, tmp_path, trash):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_clip(src / "a.mp4")
    make_clip(dst / "a_archived.mp4", seconds=1)
    earlier = (dst / "a_archived.mp4").read_bytes()
    worker = archive_worker(src, dst, [item(src, dst, "a.mp4")], overwrite=True)
    worker.run()
    assert [s[1] for s in worker.events["status"] if s[1] != "running"] == ["finished"]
    assert (trash / "a_archived.mp4").read_bytes() == earlier
    assert (dst / "a_archived.mp4").read_bytes() != earlier


@requires_ffmpeg
def test_e_replacing_the_original_keeps_it_when_the_result_is_not_smaller(qapp, tmp_path, trash):
    from archive_helper.compressor.utils import partial_path

    class Done:
        returncode = 0

    original = make_clip(tmp_path / "a.mp4")
    content = original.read_bytes()
    partial = partial_path(original)
    shutil.copy(original, partial)
    with open(partial, "ab") as f:
        f.write(b"\0" * 1000)   # bigger than the original
    worker = archive_worker(tmp_path, tmp_path, [])
    message, data = worker.finalize_video_process(Done(), original, original, replace_source=True)
    assert "KEPT ORIGINAL" in message or "ORIGINAL BEHALTEN" in message
    assert data["ratio"] == 100.0 and data["settings"] is None
    assert original.read_bytes() == content and not partial.exists() and not trash.exists()


@requires_ffmpeg
def test_e_a_failed_encode_leaves_no_partial_file_and_an_earlier_result_alone(qapp, tmp_path):
    from archive_helper.compressor.utils import partial_path

    class Failed:
        returncode = 1

    src, dst = tmp_path / "a.mp4", tmp_path / "out" / "a_archived.mp4"
    make_clip(src)
    dst.parent.mkdir()
    dst.write_bytes(b"earlier")
    partial_path(dst).write_bytes(b"half")
    worker = archive_worker(tmp_path, dst.parent, [])
    message, data = worker.finalize_video_process(Failed(), src, dst)
    assert message.startswith("❌") and data == {}
    assert not partial_path(dst).exists() and dst.read_bytes() == b"earlier"


def test_e_a_queue_change_after_stop_starts_nothing(qapp, tmp_path):
    worker = archive_worker(tmp_path, tmp_path, [{"path": tmp_path / "a.mp4", "dst_path": tmp_path / "b.mp4"}])
    worker.stop()
    worker.run()
    assert worker.events["status"] == [] and len(worker.events["done"]) == 1
