"""The metadata viewer: original and result side by side."""
import subprocess
import time

from conftest import requires_exiftool, requires_ffmpeg
from test_video_output import compress, make_clip


def test_fields_are_sorted_into_same_changed_missing_new_and_expected():
    from archive_helper.compressor.metadata_view import (
        ADDED, CHANGED, EXPECTED, MISSING, MOVED, SAME, compare_entries)

    src = [("Keys", "Make", "Apple"), ("Keys", "Model", "iPhone 15 Pro"), ("Keys", "Cinematic-video", "(Binary data 208 bytes)"),
           ("UserData", "Software", "26.4"), ("Track1", "ImageWidth", "1920"), ("XMP-dc", "Title", ""),
           ("ItemList", "Author", "Fabian"), ("Track2UserData", "Unknown_Track2tagc", "com.apple.quicktime.video-map")]
    dst = [("Track1", "ImageWidth", "1280"), ("Keys", "Make", "Apple"), ("Keys", "Model", "iPhone 16"),
           ("UserData", "Software", "26.4"), ("Keys", "Author", "Fabian"), ("XMP-vht", "CompressionSettings", "{}"),
           ("Keys", "Rating", "5"), ("Track3UserData", "Unknown_Track3tagc", "com.apple.quicktime.video-map")]

    rows = compare_entries(src, dst)

    assert [(state, f"{group}:{tag}") for state, group, tag, _, _ in rows] == [
        (SAME, "Keys:Make"), (CHANGED, "Keys:Model"), (MISSING, "Keys:Cinematic-video"),
        (ADDED, "Keys:Rating"),                             # only in the result: after the others of its group
        (SAME, "UserData:Software"),
        (EXPECTED, "Track1:ImageWidth"),                    # the size changes by compressing
        (EXPECTED, "XMP-dc:Title"),                         # empty: nothing to lose
        (MOVED, "ItemList:Author"),                         # the same value in another group
        (MOVED, "Track2UserData:Unknown_Track2tagc"),       # the same box of a track that has another number now
        (EXPECTED, "XMP-vht:CompressionSettings"),          # our own settings tag
    ]
    assert rows[1][3:] == ("iPhone 15 Pro", "iPhone 16")
    assert rows[7][4] == "Fabian  (Keys)"


def test_tracks_count_per_kind():
    from archive_helper.compressor.metadata_view import ADDED, MISSING, SAME, compare_tracks

    assert compare_tracks(["mebx", "mebx", "tmcd"], ["mebx", "gpmd", "tmcd"]) == [
        (ADDED, "Tracks", "gpmd", "", "1"), (MISSING, "Tracks", "mebx", "2", "1"), (SAME, "Tracks", "tmcd", "1", "1")]


@requires_ffmpeg
@requires_exiftool
def test_the_viewer_shows_a_lost_field_of_a_result(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication
    from archive_helper.compressor.metadata_view import MetadataDialog

    src = tmp_path / "src"
    src.mkdir()
    clip = make_clip(src / "IMG_1.MOV", size="320x240", seconds=1)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Keys:Make=Apple", "-Keys:Model=iPhone 15 Pro", str(clip)],
                   check=True)
    out, _, _ = compress(qapp, src, "IMG_1.MOV")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Keys:Model=", str(out)], check=True)   # as if lost

    dialog = MetadataDialog(clip, out)
    for _ in range(600):   # reads in the background
        QCoreApplication.processEvents()
        if dialog.rows:
            break
        time.sleep(0.01)
    assert dialog.rows

    def visible():
        return [row for i, row in enumerate(dialog.rows) if not dialog.table.isRowHidden(i)]

    # something is missing: the dialog opens on the differences only; the capture date the original lacked was
    # filled in (new), the container's own fields are hidden as expected
    assert dialog.only_differences.isChecked()
    assert [row[:3] for row in visible()] == [("missing", "Keys", "Model"), ("added", "Keys", "CreationDate")]
    assert ("missing", "Keys", "Model", "iPhone 15 Pro", "") in visible()
    assert not any(row[2] == "Make" for row in visible())
    dialog.only_differences.setChecked(False)
    assert ("same", "Keys", "Make", "Apple", "Apple") in visible()
    dialog.search.setText("make")
    assert [row[2] for row in visible()] == ["Make"]
    dialog.close()


@requires_ffmpeg
@requires_exiftool
def test_without_a_result_the_viewer_shows_the_original(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication
    from archive_helper.compressor.metadata_view import MetadataDialog

    clip = make_clip(tmp_path / "IMG_2.MOV", size="320x240", seconds=1)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Keys:Make=Apple", str(clip)], check=True)
    dialog = MetadataDialog(clip, None)
    for _ in range(600):
        QCoreApplication.processEvents()
        time.sleep(0.01)
        if dialog.rows:
            break
    assert ("same", "Keys", "Make", "Apple", "") in dialog.rows
    assert not dialog.only_differences.isEnabled()
    dialog.close()


def test_only_the_answer_to_the_latest_reading_is_shown(qapp, tmp_path, monkeypatch):
    # reading -> reading again (per-frame data switched on): an earlier, slower answer must not replace the newer one
    from archive_helper.compressor import metadata_view

    monkeypatch.setattr(metadata_view.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: None})())
    dialog = metadata_view.MetadataDialog(tmp_path / "a.mp4", tmp_path / "b.mp4")
    dialog.embedded.setChecked(True)   # second reading
    newer = [("same", "Keys", "Make", "Apple", "Apple")]
    dialog.on_loaded(2, newer)
    dialog.on_loaded(1, [("missing", "Keys", "Model", "x", "")])
    assert dialog.rows == newer
    dialog.on_loaded(2, "OSError: gone")
    assert "gone" in dialog.summary.text()
    dialog.close()
