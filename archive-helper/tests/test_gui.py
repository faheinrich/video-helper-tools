import pytest
from PySide6.QtCore import QPointF, QRectF, Qt, QUrl

from conftest import EXAMPLE_VIDEOS, requires_ffmpeg, spin


def test_compare_dialog_plays_both_videos(qapp):
    from PySide6.QtMultimedia import QMediaPlayer
    from archive_helper.compressor.widgets import CompareVideoDialog

    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[1])
    dialog.show()
    spin(2000)

    for player in (dialog.player_orig, dialog.player_comp):
        assert player.error() == QMediaPlayer.Error.NoError
        assert player.duration() > 0
        assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    dialog.toggle_play()
    assert dialog.player_orig.playbackState() == QMediaPlayer.PlaybackState.PausedState

    dialog.reject()
    assert dialog.player_orig.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert dialog.player_orig.source().isEmpty()


def test_compare_dialog_plays_just_the_original(qapp):
    from PySide6.QtMultimedia import QMediaPlayer
    from archive_helper.compressor.widgets import CompareVideoDialog

    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0])
    dialog.show()
    spin(2000)

    assert dialog.player_comp is None and len(dialog.players()) == 1
    assert dialog.player_orig.error() == QMediaPlayer.Error.NoError
    assert dialog.player_orig.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    dialog.toggle_play()
    assert dialog.player_orig.playbackState() == QMediaPlayer.PlaybackState.PausedState
    dialog.zoom_in()  # the zoom and reset controls work with one view
    dialog.reset_views()
    dialog.reject()
    assert dialog.player_orig.source().isEmpty()


@requires_ffmpeg
def test_compare_dialog_shows_rotated_clip_upright(qapp, tmp_path):
    """Portrait phone clips carry a display matrix; Qt 6 applies it, the app must not rotate again."""
    import subprocess
    from PySide6.QtGui import QColor, QImage, QPainter
    from archive_helper.compressor.widgets import CompareVideoDialog

    rotated = tmp_path / "portrait.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(EXAMPLE_VIDEOS[0]), "-c", "copy", str(rotated)],
        check=True,
    )

    dialog = CompareVideoDialog(rotated, rotated)
    dialog.show()
    spin(2000)
    dialog.player_orig.pause()

    magenta = QColor(255, 0, 255)
    item = dialog.view_orig.video_item
    scene = dialog.view_orig.scene
    scene.setBackgroundBrush(magenta)
    rect = dialog.view_orig.frame_rect()
    image = QImage(int(rect.width()), int(rect.height()), QImage.Format.Format_RGB32)
    image.fill(magenta)
    painter = QPainter(image)
    scene.render(painter, image.rect(), item.mapRectToScene(rect))
    painter.end()
    dialog.reject()

    def is_background(x, y):
        c = image.pixelColor(x, y)
        return c.red() > 240 and c.green() < 20 and c.blue() > 240

    # Portrait box for a portrait clip, filled edge to edge (no bars), i.e. drawn upright.
    assert rect.height() > rect.width()
    # Qt draws into boundingRect; it must be the whole box, not a landscape area inside it.
    assert item.boundingRect() == QRectF(QPointF(0, 0), item.size())
    assert not any(is_background(x, image.height() // 2) for x in range(3, image.width() - 3, 10))
    assert not any(is_background(image.width() // 2, y) for y in range(3, image.height() - 3, 10))


@requires_ffmpeg
def test_rotated_clip_uses_the_full_view_height(qapp, tmp_path):
    """Regression: a portrait 4K clip (stored landscape + rotation) was fitted as landscape and drawn tiny."""
    import subprocess
    from archive_helper.compressor.widgets import CompareVideoDialog

    rotated = tmp_path / "portrait.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(EXAMPLE_VIDEOS[0]), "-c", "copy", str(rotated)],
        check=True,
    )
    dialog = CompareVideoDialog(rotated, EXAMPLE_VIDEOS[0])
    dialog.setWindowState(Qt.WindowNoState)
    dialog.resize(1600, 700)  # wide window: the case where the portrait frame was scaled wrong
    dialog.show()
    spin(1500)
    view = dialog.view_orig
    shown = view.mapFromScene(view.frame_rect()).boundingRect()
    viewport = view.viewport().height()
    dialog.reject()
    assert shown.height() >= viewport * 0.95


def open_compare_dialog(qapp, wait_ms=2000):
    from archive_helper.compressor.widgets import CompareVideoDialog

    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[0])
    dialog.show()
    spin(wait_ms)
    return dialog


def test_compare_player_pulls_the_compressed_video_back_in_sync(qapp):
    from archive_helper.compressor.widgets import SYNC_TOLERANCE_MS

    dialog = open_compare_dialog(qapp)
    assert dialog.is_playing()
    assert dialog.audio_comp.isMuted() and not dialog.audio_orig.isMuted()
    # Simulate the faster-decoding file running ahead.
    dialog.player_comp.setPosition(dialog.player_orig.position() + 3000)
    spin(700)
    drift = abs(dialog.player_comp.position() - dialog.player_orig.position())
    dialog.reject()
    assert drift <= SYNC_TOLERANCE_MS + 250  # both keep playing between the checks


def test_compare_player_scrubber_and_restart(qapp):
    dialog = open_compare_dialog(qapp)
    assert dialog.slider_pos.maximum() == dialog.player_orig.duration() > 0
    assert dialog.lbl_time.text().endswith("/ 0:26")

    dialog.pause()
    dialog.slider_pos.setValue(15000)  # like a user click/drag on the timeline
    spin(500)
    for player in (dialog.player_orig, dialog.player_comp):
        assert abs(player.position() - 15000) < 300
    assert dialog.lbl_time.text().startswith("0:15")

    dialog.restart()
    spin(500)
    assert dialog.is_playing()
    assert dialog.player_orig.position() < 3000
    dialog.reject()


def test_enter_does_not_trigger_a_dialog_button(qapp):
    from PySide6.QtWidgets import QPushButton

    dialog = open_compare_dialog(qapp, wait_ms=300)
    assert not any(b.isDefault() or b.autoDefault() for b in dialog.findChildren(QPushButton))
    dialog.reject()


def test_player_error_names_side_file_and_format(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from archive_helper.compressor.widgets import CompareVideoDialog

    broken = tmp_path / "broken_archived.mp4"
    broken.write_bytes(EXAMPLE_VIDEOS[0].read_bytes()[:200_000])  # like a half-written result
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda parent, title, text: shown.append(text)))
    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], broken)
    dialog.show()
    spin(3000)
    dialog.reject()
    assert len(shown) == 1
    assert "komprimierte Version" in shown[0] and "broken_archived.mp4" in shown[0] and "Format:" in shown[0]


def test_players_use_software_decoding():
    # Qt + VideoToolbox reported "Unknown error occurred" at the end of some camera H.264 files.
    import os
    import subprocess
    import sys

    code = "import os, archive_helper; print(os.environ.get('QT_FFMPEG_DECODING_HW_DEVICE_TYPES'))"
    from conftest import REPO_ROOT

    env = {k: v for k, v in os.environ.items() if k != "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"}
    env["PYTHONPATH"] = str(REPO_ROOT)  # works whether or not the package is installed in this environment
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert result.stdout.strip() == ","


def test_path_field_accepts_dropped_folder(qapp, tmp_path):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    from archive_helper.compressor.widgets import DropLineEdit

    field = DropLineEdit()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    field.dropEvent(QDropEvent(QPointF(1, 1), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier))
    assert field.text() == str(tmp_path)


def test_video_view_zooms_with_the_wheel(qapp):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    from archive_helper.compressor.widgets import ZoomableVideoView

    from PySide6.QtMultimedia import QMediaPlayer

    view = ZoomableVideoView(QMediaPlayer())
    seen = []
    view.zoom_changed.connect(seen.append)
    for delta in (120, -120):
        view.wheelEvent(QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, delta), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
    assert len(seen) == 2 and seen[0] > 1 and seen[1] == pytest.approx(1.0)


def test_slider_view_puts_the_videos_on_top_of_each_other_with_a_movable_dividing_line(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from archive_helper.compressor.widgets import CompareVideoDialog

    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[1])
    dialog.setWindowState(Qt.WindowNoState)
    dialog.resize(1000, 600)
    dialog.show()
    spin(300)
    stage = dialog.stage
    assert stage.mode == "side" and dialog.mode_buttons["side"].isChecked()
    assert [b.text() for b in dialog.mode_buttons.values()] == ["◫ Nebeneinander", "◧ Schieberegler", "⇄ Wechseln"]
    assert dialog.view_orig.geometry().right() < dialog.view_comp.geometry().left()   # side by side

    dialog.mode_buttons["slider"].click()
    assert stage.mode == "slider" and dialog.mode_buttons["slider"].isChecked()
    # one view shows both, so they repaint together (two stacked views: the covered one froze on macOS)
    assert dialog.view_orig.geometry() == stage.rect() and dialog.view_comp.isHidden()
    assert dialog.player_comp.videoOutput() is dialog.overlay_item
    assert dialog.overlay_item.scene() is dialog.view_orig.scene
    half = stage.width() // 2

    def clip_left():   # where the compressed version starts, on screen
        view = dialog.view_orig
        return view.mapFromScene(view.clip_item.rect().topLeft()).x() + view.viewport().x()

    assert abs(clip_left() - half) <= 1 and stage.handle.isVisible()
    assert all(tag.isVisible() for tag in stage.tags)

    QTest.mousePress(stage.handle, Qt.LeftButton, pos=QPoint(stage.handle.width() // 2 - 100, 50))   # grab and pull left
    assert abs(stage.split_x() - (half - 100)) <= 2
    stage.handle.moved.emit(stage.width() // 4)
    assert abs(stage.split - 0.25) < 0.01
    assert abs(clip_left() - stage.split_x()) <= 1
    assert abs(stage.handle.geometry().center().x() - stage.split_x()) <= 1
    stage.handle.moved.emit(-50)   # dragged past the edge: the whole picture is the compressed version
    assert stage.split == 0.0
    dialog.mode_buttons["side"].click()   # back: each video in its own view again
    assert dialog.player_comp.videoOutput() is dialog.view_comp.video_item and dialog.view_comp.isVisible()
    assert dialog.view_orig.clip_item is None
    dialog.mode_buttons["slider"].click()
    dialog.reject()

    again = CompareVideoDialog(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[1])   # the choice is kept
    assert again.stage.overlay
    again.mode_buttons["side"].click()
    again.reject()
    assert not CompareVideoDialog(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[1]).stage.overlay


def test_no_slider_view_for_a_single_video(qapp):
    from archive_helper.compressor.widgets import CompareVideoDialog

    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0])
    assert all(button.isHidden() for button in dialog.mode_buttons.values()) and not dialog.stage.overlay
    dialog.reject()


@requires_ffmpeg
def test_zooming_and_panning_keep_the_slider_view_aligned(qapp, tmp_path):
    import subprocess
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    from archive_helper.compressor.widgets import CompareVideoDialog

    # like a result with limited resolution: same picture, half the pixels
    smaller = tmp_path / "smaller.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(EXAMPLE_VIDEOS[0]), "-t", "3", "-vf", "scale=360:240",
                    "-an", str(smaller)], check=True)
    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], smaller)
    dialog.setWindowState(Qt.WindowNoState)
    dialog.resize(1000, 600)
    dialog.show()
    dialog.set_view_mode("slider")
    spin(1500)
    orig, comp = dialog.view_orig, dialog.view_comp

    def wheel(view, steps):
        for _ in range(abs(steps)):
            pos = QPointF(view.viewport().width() / 2, view.viewport().height() / 2)
            event = QWheelEvent(pos, view.mapToGlobal(pos.toPoint()), QPoint(), QPoint(0, 120 if steps > 0 else -120),
                                Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
            view.wheelEvent(event)

    def aligned():
        # the compressed picture covers exactly the original's, and its visible part starts at the dividing line
        overlay = dialog.overlay_item
        same = orig.video_item.sceneBoundingRect() == overlay.sceneBoundingRect()
        line = orig.mapFromScene(orig.clip_item.rect().topLeft()).x() + orig.viewport().x()
        return same and abs(line - dialog.stage.split_x()) <= 1

    assert aligned()
    wheel(orig, 4)   # zoom in …
    assert orig.zoom_factor > 1.5 and aligned()
    orig.horizontalScrollBar().setValue(orig.horizontalScrollBar().value() + 40)   # … pan …
    assert aligned()
    orig.rotate_by(90)   # … turn …
    assert aligned()
    wheel(orig, -8)   # … and zoom out below the starting size; the hidden view follows for side by side
    assert abs(orig.zoom_factor - comp.zoom_factor) < 1e-6 and orig.zoom_factor < 1 and aligned()
    dialog.reject()


def test_flip_mode_changes_between_original_and_result_every_two_seconds(qapp):
    from archive_helper.compressor.widgets import ORIGINAL_COLOR, RESULT_COLOR, CompareVideoDialog

    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], EXAMPLE_VIDEOS[1])
    dialog.setWindowState(Qt.WindowNoState)
    dialog.resize(1000, 600)
    dialog.show()
    spin(300)
    view, stage = dialog.view_orig, dialog.stage

    def visible_part_of_result():
        clip = view.clip_item.rect()
        shown = view.mapToScene(view.viewport().rect()).boundingRect()
        return max(0.0, min(clip.right(), shown.right()) - max(clip.left(), shown.left())) / shown.width()

    dialog.mode_buttons["flip"].click()
    assert dialog.mode_buttons["flip"].isChecked() and dialog.flip_timer.isActive()
    assert dialog.flip_timer.interval() == 2000
    assert dialog.player_comp.videoOutput() is dialog.overlay_item   # both keep playing: a flip is instant
    assert visible_part_of_result() == 0 and not stage.handle.isVisible()
    assert stage.tags[0].isVisible() and not stage.tags[1].isVisible()   # "Original"
    assert view.border_color == ORIGINAL_COLOR and ORIGINAL_COLOR.name() in stage.tags[0].styleSheet()   # blue

    dialog.on_flip()
    assert visible_part_of_result() == 1.0
    assert view.border_color == RESULT_COLOR and RESULT_COLOR.name() in stage.tags[1].styleSheet()   # red
    assert stage.tags[1].isVisible() and not stage.tags[0].isVisible() and stage.tags[1].x() == 10   # same corner
    dialog.pause()
    assert dialog.flip_timer.isActive()   # a still frame can be compared too
    dialog.on_flip()
    assert visible_part_of_result() == 0

    view.viewport().grab()   # the frame is drawn without errors
    dialog.mode_buttons["slider"].click()
    assert not dialog.flip_timer.isActive() and stage.handle.isVisible() and view.border_color is None
    dialog.reject()


def test_the_original_still_plays_when_the_result_cannot_be_loaded(qapp, tmp_path, monkeypatch):
    # Compare: waiting for both -> playing, also when one side ends as invalid instead of loaded
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QMessageBox
    from archive_helper.compressor.widgets import CompareVideoDialog

    broken = tmp_path / "broken_archived.mp4"
    broken.write_bytes(EXAMPLE_VIDEOS[0].read_bytes()[:200_000])
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *args: None))
    dialog = CompareVideoDialog(EXAMPLE_VIDEOS[0], broken)
    dialog.show()
    spin(3000)
    try:
        assert dialog.player_orig.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    finally:
        dialog.reject()
