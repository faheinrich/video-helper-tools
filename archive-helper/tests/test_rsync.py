import json
import shutil
import subprocess
import sys
import time

import pytest
from PySide6.QtWidgets import QMessageBox

from conftest import spin, wait_for


# --- rsync GUI -------------------------------------------------------------------------------------------------------

@pytest.fixture
def rsync(qapp):
    from archive_helper.rsync_gui.gui import RsyncSyncGUI

    gui = RsyncSyncGUI()
    yield gui
    gui.shutdown()


def confirm(monkeypatch, answer=QMessageBox.Yes):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: answer))


def test_rsync_needs_both_folders_and_an_existing_source(rsync, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    rsync.start_sync()
    rsync.src_edit.setText(str(tmp_path / "missing"))
    rsync.tgt_edit.setText(str(tmp_path / "target"))
    rsync.start_sync()
    assert len(warnings) == 2 and not rsync.is_busy()


def test_declining_the_confirmation_starts_nothing(rsync, tmp_path, monkeypatch):
    confirm(monkeypatch, QMessageBox.No)
    rsync.src_edit.setText(str(tmp_path))
    rsync.tgt_edit.setText(str(tmp_path / "t"))
    rsync.start_sync()
    assert not rsync.is_busy() and rsync.log_output.toPlainText() == ""


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_rsync_mirrors_a_folder_and_reports_progress(rsync, tmp_path, monkeypatch):
    confirm(monkeypatch)
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    tgt.mkdir()
    (tgt / "stale.txt").write_text("deleted by --del")
    rsync.src_edit.setText(str(src))
    rsync.tgt_edit.setText(str(tgt))

    rsync.start_sync()
    assert rsync.is_busy() and rsync.sync_btn.property("mode") == "stop"
    wait_for(rsync.process.finished)
    spin(50)

    assert (tgt / "a.txt").read_text() == "a" and (tgt / "sub" / "b.txt").exists() and not (tgt / "stale.txt").exists()
    assert rsync.values["files"].text() == "2"
    assert rsync.progress.value() == 100 and rsync.sync_btn.property("mode") == "start"
    assert "rsync -av --del" in rsync.log_output.toPlainText()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_rsync_error_is_shown(rsync, tmp_path, monkeypatch):
    confirm(monkeypatch)
    rsync.src_edit.setText(str(tmp_path))
    rsync.tgt_edit.setText("/proc/definitely/not/writable")
    rsync.start_sync()
    wait_for(rsync.process.finished)
    spin(50)
    assert rsync.values["status"].property("tone") == "warn"


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_stop_kills_a_running_sync(rsync, tmp_path, monkeypatch):
    confirm(monkeypatch)
    monkeypatch.setattr("archive_helper.rsync_gui.gui.RSYNC_ARGS", ["-a", "--bwlimit=1"])
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"0" * 4_000_000)
    rsync.src_edit.setText(str(src))
    rsync.tgt_edit.setText(str(tmp_path / "tgt"))
    rsync.toggle_sync()
    spin(300)
    rsync.toggle_sync()  # second click stops
    wait_for(rsync.process.finished, 10_000)
    assert not rsync.is_busy()


def test_line_parsing_counts_files_but_not_status_lines(rsync):
    for line in ["sending incremental file list", "photos/", "a.jpg", "  45%  1.2MB/s", "b.jpg", "sent 10 bytes",
                 "total size is 5", "deleting old.jpg", "Transfer starting: 4 files", ""]:
        rsync.handle_line(line)
    assert rsync.files_done == 2 and rsync.progress.value() == 45


def test_log_escapes_html_in_file_names(rsync):
    rsync.append_log("<b>evil</b>.mp4")
    assert "<b>evil</b>.mp4" in rsync.log_output.toPlainText()


def test_browse_buttons_fill_the_fields(rsync, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path)))
    rsync.browse_source()
    rsync.browse_target()
    assert rsync.src_edit.text() == rsync.tgt_edit.text() == str(tmp_path)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    rsync.src_edit.clear()
    rsync.browse_source()
    assert rsync.src_edit.text() == ""


def test_elapsed_time_format(rsync):
    import time

    rsync.started_at = time.monotonic() - 3725
    rsync.update_elapsed()
    assert rsync.values["elapsed"].text() == "1:02:05"
    rsync.started_at = time.monotonic() - 65
    rsync.update_elapsed()
    assert rsync.values["elapsed"].text() == "1:05"


class FakeKeepAwake:
    def __init__(self):
        self.starts = self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_backup_keeps_the_computer_awake_and_reports_the_end(rsync, tmp_path, monkeypatch):
    confirm(monkeypatch)
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    src.mkdir()
    tgt.mkdir()
    (src / "a.txt").write_text("a")
    rsync.src_edit.setText(str(src))
    rsync.tgt_edit.setText(str(tgt))
    rsync.keep_awake = FakeKeepAwake()
    rsync.cb_keep_awake.setChecked(True)
    rsync.keep_awake.stops = 0   # ticking the box while idle only released nothing
    notified = []
    rsync.notifier = lambda *args: notified.append(args[1:])

    rsync.start_sync()
    assert rsync.keep_awake.starts == 1
    wait_for(rsync.process.finished)
    spin(50)
    assert rsync.keep_awake.stops == 1 and notified == [("Backup (rsync)", "Fertig")]


@pytest.mark.skipif(sys.platform == "win32", reason="uses the POSIX sleep command")
def test_stopping_a_backup_by_hand_sends_no_notification(rsync, tmp_path, monkeypatch):
    from PySide6.QtCore import QProcess

    confirm(monkeypatch)
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    src.mkdir()
    tgt.mkdir()
    rsync.src_edit.setText(str(src))
    rsync.tgt_edit.setText(str(tgt))
    rsync.keep_awake = FakeKeepAwake()
    rsync.cb_keep_awake.setChecked(True)
    rsync.keep_awake.stops = 0   # ticking the box while idle only released nothing
    notified = []
    rsync.notifier = lambda *args: notified.append(args)
    monkeypatch.setattr(rsync.process, "start", lambda program, args: QProcess.start(rsync.process, "sleep", ["30"]))
    rsync.start_sync()
    while rsync.process.state() != QProcess.Running:
        spin(10)
    begin = time.monotonic()
    rsync.stop_sync()   # right away, while the process may still be starting
    wait_for(rsync.process.finished, timeout_ms=10_000)
    assert time.monotonic() - begin < 5   # stopped, not waited out
    spin(50)
    assert notified == [] and rsync.keep_awake.stops == 1


@pytest.mark.skipif(sys.platform == "win32", reason="uses the POSIX sleep command")
def test_backup_keep_awake_checkbox_decides(rsync, tmp_path, monkeypatch):
    from PySide6.QtCore import QProcess

    confirm(monkeypatch)
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    src.mkdir()
    tgt.mkdir()
    rsync.src_edit.setText(str(src))
    rsync.tgt_edit.setText(str(tgt))
    rsync.keep_awake = FakeKeepAwake()
    monkeypatch.setattr(rsync.process, "start", lambda program, args: QProcess.start(rsync.process, "sleep", ["30"]))
    assert not rsync.cb_keep_awake.isChecked()       # off unless the user asks for it
    rsync.start_sync()
    assert rsync.keep_awake.starts == 0
    rsync.cb_keep_awake.setChecked(True)
    assert rsync.keep_awake.starts == 1
    begin = time.monotonic()
    rsync.stop_sync()   # right away, while the process may still be starting
    wait_for(rsync.process.finished, timeout_ms=10_000)
    assert time.monotonic() - begin < 5   # stopped, not waited out
