"""Shows the metadata of an original and its result side by side: what stayed, changed, got lost or was added."""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from video_helper_core.i18n import tr
from .model import CONFLICT_COLOR, STATUS_COLORS, WARNING_COLOR
from .utils import expected_to_change, metadata_entries, parse_exif_date, side_tracks, without_track_number

# what a row says about a field; "same" and "moved" mean the value is in the result
SAME, MOVED, CHANGED, MISSING, ADDED, EXPECTED = "same", "moved", "changed", "missing", "added", "expected"
TRACKS_GROUP = "Tracks"
MAX_SHOWN = 400   # characters of a value in the table; the tooltip has all of it


def compared(group, tag, value=None):
    """Whether a difference in this field matters (missing_metadata reports it). The movie's date is checked
    although the rest of its group is technical, unless the original's is empty (then it is filled in)."""
    if (group, tag) == ("QuickTime", "CreateDate"):
        return value is None or parse_exif_date(value) is not None
    return not expected_to_change(group, tag)


def same_name(a, b):
    """Field names that differ only in a track number (see without_track_number)."""
    return a == b or without_track_number(a) == without_track_number(b)


def compare_entries(src_entries, dst_entries):
    """[(state, group, tag, original value, result value)] for two lists of (group, tag, value), in the original's
    order, fields only in the result after the others of their group. A value counts as kept in another group too
    (state MOVED; exiftool may file a copy elsewhere), as missing_metadata does."""
    pool = {}   # (group, tag) -> values of the result not yet matched
    for group, tag, value in dst_entries:
        pool.setdefault((group, tag), []).append(value)

    def take(group, tag, value=None):
        values = pool.get((group, tag)) or []
        if value is None:
            return values.pop(0) if values else None
        if value in values:
            values.remove(value)
            return value
        return None

    rows = []
    for group, tag, value in src_entries:
        if take(group, tag, value) is not None:
            rows.append((SAME, group, tag, value, value))
            continue
        other = take(group, tag)
        if other is not None:
            rows.append((CHANGED if compared(group, tag, value) else EXPECTED, group, tag, value, other))
            continue
        # also under another track number: the result's tracks can be in another order
        found = next(((g, t) for (g, t), values in pool.items() if same_name(t, tag) and value in values), None)
        if found is not None:
            elsewhere, _ = found
            take(*found, value)
            rows.append((MOVED, group, tag, value, f"{value}  ({elsewhere})"))
        elif not compared(group, tag, value) or not value.strip() or value.startswith("(Binary data 0 bytes"):
            rows.append((EXPECTED, group, tag, value, ""))   # an empty field holds nothing to lose
        else:
            rows.append((MISSING, group, tag, value, ""))
    for (group, tag), values in pool.items():
        for value in values:
            rows.append((ADDED if compared(group, tag) else EXPECTED, group, tag, "", value))
    order = {}
    for _, group, *_ in rows:
        order.setdefault(group, len(order))
    return sorted(rows, key=lambda row: order[row[1]])   # stable: within a group the order stays


def compare_tracks(src_tracks, dst_tracks):
    """Rows for the tracks that are neither picture nor sound (timecode, camera data per frame, subtitles)."""
    rows = []
    for tag in sorted(set(src_tracks) | set(dst_tracks)):
        before, after = src_tracks.count(tag), dst_tracks.count(tag)
        state = SAME if before == after else MISSING if after < before else ADDED
        rows.append((state, TRACKS_GROUP, tag, str(before) if before else "", str(after) if after else ""))
    return rows


def read_comparison(src_path, dst_path, embedded=False):
    """The rows for two files; either may be None (then its column stays empty). Boxes exiftool has no name for
    are listed too (Unknown_xxxx); embedded adds the data per frame."""
    src = metadata_entries(src_path, unknown=True, embedded=embedded) if src_path else []
    dst = metadata_entries(dst_path, unknown=True, embedded=embedded) if dst_path else []
    if src_path and dst_path:
        return compare_entries(src, dst) + compare_tracks(side_tracks(src_path), side_tracks(dst_path))
    if src_path:
        return [(SAME, g, t, v, "") for g, t, v in src] + compare_tracks(side_tracks(src_path), [])
    return [(SAME, g, t, "", v) for g, t, v in dst] + compare_tracks([], side_tracks(dst_path))


STATES = (SAME, MOVED, CHANGED, MISSING, ADDED, EXPECTED)


def state_texts():
    """state -> (word for the status column, explanation for its tooltip), in the current language."""
    return {
        SAME: ("", ""),
        MOVED: (tr("elsewhere"), tr("Same value, filed under another group in the result.")),
        CHANGED: (tr("changed"), tr("The result has another value.")),
        MISSING: (tr("missing"), tr("Not in the result.")),
        ADDED: (tr("new"), tr("Only in the result.")),
        EXPECTED: (tr("expected"), tr("Changed by compressing (container, codec, sizes, our settings): no loss.")),
    }


class MetadataDialog(QDialog):
    """Original and result side by side. Reads in the background: exiftool takes a moment on large files."""
    loaded = Signal(int, object)   # the reading it answers, rows or an error text

    def __init__(self, src_path, dst_path, parent=None):
        super().__init__(parent)
        self.src_path = Path(src_path) if src_path else None
        self.dst_path = Path(dst_path) if dst_path else None
        self.rows = []
        self.reading = 0   # counts the readings: only the answer to the last one is shown
        name = (self.src_path or self.dst_path).name
        self.setWindowTitle(tr("Metadata: {name}", name=name))
        self.resize(1100, 700)
        layout = QVBoxLayout(self)

        files = QLabel("\n".join(
            [tr("Original: {path}", path=self.src_path or "–"), tr("Result: {path}", path=self.dst_path or "–")]))
        files.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(files)
        self.summary = QLabel(tr("Reading metadata…"))
        layout.addWidget(self.summary)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search fields and values…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        bar.addWidget(self.search, 1)
        self.only_differences = QCheckBox(tr("Only differences"))
        self.only_differences.toggled.connect(self.apply_filter)
        bar.addWidget(self.only_differences)
        self.show_expected = QCheckBox(tr("Show expected changes"))
        self.show_expected.toggled.connect(self.apply_filter)
        bar.addWidget(self.show_expected)
        self.embedded = QCheckBox(tr("Data per frame"))
        self.embedded.setToolTip(tr("Also reads the data stored with every frame (GPS tracks, camera data per frame). "
                                    "Takes a while on long videos."))
        self.embedded.toggled.connect(self.start_reading)
        bar.addWidget(self.embedded)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([tr("Status"), tr("Group"), tr("Field"), tr("Original"), tr("Result")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        for column in range(3):   # sized once over all rows (see show_rows): hidden rows would not count
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.loaded.connect(self.on_loaded)
        self.start_reading()

    def start_reading(self):
        self.reading += 1
        self.summary.setText(tr("Reading metadata…"))
        threading.Thread(target=self.read, args=(self.reading, self.embedded.isChecked()), daemon=True).start()

    def read(self, reading, embedded):
        try:
            result = read_comparison(self.src_path, self.dst_path, embedded)
        except Exception as e:   # exiftool missing, unreadable file: shown in the dialog
            result = f"{type(e).__name__}: {e}"
        try:
            self.loaded.emit(reading, result)
        except RuntimeError:
            pass   # the dialog was closed meanwhile

    def on_loaded(self, reading, result):
        if reading != self.reading:
            return   # an earlier reading, replaced by a newer one
        if isinstance(result, str):
            self.summary.setText(tr("Metadata could not be read: {error}", error=result))
            return
        self.show_rows(result)

    def show_rows(self, rows):
        self.rows = rows
        counts = {state: sum(row[0] == state for row in rows) for state in STATES}
        if self.src_path and self.dst_path:
            self.summary.setText(tr(
                "{same} the same · {missing} missing · {changed} changed · {added} new · {expected} changed by "
                "compressing", same=counts[SAME] + counts[MOVED], missing=counts[MISSING], changed=counts[CHANGED],
                added=counts[ADDED], expected=counts[EXPECTED]))
            if self.reading == 1:   # the first look; later readings keep what the user chose
                self.only_differences.setChecked(bool(counts[MISSING] or counts[CHANGED]))
        else:
            self.summary.setText(tr("{count} fields", count=len(rows)))
            self.only_differences.setEnabled(False)
            self.show_expected.setEnabled(False)
        colors = {MISSING: CONFLICT_COLOR, CHANGED: WARNING_COLOR, ADDED: STATUS_COLORS["done"],
                  MOVED: STATUS_COLORS["done"]}
        texts_of = state_texts()
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for index, (state, group, tag, before, after) in enumerate(rows):
            word, hint = texts_of[state]
            texts = [word, group, tag, before, after]
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text if len(text) <= MAX_SHOWN else text[:MAX_SHOWN] + "…")
                if column >= 3 and text:
                    item.setToolTip(text[:5000])
                elif column == 0 and hint:
                    item.setToolTip(hint)
                if state in colors and column in (0, 3, 4):
                    item.setForeground(QBrush(colors[state]))
                elif state == EXPECTED:
                    item.setForeground(QBrush(STATUS_COLORS["planned"]))
                self.table.setItem(index, column, item)
        for column in range(3):
            self.table.resizeColumnToContents(column)
        self.table.setUpdatesEnabled(True)
        self.apply_filter()

    def apply_filter(self):
        needle = self.search.text().strip().lower()
        differences = self.only_differences.isChecked() and self.only_differences.isEnabled()
        expected = self.show_expected.isChecked() or not self.show_expected.isEnabled()
        for index, (state, *texts) in enumerate(self.rows):
            hidden = (differences and state in (SAME, MOVED)) or (not expected and state == EXPECTED) \
                or (needle and not any(needle in text.lower() for text in texts))
            self.table.setRowHidden(index, bool(hidden))
