"""The file actions on an original and its result, and undoing them (compressor/file_actions.py)."""
import pytest

from archive_helper.compressor import file_actions


def pair(tmp_path, same_folder=False):
    original = tmp_path / "src" / "clip.mov"
    result = (original.parent if same_folder else tmp_path / "dst") / "clip_archived.mp4"
    for path, data in ((original, b"original"), (result, b"result")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return original, result


def files(tmp_path):
    """Every file under tmp_path except the trash, with its content."""
    return {str(p.relative_to(tmp_path)): p.read_bytes() for p in sorted(tmp_path.rglob("*"))
            if p.is_file() and "Trash" not in p.parts}


@pytest.mark.parametrize("action", file_actions.ACTIONS)
@pytest.mark.parametrize("same_folder", [False, True])
def test_every_action_can_be_undone_to_the_byte(tmp_path, trash, action, same_folder):
    original, result = pair(tmp_path, same_folder)
    before = files(tmp_path)
    steps = file_actions.apply(action, original, result)
    assert files(tmp_path) != before
    file_actions.undo(steps)
    assert files(tmp_path) == before


def test_what_each_action_does(tmp_path, trash):
    original, result = pair(tmp_path)
    file_actions.apply("overwrite", original, result)
    assert not original.exists() and (original.parent / result.name).read_bytes() == b"result"
    assert (trash / "clip.mov").read_bytes() == b"original"

    original, result = pair(tmp_path / "2")
    file_actions.apply("swap", original, result)
    assert original.read_bytes() == b"result" and result.read_bytes() == b"original"


def test_replacing_keeps_another_file_of_the_results_name_and_brings_it_back(tmp_path, trash):
    original, result = pair(tmp_path)
    other = original.parent / result.name
    other.write_bytes(b"someone else's")
    steps = file_actions.apply("overwrite", original, result)
    assert other.read_bytes() == b"result"
    file_actions.undo(steps)
    assert other.read_bytes() == b"someone else's" and original.read_bytes() == b"original"


def test_undo_is_refused_without_a_change_when_the_trash_was_emptied(tmp_path, trash):
    original, result = pair(tmp_path)
    steps = file_actions.apply("overwrite", original, result)
    (trash / "clip.mov").unlink()   # emptied
    before = files(tmp_path)
    with pytest.raises(file_actions.UndoError, match="clip.mov"):
        file_actions.undo(steps)
    assert files(tmp_path) == before


def test_undo_is_refused_when_a_file_of_that_name_appeared(tmp_path, trash):
    original, result = pair(tmp_path)
    steps = file_actions.apply("del_orig", original, result)
    original.write_bytes(b"new")
    with pytest.raises(file_actions.UndoError):
        file_actions.undo(steps)
    assert original.read_bytes() == b"new"


def test_a_refusing_trash_changes_nothing(tmp_path, monkeypatch):
    from video_helper_core import trash as trash_module

    original, result = pair(tmp_path)
    before = files(tmp_path)
    monkeypatch.setattr(trash_module, "_system_trash", lambda path: None)
    for action in file_actions.ACTIONS:
        if action == "swap":
            continue   # moves only, no trash
        with pytest.raises(OSError):
            file_actions.apply(action, original, result)
        assert files(tmp_path) == before


# --- in the page: Edit > Undo / Redo, and the menus -----------------------------------------------------------------

@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from archive_helper.compressor.gui import ArchiverGUI
    from archive_helper.compressor.model import VideoRow

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    gui = ArchiverGUI()
    original, result = pair(tmp_path)
    gui.model.reset([VideoRow(src=original, dst=result, size=8, status="exists", out_size=6, root=original.parent)])
    gui.menus()
    yield gui
    gui.shutdown()


def test_an_action_is_undone_and_redone_from_the_edit_menu(page, tmp_path, trash):
    row = page.model.rows[0]
    before = files(tmp_path)
    assert not page.undo_action.isEnabled()
    page.handle_compare_action("del_comp", row)
    assert not row.dst.exists() and page.model.rows[0].status == "planned"
    assert page.undo_action.isEnabled() and "In den Papierkorb" in page.undo_action.text()

    page.undo_action.trigger()
    assert files(tmp_path) == before
    assert (page.model.rows[0].status, page.model.rows[0].out_size) == ("exists", 6)
    assert not page.undo_action.isEnabled() and page.redo_action.isEnabled()

    page.redo_action.trigger()
    assert not row.dst.exists() and page.model.rows[0].status == "planned"
    page.undo_action.trigger()
    assert files(tmp_path) == before


def test_undo_that_is_no_longer_possible_says_why_and_keeps_it(page, tmp_path, trash, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    page.handle_compare_action("del_orig", page.model.rows[0])
    for path in trash.iterdir():
        path.unlink()   # the trash was emptied
    page.undo_action.trigger()
    assert len(warnings) == 1 and "Papierkorb" in warnings[0]
    assert page.undo_action.isEnabled()


def test_no_undo_while_the_video_is_being_compressed(page, tmp_path, trash, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    page.handle_compare_action("del_comp", page.model.rows[0])
    page.model.update(page.model.rows[0].src, status="running")
    page.undo_action.trigger()
    assert len(warnings) == 1 and not page.model.rows[0].dst.exists()


def test_the_videos_menu_offers_what_the_right_click_menu_does(page):
    page.table.selectRow(0)
    page.video_menu.aboutToShow.emit()
    texts = [a.text() for a in page.video_menu.actions() if a.text()]
    assert "Vergleichen" in texts and "Metadaten" in texts and any(t.startswith("Neu scannen") for t in texts)
    page.table.clearSelection()
    page.video_menu.aboutToShow.emit()
    assert page.video_menu.actions()[0].text() == "Kein Video ausgewählt"


@pytest.mark.parametrize("native", [True, False])
def test_the_window_shows_the_pages_menus_only_while_it_is_open(qapp, monkeypatch, native):
    from PySide6.QtGui import QKeySequence
    from video_helper_core import shell
    from archive_helper import app

    from PySide6.QtWidgets import QMenuBar

    monkeypatch.setattr(shell, "NATIVE_MENU_BAR", native)
    monkeypatch.setattr(QMenuBar, "isNativeMenuBar", lambda self: True)   # as on a Mac (tests run offscreen)
    window = app.ArchiveHelperWindow()
    window.show_tool("compressor")
    titles = [a.text() for a in window.menuBar().actions()]
    page = window.compressor_tab
    if native:
        assert titles == ["Bearbeiten", "Videos"]
    else:   # no bar in the window, but the shortcuts work through the page
        assert titles == [] and not window.menuBar().isVisible()
        assert any(a.shortcut() == QKeySequence(QKeySequence.Undo) for a in page.actions())
    window.show_landing()
    assert window.menuBar().actions() == []
    window.close()
