import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from conftest import EXAMPLE_VIDEOS, requires_exiftool, requires_ffmpeg, spin, wait_for


@pytest.fixture
def make_gui(qapp):
    """Creates ArchiverGUIs and stops their background threads afterwards."""
    from archive_helper.compressor.gui import ArchiverGUI

    created = []

    def factory():
        gui = ArchiverGUI()
        created.append(gui)
        return gui

    yield factory
    for gui in created:
        gui.shutdown()


def wait_idle(qapp, gui, timeout=120):
    """Waits until the archive thread has really ended (not just emitted finished_all)."""
    deadline = time.monotonic() + timeout
    while gui.is_archiving() and time.monotonic() < deadline:
        spin(20)
    qapp.processEvents()


def scan(qapp, gui, src, dst):
    gui.txt_src.setText(str(src))
    gui.txt_dst.setText(str(dst))
    gui.start_unified_scan()
    wait_for(gui.scan_worker.scan_finished)
    qapp.processEvents()


def run_archive(qapp, gui, src, dst, renderer_index=0, crf=None):
    gui.combo_renderer.setCurrentIndex(renderer_index)
    gui.combo_preset.setCurrentText("ultrafast")
    if crf is not None:
        gui.slider_crf.setValue(crf)
    gui.spin_jobs.setValue(2)
    scan(qapp, gui, src, dst)
    gui.start_archiving()
    wait_idle(qapp, gui)
    qapp.processEvents()
    return gui


@requires_ffmpeg
@requires_exiftool
def test_archive_run_end_to_end(qapp, make_gui, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    # Same file name in two folders must not collide.
    shutil.copy(EXAMPLE_VIDEOS[0], src / "a.mp4")
    shutil.copy(EXAMPLE_VIDEOS[1], src / "sub" / "a.mp4")

    gui = run_archive(qapp, make_gui(), src, dst)

    outputs = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*.mp4"))
    assert outputs == ["a_archived.mp4", "sub/a_archived.mp4"]
    assert {row.status for row in gui.model.rows} == {"done"}
    assert sorted(row.folder for row in gui.model.rows) == ["", "sub"]

    # Savings totals must match the real file sizes (each source counted once).
    assert gui.total_src_bytes == sum(p.stat().st_size for p in src.rglob("*.mp4"))
    assert gui.total_dst_bytes == sum(p.stat().st_size for p in dst.rglob("*.mp4"))
    gui.refresh_summary()
    assert "%" in gui.summary_values["saving"][1].text()
    assert gui.progress_text.text().startswith("Fertig")

    # A second scan finds both outputs and plans nothing.
    gui.start_unified_scan()
    wait_for(gui.scan_worker.scan_finished)
    assert {row.status for row in gui.model.rows} == {"exists"}
    assert not gui.btn_run.isEnabled()


@requires_ffmpeg
def test_every_thumbnail_arrives(qapp, make_gui, tmp_path):
    # Regression: thumbnails were delivered through a lambda bound to the pool job's
    # short-lived signals object; most got lost and the app crashed on larger folders.
    src = tmp_path / "src"
    src.mkdir()
    for i in range(30):
        shutil.copy(EXAMPLE_VIDEOS[i % 2], src / f"clip_{i:02d}.mp4")
    gui = make_gui()
    scan(qapp, gui, src, tmp_path / "dst")

    deadline = time.monotonic() + 60
    while len(gui.model.thumbnails) < 30 and time.monotonic() < deadline:
        spin(100)
    assert len(gui.model.thumbnails) == 30


@requires_ffmpeg
@requires_exiftool
@pytest.mark.parametrize("rescan", [False, True], ids=["right-after-run", "after-rescan"])
def test_overwrite_option_reprocesses_existing_results(qapp, make_gui, tmp_path, monkeypatch, rescan):
    from PySide6.QtWidgets import QMessageBox

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    shutil.copy(EXAMPLE_VIDEOS[0], src / "a.mp4")
    gui = make_gui()
    gui.cb_dry_run.setChecked(True)
    run_archive(qapp, gui, src, dst)
    # mtime is copied from the original on purpose, so compare the inode change time.
    first = (dst / "a_archived.mp4").stat().st_ino   # a new file replaces it (st_ctime is the creation time on Windows)
    if rescan:
        gui.start_unified_scan()
        wait_for(gui.scan_worker.scan_finished)
    assert not gui.btn_run.isEnabled()
    if rescan:  # everything already archived: the summary points to the overwrite option
        assert "überschreiben" in gui.progress_text.text()

    gui.cb_overwrite.setChecked(True)
    qapp.processEvents()
    spin(300)
    assert gui.btn_run.isEnabled()
    assert "ersetzen" in gui.progress_text.text()

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    gui.start_archiving()
    wait_idle(qapp, gui)
    assert gui.model.rows[0].status == "done"
    assert (dst / "a_archived.mp4").stat().st_ino != first
    assert not gui.btn_run.isEnabled() or gui.cb_overwrite.isChecked()


def test_failed_videos_are_retried_on_the_next_start(qapp, make_gui, tmp_path):
    from pathlib import Path
    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    gui.scan_items = [{'path': Path("/a/one.mp4"), 'dst_path': Path("/b/one_archived.mp4"), 'size': 1}]
    gui.model.reset([VideoRow(src=Path("/a/one.mp4"), dst=Path("/b/one_archived.mp4"), size=1, status="error")])
    assert [item['path'].name for item in gui.queue_items()] == ["one.mp4"]


@requires_ffmpeg
def test_scan_starts_automatically_once_both_folders_are_set(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(EXAMPLE_VIDEOS[0], src / "clip.mp4")

    gui = make_gui()
    gui.txt_src.setText(str(src))
    spin(700)
    assert gui.scan_worker is None  # target still missing

    gui.txt_dst.setText(str(tmp_path / "dst"))
    spin(700)
    if gui.is_scanning():
        wait_for(gui.scan_worker.scan_finished, 10_000)
    qapp.processEvents()
    assert [row.src.name for row in gui.model.rows] == ["clip.mp4"]
    assert gui.btn_run.isEnabled()

    gui.duration_worker.wait(10_000)
    qapp.processEvents()
    assert gui.model.rows[0].duration == pytest.approx(26.9, abs=0.1)


@requires_ffmpeg
def test_home_relative_paths_are_expanded(qapp, make_gui, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # what "~" means on Windows
    (tmp_path / "src").mkdir()
    shutil.copy(EXAMPLE_VIDEOS[0], tmp_path / "src" / "clip.mp4")
    gui = make_gui()
    scan(qapp, gui, "~/src", "~/dst")
    assert [row.src.name for row in gui.model.rows] == ["clip.mp4"]
    assert gui.model.rows[0].dst == tmp_path / "dst" / "clip_archived.mp4"


def make_named_videos(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")  # the scan only needs names and sizes


def test_scan_skips_earlier_results_in_the_source_folder(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    make_named_videos(src, "clip.mp4", "clip_archived.mp4", "other_Compressed.mov", "note_final.mp4")
    gui = make_gui()
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    assert sorted(row.src.name for row in gui.model.rows) == ["clip.mp4", "note_final.mp4"]

    gui.txt_excludes.setText("_final; _archived")  # list is editable, separators are free
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    assert sorted(row.src.name for row in gui.model.rows) == ["clip.mp4", "other_Compressed.mov"]


def test_result_postfix_is_editable_and_always_skipped(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    make_named_videos(src, "clip.mp4", "clip_small.mp4")
    gui = make_gui()
    gui.txt_postfix.setText("_small")
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    assert [(row.src.name, row.dst.name) for row in gui.model.rows] == [("clip.mp4", "clip_small.mp4")]


def test_postfix_cannot_leave_the_target_folder_or_overwrite_the_original(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    make_named_videos(src, "clip.mp4", "movie.mov")
    gui = make_gui()
    gui.txt_postfix.setText("../x/")
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    assert {row.dst.parent for row in gui.model.rows} == {tmp_path / "dst"}
    assert {row.dst.name for row in gui.model.rows} == {"clip..x.mp4", "movie..x.mov".replace(".mov", ".mp4")}

    gui.txt_postfix.setText("")  # same folder, same name: clip.mp4 would replace itself, so it is flagged
    scan(qapp, gui, str(src), str(src))
    rows = {row.src.name: row for row in gui.model.rows}
    assert rows["clip.mp4"].conflict and rows["clip.mp4"].status == "planned"
    assert not rows["movie.mov"].conflict and rows["movie.mov"].dst.name == "movie.mp4"


def test_hidden_files_and_folders_are_ignored_by_default(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    make_named_videos(src, "clip.mp4", ".secret.mp4", "._clip.mp4")
    make_named_videos(src / ".hermes" / "cache", "frame.mp4")
    make_named_videos(src / "trip", "beach.mov")
    gui = make_gui()
    assert gui.cb_skip_hidden.isChecked()
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    assert sorted(row.src.name for row in gui.model.rows) == ["beach.mov", "clip.mp4"]

    gui.cb_skip_hidden.setChecked(False)  # macOS "._" metadata files are never videos
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    assert sorted(row.src.name for row in gui.model.rows) == [".secret.mp4", "beach.mov", "clip.mp4", "frame.mp4"]


def test_name_settings_are_saved_as_defaults(qapp, make_gui, tmp_path):
    gui = make_gui()
    gui.txt_postfix.setText("_small")
    gui.txt_excludes.setText("_a, _b")
    gui.cb_skip_hidden.setChecked(False)
    gui.save_defaults()
    restored = make_gui()
    assert (restored.txt_postfix.text(), restored.txt_excludes.text()) == ("_small", "_a, _b")
    assert not restored.cb_skip_hidden.isChecked()


def test_quality_presets_and_custom_values(qapp, make_gui):
    gui = make_gui()
    gui.apply_quality_preset("small")
    assert (gui.slider_crf.value(), gui.slider_vt.value()) == (26, 45)
    assert gui.quality_buttons["small"].isChecked()

    # "Custom" follows the selected encoder's value.
    gui.combo_renderer.setCurrentIndex(1)  # VideoToolbox
    gui.slider_vt.setValue(70)
    assert not any(b.isChecked() for b in gui.quality_buttons.values())
    assert "70" in gui.quality_hint.text()
    gui.combo_renderer.setCurrentIndex(0)  # CPU still matches "small"
    assert gui.quality_buttons["small"].isChecked()

    gui.apply_quality_preset("archive")
    assert gui.quality_buttons["archive"].isChecked()


def test_only_the_selected_encoders_quality_controls_are_shown(qapp, make_gui):
    gui = make_gui()
    gui.show()
    gui.btn_advanced.setChecked(True)
    gui.combo_renderer.setCurrentIndex(0)
    assert gui.cpu_quality.isVisible() and not gui.gpu_quality.isVisible()
    gui.combo_renderer.setCurrentIndex(1)
    assert gui.gpu_quality.isVisible() and not gui.cpu_quality.isVisible()


def test_settings_drawer_grows_when_its_content_needs_more_room(qapp, make_gui):
    # Regression (Windows): with its fonts the options needed about 500 px, the drawer was fixed at 340 and,
    # scrolling only up and down, cut them off at the right.
    gui = make_gui()
    gui.resize(1400, 800)
    gui.show()
    gui.btn_save_defaults.setMinimumWidth(600)   # stands for wider text
    gui.fit_drawer_width()
    qapp.processEvents()
    content = gui.drawer_scroll.widget()
    assert gui.drawer_frame.width() > gui.DRAWER_WIDTH
    assert content.minimumSizeHint().width() <= gui.drawer_scroll.viewport().width()


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("renderer_index", [0, 1])
def test_settings_drawer_fits_without_clipping(qapp, make_gui, language, renderer_index):
    # Regression: long checkbox labels made the drawer content wider than the drawer.
    from video_helper_core import i18n

    i18n.set_language(language)
    gui = make_gui()
    gui.resize(1200, 800)
    gui.show()
    gui.btn_advanced.setChecked(True)
    gui.combo_renderer.setCurrentIndex(renderer_index)
    qapp.processEvents()
    content = gui.drawer_scroll.widget()
    assert content.minimumSizeHint().width() <= gui.drawer_scroll.viewport().width()


def test_parallel_videos_accept_custom_values(qapp, make_gui):
    gui = make_gui()
    assert [gui.spin_jobs.itemText(i) for i in range(gui.spin_jobs.count())] == ["1", "2", "3", "4", "6", "8"]
    gui.spin_jobs.setValue(5)
    assert gui.spin_jobs.value() == 5
    gui.spin_jobs.setCurrentText("12")
    assert gui.spin_jobs.value() == 12


def test_status_filter_and_search(qapp, make_gui):
    from pathlib import Path
    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    gui.model.reset([
        VideoRow(src=Path("/a/one.mp4"), dst=Path("/b/one_archived.mp4"), size=10, status="planned"),
        VideoRow(src=Path("/a/two.mp4"), dst=Path("/b/two_archived.mp4"), size=20, status="exists", out_size=8),
        VideoRow(src=Path("/a/three.mp4"), dst=Path("/b/three_archived.mp4"), size=30, status="error"),
    ])
    gui.refresh_summary()
    assert gui.filter_buttons["error"].isVisibleTo(gui)
    assert not gui.filter_buttons["skipped"].isVisibleTo(gui)

    gui.set_filter("done")
    assert gui.proxy.rowCount() == 1
    gui.set_filter("all")
    gui.proxy.set_text_filter("thr")
    assert gui.proxy.rowCount() == 1


def test_folder_outside_the_source_root_does_not_break_the_row():
    from pathlib import Path
    from archive_helper.compressor.model import VideoRow

    row = VideoRow(src=Path("/elsewhere/clip.mp4"), dst=Path("/b/clip_archived.mp4"), size=1, root=Path("/src"))
    assert row.folder == ""


RENDERERS = [
    pytest.param(0, id="libx265"),
    pytest.param(1, id="videotoolbox", marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")),
]


@requires_ffmpeg
@requires_exiftool
@pytest.mark.parametrize("renderer_index", RENDERERS)
def test_output_imports_into_apple_photos_with_metadata(qapp, make_gui, tmp_path, renderer_index):
    from archive_helper.compressor.utils import is_photos_compatible

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    clip = src / "IMG_0001.mov"
    shutil.copy(EXAMPLE_VIDEOS[0], clip)
    # iPhone clips carry capture date and location in QuickTime Keys, which Photos reads.
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-Keys:CreationDate=2025:06:15 19:20:57+09:00",
         "-Keys:GPSCoordinates=35.1623 N, 129.1577 E, 8.3", str(clip)],
        check=True,
    )

    run_archive(qapp, make_gui(), src, dst, renderer_index=renderer_index)

    out = dst / "IMG_0001_archived.mp4"
    assert is_photos_compatible(out)
    tags = json.loads(subprocess.run(
        ["exiftool", "-j", "-n", "-Keys:CreationDate", "-Keys:GPSCoordinates", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout)[0]
    assert tags["CreationDate"].startswith("2025:06:15 19:20:57")
    assert tags["GPSCoordinates"].startswith("35.1623 129.1577")


def make_low_bitrate_clip(path, codec_args):
    """Tiny, heavily compressed clip so a lossless (CRF 0) re-encode is guaranteed to be larger."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-t", "3",
         *codec_args, "-c:a", "aac", "-b:a", "32k", str(path)],
        check=True,
    )


@requires_ffmpeg
@requires_exiftool
def test_larger_output_keeps_photos_compatible_original(qapp, make_gui, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    make_low_bitrate_clip(src / "clip.mp4", ["-c:v", "libx264", "-b:v", "50k"])

    run_archive(qapp, make_gui(), src, dst, crf=0)

    assert (dst / "clip_archived.mp4").exists()
    assert (dst / "clip_source.mp4").exists()


@requires_ffmpeg
@requires_exiftool
def test_larger_output_next_to_the_original_makes_no_second_copy(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_low_bitrate_clip(src / "clip.mp4", ["-c:v", "libx264", "-b:v", "50k"])

    gui = run_archive(qapp, make_gui(), src, src, crf=0)

    assert sorted(p.name for p in src.iterdir()) == ["clip.mp4", "clip_archived.mp4"]
    assert "Original daneben ist kleiner" in "\n".join(gui.model.rows[0].log)


def test_source_copies_are_skipped_by_default(qapp, make_gui, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_named_videos(src, "clip.mp4", "clip_source.mp4")
    gui = make_gui()
    scan(qapp, gui, src, dst)
    assert [row.src.name for row in gui.model.rows] == ["clip.mp4"]


@requires_ffmpeg
@requires_exiftool
def test_larger_output_does_not_copy_incompatible_original(qapp, make_gui, tmp_path):
    from archive_helper.compressor.utils import is_photos_compatible

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    # MPEG-4 Part 2 (mp4v) is not importable into Apple Photos.
    make_low_bitrate_clip(src / "clip.mp4", ["-c:v", "mpeg4", "-b:v", "50k"])
    assert not is_photos_compatible(src / "clip.mp4")

    gui = run_archive(qapp, make_gui(), src, dst, crf=0)

    outputs = sorted(p.name for p in dst.iterdir())
    assert outputs == ["clip_archived.mp4"]
    assert all(is_photos_compatible(p) for p in dst.iterdir())
    assert "Apple" in "\n".join(gui.model.rows[0].log)


def make_dummy_videos(folder, sizes):
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in sizes.items():
        (folder / name).write_bytes(b"x" * size)


def queue_names(gui):
    return [item['path'].name for item in gui.queue_items()]


def test_processing_order_follows_table_sort_and_moved_up_videos(qapp, make_gui, tmp_path):
    from PySide6.QtCore import Qt
    from archive_helper.compressor.model import COL_FILE, COL_SIZE

    make_dummy_videos(tmp_path / "src", {"a.mp4": 3, "b.mp4": 1, "c.mp4": 2})
    gui = make_gui()
    scan(qapp, gui, tmp_path / "src", tmp_path / "dst")

    gui.table.sortByColumn(COL_SIZE, Qt.AscendingOrder)
    assert queue_names(gui) == ["b.mp4", "c.mp4", "a.mp4"]
    gui.table.sortByColumn(COL_FILE, Qt.DescendingOrder)
    assert queue_names(gui) == ["c.mp4", "b.mp4", "a.mp4"]

    gui.move_up(gui.model.row_for(tmp_path / "src" / "a.mp4"))
    assert queue_names(gui) == ["a.mp4", "c.mp4", "b.mp4"]
    gui.move_up(gui.model.row_for(tmp_path / "src" / "b.mp4"))
    assert queue_names(gui) == ["b.mp4", "a.mp4", "c.mp4"]
    assert gui.model.row_for(tmp_path / "src" / "a.mp4").moved_up


def test_reordering_never_requeues_a_started_video(tmp_path):
    from archive_helper.compressor.workers import ArchiveWorker

    items = [{'path': tmp_path / n, 'dst_path': tmp_path / "out" / n} for n in ("a.mp4", "b.mp4", "c.mp4")]
    worker = ArchiveWorker(tmp_path, tmp_path / "out", 1, items, {})
    assert worker.next_item()['path'].name == "a.mp4"
    # The GUI still sees a.mp4 as planned until its "running" status arrives.
    worker.set_queue(list(reversed(items)))
    assert [worker.next_item()['path'].name for _ in range(2)] == ["c.mp4", "b.mp4"]
    assert worker.next_item() is None


@requires_ffmpeg
@requires_exiftool
def test_resorting_during_a_run_changes_the_remaining_order(qapp, make_gui, tmp_path):
    from PySide6.QtCore import Qt
    from archive_helper.compressor.model import COL_FILE

    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        shutil.copy(EXAMPLE_VIDEOS[0], src / name)
    gui = make_gui()
    gui.cb_dry_run.setChecked(True)
    gui.combo_renderer.setCurrentIndex(0)
    gui.combo_preset.setCurrentText("ultrafast")
    gui.spin_jobs.setValue(1)
    scan(qapp, gui, src, tmp_path / "dst")
    gui.table.sortByColumn(COL_FILE, Qt.AscendingOrder)

    started = []
    gui.start_archiving()
    deadline = time.monotonic() + 120
    while gui.is_archiving() and time.monotonic() < deadline:
        spin(20)
        for row in gui.model.rows:
            if row.status in ("running", "done") and row.src.name not in started:
                started.append(row.src.name)
                if len(started) == 1:
                    gui.table.sortByColumn(COL_FILE, Qt.DescendingOrder)
    assert started == ["a.mp4", "c.mp4", "b.mp4"]
    assert {row.status for row in gui.model.rows} == {"done"}


def test_quality_guidance_is_visible_and_follows_the_value(qapp, make_gui):
    gui = make_gui()
    gui.show()
    gui.btn_advanced.setChecked(True)

    gui.combo_renderer.setCurrentIndex(0)
    gui.slider_crf.setValue(21)
    assert gui.crf_hint.isVisible() and "Sweet Spot" in gui.crf_hint.text()
    gui.slider_crf.setValue(18)
    assert "Visuell verlustfrei" in gui.crf_hint.text()

    gui.combo_renderer.setCurrentIndex(1)
    gui.slider_vt.setValue(70)
    assert gui.vt_hint.isVisible() and "fast visuell verlustfrei" in gui.vt_hint.text()
    gui.slider_vt.setValue(40)
    assert "sichtbare Verluste" in gui.vt_hint.text()


@pytest.fixture
def archived_row(qapp, make_gui, tmp_path, monkeypatch):
    """A GUI with one selected row whose original and result exist; confirmations answered Yes."""
    from pathlib import Path
    from PySide6.QtWidgets import QMessageBox
    from archive_helper.compressor.model import VideoRow

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    src, dst = tmp_path / "src" / "clip.mov", tmp_path / "dst" / "clip_archived.mp4"
    src.parent.mkdir()
    dst.parent.mkdir()
    src.write_bytes(b"original")
    dst.write_bytes(b"result")
    gui = make_gui()
    gui.scan_items = [{'path': src, 'dst_path': dst, 'size': 8}]
    gui.model.reset([VideoRow(src=src, dst=dst, size=8, status="exists", out_size=6)])
    gui.table.selectRow(0)
    return gui, gui.model.rows[0], src, dst


def menu_texts(menu):
    return {a.text(): a.isEnabled() for a in menu.actions() if a.text()}


def test_file_actions_are_offered_for_archived_videos(archived_row):
    gui, row, _, _ = archived_row
    assert gui.btn_file_actions.isEnabled()
    assert menu_texts(gui.btn_file_actions.menu()) == {
        "Original durch Ergebnis ersetzen…": True, "Original und Ergebnis tauschen…": True,
        "Original in den Papierkorb…": True, "Ergebnis in den Papierkorb…": True,
    }
    context = menu_texts(gui.build_row_menu(row))
    assert context["Vergleichen"] and context["Original durch Ergebnis ersetzen…"]


def test_file_actions_are_locked_only_for_the_video_being_compressed(archived_row):
    gui, row, _, _ = archived_row
    gui.model.update(row.src, status="running")
    gui.update_detail_bar()
    assert not gui.btn_file_actions.isEnabled()
    assert not any(enabled for text, enabled in menu_texts(gui.build_row_menu(row)).items() if text.endswith("…"))


def test_swap_original_and_result(archived_row):
    gui, row, src, dst = archived_row
    gui.handle_compare_action("swap", row)
    assert (src.read_bytes(), dst.read_bytes()) == (b"result", b"original")


def test_replace_original_with_result(archived_row, trash):
    gui, row, src, dst = archived_row
    gui.handle_compare_action("overwrite", row)
    assert not src.exists() and (trash / src.name).read_bytes() == b"original"
    assert (src.parent / dst.name).read_bytes() == b"result"


def test_replace_original_with_a_result_in_the_same_folder(archived_row):
    """The result already sits next to the original: replacing only removes the original (was "the same file")."""
    gui, row, src, _ = archived_row
    result = src.parent / "clip_archived.mp4"
    result.write_bytes(b"result")
    gui.model.update(row.src, dst=result)
    gui.handle_compare_action("overwrite", gui.model.rows[0])
    assert sorted(p.name for p in src.parent.iterdir()) == ["clip_archived.mp4"]
    assert result.read_bytes() == b"result"


def test_replace_original_with_a_result_of_the_same_name_keeps_the_original_in_the_trash(archived_row, trash):
    # Regression: with the same name (empty name ending, other folder) the copy overwrote the original in place,
    # so it never reached the trash.
    gui, row, src, dst = archived_row
    same_name = dst.with_name(src.name)
    dst.rename(same_name)
    gui.model.update(row.src, dst=same_name)
    gui.handle_compare_action("overwrite", gui.model.rows[0])
    assert src.read_bytes() == b"result" and (trash / src.name).read_bytes() == b"original"
    assert sorted(p.name for p in src.parent.iterdir()) == [src.name]   # no temporary file left


def test_replace_original_moves_another_file_with_the_results_name_to_the_trash(archived_row, trash):
    gui, row, src, dst = archived_row
    older = src.parent / dst.name
    older.write_bytes(b"older copy")
    gui.handle_compare_action("overwrite", row)
    assert older.read_bytes() == b"result"
    assert sorted(p.read_bytes() for p in trash.iterdir()) == [b"older copy", b"original"]


def test_replace_original_changes_nothing_when_the_trash_refuses(archived_row, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from video_helper_core import trash as trash_module

    gui, row, src, dst = archived_row
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(trash_module, "_system_trash", lambda path: False)
    gui.handle_compare_action("overwrite", row)
    assert sorted(p.name for p in src.parent.iterdir()) == [src.name] and src.read_bytes() == b"original"


@pytest.fixture
def three_rows(qapp, make_gui, tmp_path, monkeypatch):
    """Two videos with a result next to them and one still planned; confirmations counted and answered Yes."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    from archive_helper.compressor.model import VideoRow

    questions = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: questions.append(a[2]) or QMessageBox.Yes))
    folder = tmp_path / "src"
    folder.mkdir()
    rows = []
    for name, done in (("a", True), ("b", True), ("c", False)):
        src, dst = folder / f"{name}.mov", folder / f"{name}_archived.mp4"
        src.write_bytes(b"original")
        if done:
            dst.write_bytes(b"result")
        rows.append(VideoRow(src=src, dst=dst, size=8, status="exists" if done else "planned",
                             out_size=6 if done else None))
    gui = make_gui()
    gui.scan_items = [{'path': r.src, 'dst_path': r.dst, 'size': 8} for r in rows]
    gui.model.reset(rows)
    gui.table.sortByColumn(1, Qt.AscendingOrder)   # by file
    return gui, folder, questions


def test_several_videos_can_be_selected_and_get_the_file_actions_at_once(three_rows, trash):
    from PySide6.QtWidgets import QLabel

    gui, folder, questions = three_rows
    gui.table.selectAll()
    assert len(gui.selected_rows()) == 3
    assert "3 Videos ausgewählt" in gui.detail_bar.findChild(QLabel).text()
    assert menu_texts(gui.btn_file_actions.menu()) == {
        "Original durch Ergebnis ersetzen… (2)": True, "Original und Ergebnis tauschen… (2)": True,
        "Original in den Papierkorb… (2)": True, "Ergebnis in den Papierkorb… (2)": True}
    assert menu_texts(gui.build_rows_menu(gui.selected_rows()))["Als Nächstes"]   # c.mp4 is still planned

    gui.handle_compare_action("overwrite", gui.selected_rows())
    # one question for all
    assert questions == ["2 Originale durch ihre komprimierten Versionen ersetzen? Die Originale kommen in den Papierkorb."]
    assert sorted(p.name for p in folder.iterdir()) == ["a_archived.mp4", "b_archived.mp4", "c.mov"]
    assert sorted(p.name for p in trash.iterdir()) == ["a.mov", "b.mov"]


def test_a_failure_on_one_video_does_not_stop_the_others(three_rows, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    gui, folder, _ = three_rows
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a[2])))
    (folder / "a_archived.mp4").unlink()   # gone behind the app's back
    gui.model.rows[0].out_size = 6
    rows = gui.model.rows[:2]
    monkeypatch.setattr(type(gui), "file_action_applies", staticmethod(lambda action, row: True))
    gui.handle_compare_action("del_comp", rows)
    assert not (folder / "b_archived.mp4").exists()
    assert len(errors) == 1 and "a.mov" in errors[0]


def test_original_and_result_go_to_the_trash(archived_row, trash):
    gui, row, src, dst = archived_row
    gui.handle_compare_action("del_comp", row)
    assert not dst.exists() and src.exists() and (trash / dst.name).read_bytes() == b"result"
    assert gui.model.rows[0].status == "planned"  # can be compressed again
    dst.write_bytes(b"result")
    gui.model.update(row.src, out_size=6, status="exists")
    gui.handle_compare_action("del_orig", row)
    assert not src.exists() and dst.exists() and (trash / src.name).read_bytes() == b"original"


def test_nothing_is_deleted_when_the_trash_refuses(archived_row, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from video_helper_core import trash as trash_module

    gui, row, src, dst = archived_row
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a[2])))
    monkeypatch.setattr(trash_module, "_system_trash", lambda path: False)   # e.g. a drive without a trash
    gui.handle_compare_action("del_orig", row)
    assert src.read_bytes() == b"original" and dst.exists()
    assert errors and "Papierkorb" in errors[0]


@requires_ffmpeg
@requires_exiftool
def test_settings_are_stored_in_the_result_and_shown_after_a_restart(qapp, make_gui, tmp_path):
    from archive_helper.compressor.model import COL_SETTINGS
    from archive_helper.compressor.utils import is_photos_compatible, read_compression_settings

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    shutil.copy(EXAMPLE_VIDEOS[0], src / "clip.mp4")
    gui = make_gui()
    gui.cb_dry_run.setChecked(True)
    gui.combo_res.setCurrentIndex(gui.combo_res.findData(1280))
    run_archive(qapp, gui, src, dst, crf=23)

    result = dst / "clip_archived.mp4"
    stored = read_compression_settings([result])[str(result)]
    encoding = {key: stored[key] for key in ("encoder", "crf", "preset", "max_res", "max_fps", "copy_aac", "dry_run")}
    assert encoding == {"encoder": "libx265", "crf": 23, "preset": "ultrafast", "max_res": 1280,
                        "max_fps": 30, "copy_aac": True, "dry_run": True}
    assert stored["version"] == 2 and stored["source"]["name"] == "clip.mp4"
    assert is_photos_compatible(result)
    assert gui.model.rows[0].settings == stored  # shown right away

    restarted = make_gui()  # a fresh window, like after restarting the app
    scan(qapp, restarted, src, dst)
    restarted.settings_worker.wait(30_000)
    qapp.processEvents()
    assert restarted.model.rows[0].settings == stored
    shown = restarted.model.index(0, COL_SETTINGS).data()
    assert "CRF 23" in shown and "1280 px" in shown


@requires_ffmpeg
def test_results_made_before_settings_were_recorded_show_a_dash(qapp, make_gui, tmp_path):
    from archive_helper.compressor.model import COL_SETTINGS

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    shutil.copy(EXAMPLE_VIDEOS[0], src / "clip.mp4")
    shutil.copy(EXAMPLE_VIDEOS[0], dst / "clip_archived.mp4")  # old result without the tag
    gui = make_gui()
    scan(qapp, gui, src, dst)
    gui.settings_worker.wait(30_000)
    qapp.processEvents()
    assert gui.model.rows[0].settings is None
    assert gui.model.index(0, COL_SETTINGS).data() == "–"


def test_settings_summary_for_videotoolbox():
    from archive_helper.compressor.model import describe_settings

    text = describe_settings({"encoder": "videotoolbox", "vt_quality": 65, "max_res": None, "max_fps": 30, "copy_aac": True})
    assert text == "Mac-GPU · Qualität 65 · volle Auflösung · 30 fps"


def test_size_sorting_handles_files_larger_than_2_gb(qapp):
    # Regression: sizes above 2**31 bytes were compared as truncated 32-bit numbers.
    from pathlib import Path
    from PySide6.QtCore import Qt
    from archive_helper.compressor.model import COL_SIZE, VideoFilterProxy, VideoRow, VideoTableModel

    MB, GB = 1024 ** 2, 1024 ** 3
    sizes = [int(610.65 * MB), int(868.43 * MB), int(5.95 * GB), int(6.80 * GB), int(83.58 * MB), int(107.24 * MB)]
    model, proxy = VideoTableModel(), VideoFilterProxy()
    proxy.setSourceModel(model)
    model.reset([VideoRow(src=Path(f"/v/{i}.mp4"), dst=Path(f"/o/{i}.mp4"), size=s) for i, s in enumerate(sizes)])
    for order in (Qt.AscendingOrder, Qt.DescendingOrder):
        proxy.sort(COL_SIZE, order)
        shown = [proxy.index(r, COL_SIZE).data(Qt.UserRole + 1).size for r in range(proxy.rowCount())]
        assert shown == sorted(sizes, reverse=order == Qt.DescendingOrder)


def test_compare_is_locked_while_the_result_is_being_rewritten(archived_row):
    gui, row, _, _ = archived_row
    gui.model.update(row.src, status="running")
    assert not menu_texts(gui.build_row_menu(row))["Vergleichen"]


def test_original_can_be_played_without_a_result(make_gui, tmp_path):
    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    row = VideoRow(src=EXAMPLE_VIDEOS[0], dst=tmp_path / "missing_archived.mp4", size=1)
    texts = menu_texts(gui.build_row_menu(row))
    assert texts["Original abspielen"] and "Vergleichen" not in texts


def spin_until(condition, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        spin(50)


def test_start_button_follows_the_scan_and_says_why_it_is_disabled(make_gui, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    gui = make_gui()
    gui.txt_src.setText(str(src))
    gui.txt_dst.setText(str(dst))
    spin_until(lambda: gui.scan_worker is not None and not gui.is_scanning())
    spin(100)
    assert not gui.btn_run.isEnabled()
    assert gui.btn_run.toolTip()  # says why: no videos

    shutil.copy(EXAMPLE_VIDEOS[0], src / "a.mp4")
    gui.start_unified_scan()
    spin_until(lambda: not gui.is_scanning() and len(gui.scan_items) == 1)
    spin(100)
    # Regression: the result arrived while the scan thread was still ending, and the button stayed grey.
    assert gui.btn_run.isEnabled()
    assert gui.btn_run.toolTip() == ""


def make_hlg_clip(path):
    """Like iPhone HDR: HEVC Main 10, BT.2020, HLG."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-t", "2",
         "-vf", "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc,format=yuv420p10le",
         "-c:v", "libx265", "-preset", "ultrafast",
         "-x265-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:log-level=none",
         "-tag:v", "hvc1", "-c:a", "aac", str(path)],
        check=True,
    )


def video_format(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=profile,pix_fmt,color_transfer,color_primaries", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


@requires_ffmpeg
@requires_exiftool
@pytest.mark.parametrize("renderer_index", RENDERERS)
def test_hdr_sources_stay_hdr(qapp, make_gui, tmp_path, renderer_index):
    from archive_helper.compressor.utils import is_photos_compatible, read_compression_settings

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    make_hlg_clip(src / "hdr.mov")
    shutil.copy(EXAMPLE_VIDEOS[0], src / "sdr.mp4")  # BT.709 (video2.mp4 is itself HLG HDR)

    run_archive(qapp, make_gui(), src, dst, renderer_index=renderer_index)

    hdr_out, sdr_out = dst / "hdr_archived.mp4", dst / "sdr_archived.mp4"
    hdr_format = video_format(hdr_out)
    assert hdr_format["profile"] == "Main 10" and hdr_format["pix_fmt"] == "yuv420p10le"
    assert (hdr_format["color_transfer"], hdr_format["color_primaries"]) == ("arib-std-b67", "bt2020")
    assert is_photos_compatible(hdr_out)
    assert read_compression_settings([hdr_out])[str(hdr_out)].get("hdr") is True

    sdr_format = video_format(sdr_out)
    assert sdr_format["profile"] == "Main" and sdr_format["pix_fmt"] == "yuv420p"
    assert "hdr" not in read_compression_settings([sdr_out])[str(sdr_out)]


@pytest.mark.parametrize("width", [1100, 1280])
def test_file_names_stay_visible_with_the_settings_drawer_open(make_gui, qapp, width):
    # Regression: the fixed-width columns took ~850 px, leaving the file column a thumbnail-wide sliver.
    from archive_helper.compressor.model import COL_FILE

    gui = make_gui()
    gui.resize(width, 760)
    gui.show()
    qapp.processEvents()
    assert gui.drawer.isVisible()
    # the preview has its own column now; the name had about 145 px next to the old built-in thumbnail
    assert gui.table.horizontalHeader().sectionSize(COL_FILE) >= 140


def test_rows_are_numbered_in_the_order_they_are_shown(qapp, make_gui):
    from PySide6.QtCore import Qt

    from archive_helper.compressor.model import COL_FILE, COL_SIZE, VideoRow

    gui = make_gui()
    root = Path("/src")
    gui.model.reset([VideoRow(src=root / f"{n}.mp4", dst=root / f"{n}_a.mp4", size=size, root=root)
                        for n, size in (("a", 10), ("b", 30), ("c", 20))])
    gui.table.sortByColumn(COL_SIZE, Qt.DescendingOrder)
    assert not gui.table.verticalHeader().isHidden()
    names = [gui.proxy.index(r, COL_FILE).data() for r in range(3)]
    numbers = [gui.proxy.headerData(r, Qt.Vertical, Qt.DisplayRole) for r in range(3)]
    assert names == ["b.mp4", "c.mp4", "a.mp4"] and numbers == [1, 2, 3]


def test_csv_export_lists_every_row(qapp, make_gui, tmp_path, monkeypatch):
    import csv

    from PySide6.QtWidgets import QFileDialog

    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    root = Path("/src")
    gui.model.reset([VideoRow(src=root / "a.mp4", dst=root / "a_x.mp4", size=1000, out_size=250, duration=61, status="done", root=root),
                     VideoRow(src=root / "b.mp4", dst=root / "b_x.mp4", size=500, root=root)])
    target = tmp_path / "log.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    gui.export_logs()
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    assert [r["file"] for r in rows] == ["a.mp4", "b.mp4"]
    assert rows[0]["saved_bytes"] == "750" and rows[0]["ratio"] == "0.250"
    assert rows[1]["result_bytes"] == "" and rows[1]["ratio"] == ""


def test_csv_export_reports_unwritable_target(qapp, make_gui, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    gui = make_gui()
    shown = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "no" / "dir.csv"), "")))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(a[2])))
    gui.export_logs()
    assert shown and "dir.csv" in shown[0]   # the path itself is quoted the system's way (doubled backslashes on Windows)


def test_columns_can_be_dragged_and_stay_put_when_the_window_resizes(qapp, make_gui):
    from archive_helper.compressor.model import COL_FILE, COL_RESULT, COL_STATUS

    gui = make_gui()
    gui.resize(1300, 600)
    gui.show()
    qapp.processEvents()
    header = gui.table.horizontalHeader()
    stretched = header.sectionSize(COL_FILE)
    assert stretched >= gui.MIN_FILE_COLUMN_WIDTH
    header.resizeSection(COL_RESULT, 260)
    gui.fit_columns()
    assert header.sectionSize(COL_RESULT) == 260
    header.resizeSection(COL_FILE, 333)
    gui.resize(1500, 600)
    qapp.processEvents()
    assert header.sectionSize(COL_FILE) == 333 and header.sectionSize(COL_RESULT) == 260
    assert header.sectionResizeMode(COL_FILE) != header.ResizeMode.Stretch


@pytest.mark.parametrize("width", [900, 1280, 1800])
def test_columns_reach_the_right_edge(make_gui, qapp, width):
    gui = make_gui()
    gui.resize(width, 600)
    gui.show()
    qapp.processEvents()
    header = gui.table.horizontalHeader()
    assert header.length() >= gui.table.viewport().width()


def test_the_preview_column_can_be_dragged_bigger_and_keeps_its_width(make_gui, qapp):
    from archive_helper.compressor.model import COL_THUMB

    gui = make_gui()
    gui.resize(1500, 600)
    gui.show()
    qapp.processEvents()
    header, rows = gui.table.horizontalHeader(), gui.table.verticalHeader()
    assert header.sectionSize(COL_THUMB) == 64 and rows.defaultSectionSize() == 46

    header.resizeSection(COL_THUMB, 200)
    assert rows.defaultSectionSize() == round(192 * 9 / 16) + 8   # 16:9 pictures, the rows grow with them
    assert header.length() <= gui.table.viewport().width() + 1   # the file name made room, no sideways scrolling
    header.resizeSection(COL_THUMB, 2000)
    assert header.sectionSize(COL_THUMB) == 320   # the thumbnails are not sharper than that
    header.resizeSection(COL_THUMB, 10)
    assert header.sectionSize(COL_THUMB) == 40 and rows.defaultSectionSize() == 46

    header.resizeSection(COL_THUMB, 150)
    gui.save_view()
    again = make_gui()
    assert again.table.horizontalHeader().sectionSize(COL_THUMB) == 150


def test_dragging_a_column_narrower_leaves_no_gap_at_the_right(make_gui, qapp):
    from archive_helper.compressor.model import COL_FILE

    gui = make_gui()
    gui.resize(1500, 600)
    gui.show()
    qapp.processEvents()
    gui.table.horizontalHeader().resizeSection(COL_FILE, 180)
    qapp.processEvents()
    header = gui.table.horizontalHeader()
    assert header.length() >= gui.table.viewport().width()
    assert header.sectionSize(6) > 160   # the status column took the freed room


def test_progress_text_shows_how_many_durations_are_read(qapp, make_gui):
    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    root = Path("/src")
    rows = [VideoRow(src=root / f"{n}.mp4", dst=root / f"{n}_x.mp4", size=10, duration=5 if n < 2 else None, root=root) for n in range(4)]
    gui.model.reset(rows)

    class Busy:
        def isRunning(self):
            return True

    gui.duration_worker = Busy()
    assert "2 von 4" in gui.progress_message(rows, gui.model.counts())
    gui.duration_worker = None


def test_quality_hint_does_not_change_the_layout_when_the_slider_moves(qapp, make_gui):
    gui = make_gui()
    gui.resize(1300, 800)
    gui.show()
    qapp.processEvents()
    heights, tops = set(), set()
    for crf in (28, 26, 23, 20, 18, 17, 15):
        gui.slider_crf.setValue(crf)
        qapp.processEvents()
        heights.add(gui.quality_hint.height())
        tops.add(gui.combo_renderer.mapTo(gui, gui.combo_renderer.rect().topLeft()).y())
    assert len(heights) == 1 and len(tops) == 1


# --- pause, notification, keep awake ----------------------------------------------------------------------------------

class FakeKeepAwake:
    def __init__(self):
        self.starts = self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


@pytest.fixture
def fake_worker(monkeypatch):
    """An ArchiveWorker that only waits, so pause/stop logic can be tested without ffmpeg."""
    from PySide6.QtCore import QThread, Signal

    from archive_helper.compressor import gui as gui_module

    class Fake(QThread):
        progress_step = Signal(int, str)
        status_update_path = Signal(str, str, str, dict)
        file_duration_discovered_path = Signal(str, float)
        file_progress_path = Signal(str, int)
        ffmpeg_log_line_path = Signal(str, str)
        finished_all = Signal()

        def __init__(self, src, dst, jobs, queue, settings):
            super().__init__()
            self.settings, self.paused, self.stopped = settings, False, False

        def run(self):
            while not self.stopped:
                self.msleep(10)

        def stop(self):
            self.stopped = True

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

        def set_queue(self, items):
            pass

    monkeypatch.setattr(gui_module, "ArchiveWorker", Fake)
    return Fake


def started_gui(qapp, make_gui, tmp_path, fake_worker):
    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    src = tmp_path / "src"
    src.mkdir()
    gui.txt_src.setText(str(src))
    gui.txt_dst.setText(str(tmp_path / "dst"))
    root = Path(src)
    rows = [VideoRow(src=root / f"{n}.mp4", dst=tmp_path / "dst" / f"{n}_a.mp4", size=100, root=root) for n in "ab"]
    gui.scan_items = [{"path": r.src, "dst_path": r.dst, "size": r.size, "exists_compressed": False} for r in rows]
    gui.model.reset(rows)
    gui.keep_awake = FakeKeepAwake()
    gui.notifier = lambda *args: gui.notified.append(args)
    gui.notified = []
    return gui


def test_pause_freezes_the_run_and_lets_the_computer_sleep(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.cb_keep_awake.setChecked(True)
    gui.keep_awake.stops = 0   # setting the box while idle only released nothing
    gui.start_archiving()
    assert gui.is_archiving() and gui.keep_awake.starts == 1 and not gui.btn_pause.isHidden()

    gui.toggle_pause()
    assert gui.worker.paused and gui.is_paused() and gui.keep_awake.stops == 1
    assert gui.btn_pause.text() == "Fortsetzen" and "Pausiert" in gui.progress_text.text()

    gui.toggle_pause()
    assert not gui.worker.paused and gui.keep_awake.starts == 2 and gui.btn_pause.text() == "Pause"
    gui.stop_archiving()
    wait_idle(qapp, gui)
    assert gui.btn_pause.isHidden()


def test_durations_found_during_a_run_put_the_waiting_videos_in_order(qapp, make_gui, tmp_path, fake_worker):
    """Durations come in the background: started before they were all known, the queue still follows the sorting."""
    from PySide6.QtCore import Qt
    from archive_helper.compressor.model import COL_DURATION

    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.table.sortByColumn(COL_DURATION, Qt.AscendingOrder)
    gui.start_archiving()
    queued = []
    gui.worker.set_queue = lambda items: queued.append([item['path'].name for item in items])

    src = tmp_path / "src"
    gui.on_duration_found(str(src / "a.mp4"), 9.0)
    gui.on_duration_found(str(src / "b.mp4"), 2.0)
    spin(600)
    assert queued == [["b.mp4", "a.mp4"]]   # once, after the durations settled
    gui.stop_archiving()
    wait_idle(qapp, gui)


def test_status_tooltip_shows_running_time_time_left_and_how_long_it_took(qapp, make_gui, tmp_path, fake_worker):
    from PySide6.QtCore import Qt
    from archive_helper.compressor.model import COL_STATUS

    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.start_archiving()
    path = str(tmp_path / "src" / "a.mp4")
    tooltip = lambda: gui.model.data(gui.model.index(0, COL_STATUS), Qt.ToolTipRole)

    gui.on_status_update(path, "running", "", {})
    assert tooltip() == "Läuft seit 0 Sek. · Restzeit wird geschätzt…"
    gui.model.rows[0].started_at -= 150   # started 2.5 minutes ago
    gui.on_file_progress(path, 25)
    assert tooltip() == "Läuft seit 2 Min. 30 Sek. · noch etwa 7 Min. 30 Sek."

    gui.toggle_pause()   # a pause neither counts as running time …
    gui.model.rows[0].started_at -= 600   # 10 minutes pass while paused
    gui.paused_at -= 600
    gui.model.paused_since -= 600
    assert tooltip().startswith("Läuft seit 2 Min. 30 Sek.")
    gui.toggle_pause()   # … nor afterwards
    assert tooltip().startswith("Läuft seit 2 Min. 30 Sek.")

    gui.on_status_update(path, "finished", "-40 %", {"src_size": 100, "dst_size": 60})
    assert tooltip() == "Hat 2 Min. 30 Sek. gedauert\n-40 %"
    gui.stop_archiving()
    wait_idle(qapp, gui)


def test_keep_awake_follows_the_checkbox_next_to_start(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    assert not gui.cb_keep_awake.isChecked()          # off unless the user asks for it
    gui.start_archiving()
    assert gui.keep_awake.starts == 0                 # off: a run does not keep the computer awake
    gui.cb_keep_awake.setChecked(True)                # ticking it during the run takes effect at once
    assert gui.keep_awake.starts == 1
    gui.cb_keep_awake.setChecked(False)
    assert gui.keep_awake.stops >= 1
    gui.cb_keep_awake.setChecked(True)
    gui.save_defaults()
    gui.stop_archiving()
    wait_idle(qapp, gui)
    assert make_gui().cb_keep_awake.isChecked() is True


def test_paused_time_does_not_count_for_the_time_estimate(qapp, make_gui, tmp_path, fake_worker, monkeypatch):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    gui.start_archiving()
    started = gui.run_started
    clock["now"] += 30
    gui.toggle_pause()
    clock["now"] += 600
    gui.toggle_pause()
    assert gui.run_started == pytest.approx(started + 600)
    gui.stop_archiving()
    wait_idle(qapp, gui)


def test_stopping_by_hand_sends_no_notification_and_releases_the_computer(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.start_archiving()
    gui.stop_archiving()
    wait_idle(qapp, gui)
    assert gui.notified == [] and gui.keep_awake.stops >= 1


def test_a_finished_run_notifies_with_the_result(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    gui.start_archiving()
    gui.on_status_update(str(gui.model.rows[0].src), "finished", "", {"src_size": 100, "dst_size": 40})
    gui.on_status_update(str(gui.model.rows[1].src), "error", "FFmpeg error", {})
    gui.worker.stopped = True   # the thread ends by itself, as after the last video
    wait_idle(qapp, gui)
    assert len(gui.notified) == 1
    _window, title, message = gui.notified[0]
    assert title == "Archive Helper" and "1 von 2" in message and "1" in message.split("·")[1]


def test_low_priority_is_a_saved_setting_and_part_of_the_run_settings(qapp, make_gui, tmp_path, fake_worker):
    gui = started_gui(qapp, make_gui, tmp_path, fake_worker)
    assert gui.cb_low_priority.isChecked()
    gui.cb_low_priority.setChecked(False)
    gui.start_archiving()
    assert gui.worker.settings["low_priority"] is False
    gui.stop_archiving()
    wait_idle(qapp, gui)
    gui.save_defaults()
    assert make_gui().cb_low_priority.isChecked() is False


def test_worker_pauses_and_resumes_its_processes(qapp, tmp_path):
    import os
    import signal

    from archive_helper.compressor.workers import ArchiveWorker

    if sys.platform == "win32":
        pytest.skip("uses POSIX process states")
    process = subprocess.Popen(["sleep", "30"], preexec_fn=os.setsid)

    def state():
        return subprocess.run(["ps", "-o", "stat=", "-p", str(process.pid)], capture_output=True, text=True).stdout.strip()

    try:
        worker = ArchiveWorker(tmp_path, tmp_path, 1, [], {})
        worker.active_processes = [(process, Path("a.mp4"), Path("a_x.mp4"), 1, b"")]
        worker.pause()
        spin(100)
        assert worker.paused and state().startswith("T")
        worker.stop()   # stopping while paused wakes the processes up, so they can be killed
        spin(100)
        assert not worker.paused and not state().startswith("T")
    finally:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()


# --- summary wording, path fields -------------------------------------------------------------------------------------

def test_savings_are_written_as_smaller_or_larger_not_as_a_negative_percentage(qapp, make_gui):
    from archive_helper.compressor.model import VideoRow

    gui = make_gui()
    root = Path("/src")
    gui.model.reset([VideoRow(src=root / "a.mp4", dst=root / "a_x.mp4", size=1000, out_size=250, status="done", root=root)])
    gui.refresh_summary()
    text = gui.summary_values["saving"][1].text()
    assert "75 % kleiner" in text and "-" not in text and "+" not in text
    assert text.endswith("→ 250.00 B")  # the size afterwards, not the amount saved

    gui.model.reset([VideoRow(src=root / "a.mp4", dst=root / "a_x.mp4", size=1000, out_size=1500, status="done", root=root)])
    gui.refresh_summary()
    assert "50 % größer → 1.46 KB" in gui.summary_values["saving"][1].text()


def test_long_paths_keep_their_start_and_last_folder_when_not_edited(qapp):
    from archive_helper.compressor.widgets import DropLineEdit

    field = DropLineEdit()
    field.setFixedWidth(200)
    path = "/Users/fheinrich/Movies/Neuer Iphone Videos Export/Compressed - Neuer Iphone Videos Export/"
    field.setText(path)
    field.show()
    qapp.processEvents()
    field.setFocus()
    assert field.elided_text() is None   # while it is being edited the real text is shown
    field.clearFocus()
    shown = field.elided_text()
    assert shown and shown.startswith("/Users") and shown.endswith("Export/") and "…" in shown
    assert field.toolTip() == path and field.text() == path   # the real value is untouched

    field.setText("/short")
    assert field.elided_text() is None and field.toolTip() == "/short"
    field.setText("")
    assert field.elided_text() is None


# --- target names, collisions, source = target -----------------------------------------------------------------------

def scanned_rows(qapp, make_gui, tmp_path, names, postfix="_archived", flatten=False, dst=None):
    src = tmp_path / "src"
    make_named_videos(src, *names)
    gui = make_gui()
    gui.txt_postfix.setText(postfix)
    gui.cb_flatten.setChecked(flatten)
    scan(qapp, gui, str(src), str(dst or tmp_path / "dst"))
    return gui, {str(row.src.relative_to(src)): row for row in gui.model.rows}


def test_same_stem_different_extension_keeps_the_extension_in_both_names(qapp, make_gui, tmp_path):
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["test.mov", "test.mp4", "other.mov"])
    assert rows["test.mov"].dst.name == "test.mov_archived.mp4"
    assert rows["test.mp4"].dst.name == "test.mp4_archived.mp4"
    assert rows["other.mov"].dst.name == "other_archived.mp4"         # no collision: the usual name
    assert not any(row.duplicate or row.conflict for row in rows.values())
    assert len({row.dst for row in rows.values()}) == 3


def test_collisions_are_found_regardless_of_upper_and_lower_case(qapp, make_gui, tmp_path):
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["Test.MOV", "test.mp4"])
    assert rows["Test.MOV"].dst.name == "Test.MOV_archived.mp4" and rows["test.mp4"].dst.name == "test.mp4_archived.mp4"
    assert not any(row.duplicate for row in rows.values())


def test_three_sources_with_one_stem_all_get_their_own_name(qapp, make_gui, tmp_path):
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["a.mov", "a.mp4", "a.mkv"])
    assert sorted(row.dst.name for row in rows.values()) == ["a.mkv_archived.mp4", "a.mov_archived.mp4", "a.mp4_archived.mp4"]


def test_collision_naming_also_works_for_an_empty_postfix_and_removes_the_conflict(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["test.mov", "test.mp4"], postfix="", dst=src)
    assert {row.dst.name for row in rows.values()} == {"test.mov.mp4", "test.mp4.mp4"}
    assert not any(row.conflict or row.duplicate for row in rows.values())


def test_collisions_inside_one_flattened_folder_are_still_reported_as_duplicates(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    for folder in ("a", "b"):
        (src / folder).mkdir(parents=True)
        shutil.copy(EXAMPLE_VIDEOS[0], src / folder / "clip.mov")
    gui = make_gui()
    gui.cb_flatten.setChecked(True)
    scan(qapp, gui, str(src), str(tmp_path / "dst"))
    rows = {str(row.src.relative_to(src)): row for row in gui.model.rows}
    assert all(row.duplicate for row in rows.values())               # identical names cannot be told apart by extension
    assert {row.dst.name for row in rows.values()} == {"clip.mov_archived.mp4"}


def test_a_result_with_an_extension_name_is_found_again_and_not_compressed_twice(qapp, make_gui, tmp_path):
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["test.mov", "test.mp4"])
    for row in rows.values():
        row.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(row.src, row.dst)
    scan(qapp, gui, str(tmp_path / "src"), str(tmp_path / "dst"))
    assert {row.status for row in gui.model.rows} == {"exists"}


def test_same_source_and_target_file_is_flagged_not_skipped_and_not_shown_as_archived(qapp, make_gui, tmp_path):
    src = tmp_path / "src"
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["clip.mp4", "movie.mov"], postfix="", dst=src)
    clip = rows["clip.mp4"]
    assert clip.conflict and clip.status == "planned" and clip.out_size is None
    from archive_helper.compressor.model import status_label
    assert status_label(clip) == "⚠ Quelle = Ziel!"
    assert [item["path"].name for item in gui.queue_items() if item["conflict"]] == ["clip.mp4"]
    assert "Quelle = Ziel" in gui.progress_text.text()


def test_a_target_that_is_another_source_video_counts_as_source_equals_target(qapp, make_gui, tmp_path):
    # movie.mov would be written to movie.mp4, which is itself a video to compress (no collision: different stems)
    src = tmp_path / "src"
    make_named_videos(src, "movie.mov", "movie.mp4", "x.mov")
    gui = make_gui()
    gui.txt_postfix.setText("")
    scan(qapp, gui, str(src), str(src))
    names = {row.dst.name for row in gui.model.rows}
    assert names == {"movie.mov.mp4", "movie.mp4.mp4", "x.mp4"}      # collision rule resolves it
    assert not any(row.conflict for row in gui.model.rows)


def test_starting_with_source_equals_target_needs_the_delayed_confirmation(qapp, make_gui, tmp_path, fake_worker, monkeypatch):
    from archive_helper.compressor import gui as gui_module

    src = tmp_path / "src"
    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["clip.mp4"], postfix="", dst=src)
    asked = []
    monkeypatch.setattr(gui_module, "confirm_after_delay", lambda *args: asked.append(args) or False)
    gui.start_archiving()
    assert len(asked) == 1 and not gui.is_archiving()
    assert "1 Videos" in asked[0][2] and "ERSETZT" in asked[0][2] and asked[0][3] == "Originale ersetzen"

    monkeypatch.setattr(gui_module, "confirm_after_delay", lambda *args: asked.append(args) or True)
    gui.start_archiving()
    assert gui.is_archiving()
    gui.stop_archiving()
    wait_idle(qapp, gui)


def test_no_confirmation_when_nothing_replaces_an_original(qapp, make_gui, tmp_path, fake_worker, monkeypatch):
    from archive_helper.compressor import gui as gui_module

    gui, rows = scanned_rows(qapp, make_gui, tmp_path, ["clip.mp4"])
    monkeypatch.setattr(gui_module, "confirm_after_delay", lambda *args: pytest.fail("no warning expected"))
    gui.start_archiving()
    assert gui.is_archiving()
    gui.stop_archiving()
    wait_idle(qapp, gui)


@pytest.mark.parametrize("result, original, warn", [
    (3.5, 1.3, True),    # VideoToolbox 65 on a hazy sky
    (1.27, 1.30, False),  # x265 CRF 20 on the same clip
    (3.5, 3.0, False),   # the original is that blocky itself
    (2.5, None, True),   # original replaced: nothing to compare with
    (1.5, None, False),
])
def test_block_warning_threshold(result, original, warn):
    from archive_helper.compressor.utils import is_blocky

    assert is_blocky(result, original) is warn


def test_block_samples_cover_long_videos_and_stay_inside_short_ones():
    from archive_helper.compressor.utils import block_sample_starts

    assert block_sample_starts(100) == [15.0, 50.0, 85.0]
    assert block_sample_starts(3) == [0]
    assert block_sample_starts(None) == [0]


@requires_ffmpeg
def test_block_worker_measures_the_real_result_and_original(qapp):
    from PySide6.QtCore import Qt
    from archive_helper.compressor.workers import BlockCheckWorker

    worker = BlockCheckWorker()
    found = []
    worker.checked.connect(lambda path, check: found.append((path, check)), Qt.DirectConnection)
    worker.add(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[1])
    for _ in range(600):
        if found:
            break
        time.sleep(0.05)
    worker.stop()
    worker.wait()
    assert found[0][0] == str(EXAMPLE_VIDEOS[0])
    result, original = found[0][1]
    assert result > 0 and original > 0


def test_blocky_result_is_flagged_in_status_tooltip_and_survives_a_rescan(qapp, make_gui, tmp_path):
    from PySide6.QtCore import Qt
    from archive_helper.compressor.model import COL_STATUS, VideoRow

    src, dst = tmp_path / "clip.mov", tmp_path / "out" / "clip_archived.mp4"
    make_named_videos(tmp_path, "clip.mov")
    make_named_videos(dst.parent, dst.name)
    gui = make_gui()
    gui.model.reset([VideoRow(src=src, dst=dst, size=1, out_size=1, status="done", root=tmp_path)])
    gui.on_block_checked(str(src), (3.5, 1.3))
    index = gui.model.index(0, COL_STATUS)
    assert "Blöcke?" in index.data()
    assert "3.5" in index.data(Qt.ToolTipRole) and "1.3" in index.data(Qt.ToolTipRole)

    gui.on_unified_scan_finished([{'path': src, 'dst_path': dst, 'size': 1, 'exists_compressed': True, 'comp_size': 1}])
    assert gui.model.rows[0].blocky

    gui.block_checks.clear()
    gui.on_block_checked(str(src), (1.2, 1.3))
    assert "Blöcke?" not in gui.model.index(0, COL_STATUS).data()


def test_shared_style_avoids_palette_roles_that_are_broken_in_macos_dark_mode():
    from video_helper_core.style import STYLE

    assert "alternate-base" not in STYLE and "placeholder-text)" not in STYLE


def test_no_new_thumbnails_are_made_while_the_viewer_is_open(qapp, make_gui, tmp_path, monkeypatch):
    # Thumbnails of a large folder kept the disk and the CPU busy, so the viewer could hardly play.
    from types import SimpleNamespace

    from archive_helper.compressor import gui as gui_module

    seen = []

    class FakeDialog:
        def __init__(self, *args):
            pass

        def exec(self):
            seen.append(gui.thumbnails.allowed.is_set())

    monkeypatch.setattr(gui_module, "CompareVideoDialog", FakeDialog)
    gui = make_gui()
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    gui.compare(SimpleNamespace(src=video, dst=tmp_path / "missing.mp4", out_size=None))
    assert seen == [False]
    assert gui.thumbnails.allowed.is_set()
