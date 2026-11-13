from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from archive_helper.compressor.model import (
    COL_DURATION, COL_FILE, COL_RESULT, COL_SETTINGS, COL_SIZE, COL_STATUS, COL_THUMB,
    VideoFilterProxy, VideoRow, VideoTableModel, describe_settings, format_clock, status_label,
)


def row(name, size=100, status="planned", **kwargs):
    return VideoRow(src=Path("/src") / name, dst=Path("/dst") / name, size=size, status=status, root=Path("/src"), **kwargs)


# Rows are keyed by str(path), which has the system's separators (backslashes on Windows).
SRC_B, SRC_A = str(Path("/src/b.mp4")), str(Path("/src/A.mp4"))


@pytest.fixture
def model(qapp):
    m = VideoTableModel()
    m.reset([row("b.mp4", 200, duration=65), row("A.mp4", 50, "done", out_size=25, duration=3700),
             row("sub/c.mp4", 300, "error")])
    return m


def test_format_clock():
    assert format_clock(5) == "0:05" and format_clock(65) == "1:05" and format_clock(3725) == "1:02:05"


@pytest.mark.parametrize("settings,expected", [
    (None, "–"),
    ({"encoder": "libx265", "crf": 20, "preset": "slow", "max_res": 1920, "max_fps": 30},
     "CPU · CRF 20 · slow · 1920 px · 30 fps"),
    ({"encoder": "videotoolbox", "vt_quality": 60}, "Mac-GPU · Qualität 60 · volle Auflösung · Original-Bildrate"),
])
def test_describe_settings(settings, expected):
    from video_helper_core import i18n
    i18n.set_language("de")  # German is the default; reset by the autouse fixture
    assert describe_settings(settings) == expected


def test_describe_settings_details():
    settings = {"encoder": "libx265", "crf": 20, "preset": "slow", "hdr": True, "dry_run": True, "copy_aac": True}
    text = describe_settings(settings)
    assert "HDR" in text
    assert describe_settings(settings, detailed=True) != text
    assert describe_settings(None, detailed=True) == ""


def test_status_labels(qapp):
    assert "42" in status_label(row("a.mp4", status="running", progress=42, speed="2.0x"))
    assert "2.0x" in status_label(row("a.mp4", status="running", progress=42, speed="2.0x"))
    assert status_label(row("a.mp4", moved_up=True)) != status_label(row("a.mp4"))
    assert status_label(row("a.mp4", duplicate=True)).startswith("⚠")
    for status in ("planned", "done", "exists", "skipped", "error"):
        assert status_label(row("a.mp4", status=status))


def test_video_row_properties(tmp_path):
    existing = tmp_path / "out.mp4"
    existing.write_bytes(b"1")
    r = VideoRow(src=Path("/src/sub/x.mp4"), dst=existing, size=200, out_size=50, root=Path("/src"))
    assert r.folder == "sub" and r.has_result and r.ratio == 0.25
    assert VideoRow(src=Path("/src/x.mp4"), dst=existing, size=1, root=Path("/src")).folder == ""
    assert VideoRow(src=Path("/elsewhere/x.mp4"), dst=existing, size=1, root=Path("/src")).folder == ""
    assert not VideoRow(src=Path("x"), dst=tmp_path / "missing.mp4", size=1, out_size=1).has_result
    assert VideoRow(src=Path("x"), dst=existing, size=0, out_size=1).ratio is None


def test_model_display_data(model):
    def text(r, c):
        return model.data(model.index(r, c))

    assert text(0, COL_FILE) == "b.mp4" and text(0, COL_DURATION) == "1:05" and text(2, COL_DURATION) == "–"
    assert text(1, COL_RESULT).endswith("(-50 %)") and text(0, COL_RESULT) == "–"
    assert text(0, COL_SETTINGS) == "" and text(1, COL_SETTINGS) == "–"
    assert model.data(model.index(0, COL_FILE), Qt.ToolTipRole) == SRC_B
    assert model.data(model.index(0, COL_SIZE), Qt.TextAlignmentRole) is not None
    assert model.data(model.index(0, COL_FILE), Qt.TextAlignmentRole) is None
    assert model.data(model.index(99, 0)) is None
    for column in range(1, model.columnCount()):   # the preview column has no title, only a tooltip
        assert model.headerData(column, Qt.Horizontal)
    assert model.headerData(0, Qt.Horizontal, Qt.ToolTipRole)
    assert model.rowCount() == 3


def test_larger_results_are_highlighted(qapp):
    m = VideoTableModel()
    m.reset([row("big.mp4", 10, "done", out_size=20)])
    assert m.data(m.index(0, COL_RESULT), Qt.ForegroundRole) is not None
    assert m.data(m.index(0, COL_FILE), Qt.ForegroundRole) is None


def test_update_log_and_counts(model):
    changes = []
    model.changed.connect(lambda: changes.append(1))
    model.update(SRC_B, status="running", progress=10)
    assert model.row_for(SRC_B).progress == 10 and changes
    model.update("/nope", status="done")  # unknown paths are ignored
    for number in range(500):
        model.append_log(SRC_B, f"line {number}")
    assert 0 < len(model.row_for(SRC_B).log) < 500 and model.row_for(SRC_B).log[-1] == "line 499"
    model.append_log("/nope", "ignored")
    assert model.counts() == {"running": 1, "done": 1, "error": 1}


def test_thumbnails_are_stored_only_when_valid(model):
    from PySide6.QtGui import QPixmap

    model.set_thumbnail(SRC_B, None)
    model.set_thumbnail(SRC_B, QPixmap())
    model.set_thumbnail("/nope", QPixmap(4, 4))
    assert model.thumbnails == {}
    model.set_thumbnail(SRC_B, QPixmap(4, 4))
    assert SRC_B in model.thumbnails


def test_sorting_by_file_keeps_folders_together():
    from pathlib import Path

    root = Path("/fotos")
    paths = ["2022 - Nizza/b.mov", "2022/Festival/a.mov", "2022/z.mov", "2021/c.mov", "2022/Festival/C.mov", "x.mov"]
    model = VideoTableModel()
    model.reset([VideoRow(src=root / p, dst=root / "out" / p, size=1, root=root) for p in paths])
    proxy = VideoFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(COL_FILE, Qt.AscendingOrder)
    shown = [model.rows[proxy.mapToSource(proxy.index(r, 0)).row()].src.relative_to(root).as_posix()
             for r in range(proxy.rowCount())]
    assert shown == ["x.mov", "2021/c.mov", "2022/z.mov", "2022/Festival/a.mov", "2022/Festival/C.mov",
                     "2022 - Nizza/b.mov"]


@pytest.mark.parametrize("column,expected", [
    (COL_FILE, ["A.mp4", "b.mp4", "c.mp4"]),
    (COL_SIZE, ["A.mp4", "b.mp4", "c.mp4"]),
    (COL_DURATION, ["c.mp4", "b.mp4", "A.mp4"]),
    (COL_STATUS, None),
    (COL_RESULT, ["A.mp4", "b.mp4", "c.mp4"]),
])
def test_proxy_sorting(model, column, expected):
    proxy = VideoFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(column, Qt.AscendingOrder)
    names = [proxy.index(r, COL_FILE).data() for r in range(proxy.rowCount())]
    if expected:
        assert names == expected
    assert sorted(names) == sorted(["b.mp4", "A.mp4", "c.mp4"])
    assert model.sort_key(model.rows[0], COL_SETTINGS) == "–"


def test_proxy_filters_by_status_and_text(model):
    proxy = VideoFilterProxy()
    proxy.setSourceModel(model)
    proxy.set_status_filter({"done", "error"})
    assert proxy.rowCount() == 2
    proxy.set_text_filter("  SUB ")  # matches the folder, case-insensitive
    assert proxy.rowCount() == 1 and proxy.index(0, COL_FILE).data() == "c.mp4"
    proxy.set_status_filter(None)
    proxy.set_text_filter("a.mp")
    assert proxy.rowCount() == 1
    proxy.set_text_filter("")
    assert proxy.rowCount() == 3


def test_delegates_paint_and_size_every_row(qapp, model):
    from PySide6.QtGui import QPixmap, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    from archive_helper.compressor.model import COL_THUMB, FileDelegate, StatusDelegate, ThumbnailDelegate

    model.update(SRC_B, status="running", progress=50, speed="1.5x", note="hello")
    model.rows[0].duplicate = True
    model.set_thumbnail(SRC_A, QPixmap(32, 32))
    canvas = QPixmap(600, 100)
    painter = QPainter(canvas)
    delegates = {COL_THUMB: ThumbnailDelegate(model), COL_FILE: FileDelegate(model), COL_STATUS: StatusDelegate()}
    for column, delegate in delegates.items():
        for r in range(model.rowCount()):
            option = QStyleOptionViewItem()
            option.rect = canvas.rect()
            index = model.index(r, column)
            assert delegate.sizeHint(option, index).height() > 0
            delegate.paint(painter, option, index)
    painter.end()


def test_sorting_by_status_puts_finished_videos_with_problems_together(qapp):
    from archive_helper.compressor.model import finished_problems

    lost = {"metadata_missing": ["Keys:Cinematic-video"]}
    rows = [row("a.mp4", status="exists", out_size=50), row("b.mp4", status="done", out_size=50, settings=lost),
            row("c.mp4", status="planned"), row("d.mp4", status="exists", out_size=50, settings=lost),
            row("e.mp4", status="done", out_size=150), row("f.mp4", status="error"), row("g.mp4", status="done", out_size=50)]
    model = VideoTableModel()
    model.reset(rows)
    proxy = VideoFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(COL_STATUS, Qt.AscendingOrder)
    names = [proxy.index(r, COL_FILE).data() for r in range(proxy.rowCount())]
    # errors, then the finished ones worth a look grouped by problem (this session's or not), then the rest
    assert names == ["f.mp4", "e.mp4", "b.mp4", "d.mp4", "c.mp4", "g.mp4", "a.mp4"]
    assert finished_problems(rows[4]) == ["larger"] and finished_problems(rows[2]) == []


def test_sorting_by_preview_goes_by_brightness_and_counts_the_loaded_ones(qapp):
    from PySide6.QtGui import QColor, QPixmap

    def picture(gray):
        pixmap = QPixmap(8, 8)
        pixmap.fill(QColor(gray, gray, gray))
        return pixmap

    model = VideoTableModel()
    model.reset([row("a.mp4"), row("b.mp4"), row("c.mp4"), row("d.mp4")])
    model.set_thumbnail("/src/a.mp4", picture(200))
    model.set_thumbnail("/src/b.mp4", None)           # could not be made
    model.set_thumbnail("/src/c.mp4", picture(20))
    proxy = VideoFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(COL_THUMB, Qt.AscendingOrder)
    assert [proxy.index(r, COL_FILE).data() for r in range(4)][:2] == ["c.mp4", "a.mp4"]   # dark first, then the rest
    assert [r.thumbnail_tried for r in model.rows] == [True, True, True, False]
    assert model.rows[2].brightness == 20


def test_a_tag_that_only_records_added_metadata_is_no_settings():
    # adding metadata to an old result stored the source but no settings: shown as unknown until the video is read
    record = {"metadata_added_at": "2026-11-11T14:46:37+01:00", "source": {"name": "IMG_1.MOV"}}
    assert describe_settings(record) == "–"
    read = {**record, "read_from_file": True, "codec": "hevc", "encoder": "hevc_videotoolbox", "max_res": 3840,
            "max_fps": 59.94, "hdr": True}
    assert describe_settings(read).startswith("≈ ") and "3840 px · 59.94 fps · HDR" in describe_settings(read)
    assert "IMG_1.MOV" in describe_settings(read, detailed=True)
