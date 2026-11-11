"""Table model for the compressor queue: one row per source video."""
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSize, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate

from video_helper_core.i18n import tr
from .utils import format_size, is_blocky

LOG_LIMIT = 300

# Statuses in the order used when sorting by the status column.
STATUS_ORDER = ["running", "error", "planned", "done", "skipped", "exists"]

# Semantic colours; the pill background is the same hue at low alpha so it works in light and dark mode.
STATUS_COLORS = {
    "planned": QColor(133, 139, 149),
    "running": QColor(82, 130, 245),
    "done": QColor(46, 158, 108),
    "exists": QColor(46, 158, 108),
    "skipped": QColor(133, 139, 149),
    "error": QColor(226, 84, 72),
}

WARNING_COLOR = QColor(201, 130, 31)   # orange: finished, but worth a look
CONFLICT_COLOR = QColor(226, 84, 72)   # red: the result would replace the original

COL_THUMB, COL_FILE, COL_DURATION, COL_SIZE, COL_RESULT, COL_SETTINGS, COL_STATUS = range(7)


def status_label(row):
    if row.conflict and row.status in ("planned", "error"):
        return tr("⚠ Source = target!")
    label = _status_label(row)
    if row.blocky:
        label += " · " + tr("⚠ blocks?")
    if row.metadata_missing:
        label += " · " + tr("⚠ metadata?")
    return f"⚠ {tr('Duplicate target name')} · {label}" if row.duplicate else label


def _status_label(row):
    if row.status == "running":
        return tr("Running · {percent} %", percent=row.progress) + (f" · {row.speed}" if row.speed else "")
    if row.status == "planned" and row.moved_up:
        return tr("Planned · moved up")
    return {
        "planned": tr("Planned"),
        "done": tr("Done"),
        "exists": tr("Already archived"),
        "skipped": tr("Skipped"),
        "error": tr("Error"),
    }[row.status]


def describe_settings(settings, detailed=False):
    """Short summary of the stored compression settings, e.g. "CPU · CRF 20 · slow · 1920 px · 30 fps"."""
    if not settings:
        return "" if detailed else "–"
    if settings.get("encoder") == "libx265":
        parts = [tr("CPU"), f"CRF {settings.get('crf')}", str(settings.get('preset'))]
    else:
        parts = [tr("Mac GPU"), tr("quality {value}", value=settings.get('vt_quality'))]
    parts.append(f"{settings['max_res']} px" if settings.get("max_res") else tr("full resolution"))
    parts.append(f"{settings['max_fps']} fps" if settings.get("max_fps") else tr("original frame rate"))
    if settings.get("hdr"):
        parts.append("HDR")
    if settings.get("dry_run"):
        parts.append(tr("test run"))
    if not detailed:
        return " · ".join(parts)
    parts.append(tr("AAC copied") if settings.get("copy_aac") else tr("audio re-encoded"))
    lines = [" · ".join(parts)]
    source = settings.get("source") or {}
    if source.get("name"):
        about = [source["name"]]
        if source.get("size") is not None:
            about.append(format_size(source["size"]))
        if source.get("format"):
            about.append(source["format"])
        lines.append(tr("Made from: {source}", source=" · ".join(about)))
    if settings.get("compressed_at"):
        lines.append(tr("Compressed on {date} with version {version}", date=settings["compressed_at"][:16].replace("T", " "),
                        version=settings.get("app_version", "?")))
    if settings.get("capture_date_from") == "file date":
        lines.append(tr("The original had no capture date inside; the file date was written in."))
    elif settings.get("capture_date_from"):
        lines.append(tr("Capture date written in from {field}.", field=settings["capture_date_from"]))
    return "\n".join(lines)


def format_clock(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


@dataclass
class VideoRow:
    src: Path
    dst: Path
    size: int
    status: str = "planned"
    out_size: int | None = None
    duration: float | None = None
    progress: int = 0
    speed: str = ""
    note: str = ""
    duplicate: bool = False  # another source file maps to the same target name
    conflict: bool = False  # the target is a source video: the result would replace the original
    moved_up: bool = False  # processed before the table order ("Process next")
    block_check: tuple | None = None  # blockiness of (result, original) where it looked worst; original may be None
    settings: dict | None = None  # stored in the result file; None for results made before this existed
    root: Path | None = None
    log: list = field(default_factory=list)
    started_at: float | None = None  # time.monotonic() when its compression started, paused time left out
    took: float | None = None  # seconds its compression took in this session

    @property
    def folder(self):
        try:
            parent = self.src.parent.relative_to(self.root)
        except (TypeError, ValueError):  # no root, or the file lies outside it
            return ""
        return "" if parent == Path(".") else parent.as_posix()

    @property
    def blocky(self):
        return self.block_check is not None and is_blocky(*self.block_check)

    @property
    def metadata_missing(self):
        """Metadata of the original that did not reach the result (stored in the result's settings tag)."""
        return (self.settings or {}).get("metadata_missing") or [] if self.out_size is not None else []

    @property
    def has_result(self):
        return self.out_size is not None and self.dst.exists()

    @property
    def ratio(self):
        return self.out_size / self.size if self.out_size is not None and self.size else None


class VideoTableModel(QAbstractTableModel):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.by_path = {}
        self.thumbnails = {}
        self.paused_since = None   # time.monotonic() while the run is paused: running videos do not age

    # --- data management -------------------------------------------------
    def reset(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.by_path = {str(r.src): i for i, r in enumerate(rows)}
        self.endResetModel()
        self.changed.emit()

    def row_for(self, path):
        index = self.by_path.get(str(path))
        return None if index is None else self.rows[index]

    def update(self, path, **values):
        index = self.by_path.get(str(path))
        if index is None:
            return
        row = self.rows[index]
        for key, value in values.items():
            setattr(row, key, value)
        self.dataChanged.emit(self.index(index, 0), self.index(index, self.columnCount() - 1))
        self.changed.emit()

    def append_log(self, path, line):
        row = self.row_for(path)
        if row is not None:
            row.log.append(line)
            del row.log[:-LOG_LIMIT]

    def set_thumbnail(self, path, pixmap):
        index = self.by_path.get(str(path))
        if index is None or pixmap is None or pixmap.isNull():
            return
        self.thumbnails[str(path)] = pixmap
        self.dataChanged.emit(self.index(index, COL_THUMB), self.index(index, COL_THUMB))

    @staticmethod
    def sort_key(row, column):
        """Shared by the table sorting and the processing order."""
        return {
            COL_THUMB: path_sort_key(row),
            COL_FILE: path_sort_key(row),
            COL_DURATION: row.duration if row.duration is not None else -1.0,
            COL_SIZE: row.size,
            COL_RESULT: row.ratio if row.ratio is not None else 99.0,
            COL_SETTINGS: describe_settings(row.settings),
            COL_STATUS: STATUS_ORDER.index(row.status),
        }[column]

    def counts(self):
        result = {}
        for row in self.rows:
            result[row.status] = result.get(row.status, 0) + 1
        return result

    # --- Qt model API ----------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 7

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return ["", tr("File"), tr("Duration"), tr("Original"), tr("Result"), tr("Settings"), tr("Status")][section]
        if role == Qt.TextAlignmentRole and section in (COL_DURATION, COL_SIZE, COL_RESULT):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole and section == COL_THUMB:
            return tr("Drag the edge of this column to make the previews bigger.")
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return self.sort_key(row, col)
        if role == Qt.UserRole + 1:
            return row
        if role == Qt.DisplayRole:
            if col == COL_FILE:
                return row.src.name
            if col == COL_DURATION:
                return format_clock(row.duration) if row.duration else "–"
            if col == COL_SIZE:
                return format_size(row.size)
            if col == COL_RESULT:
                if row.ratio is None:
                    return "–"
                change = (row.ratio - 1) * 100
                return f"{format_size(row.out_size)}  ({change:+.0f} %)"
            if col == COL_SETTINGS:
                return describe_settings(row.settings) if row.out_size is not None else ""
            if col == COL_STATUS:
                return status_label(row)
        if role == Qt.TextAlignmentRole and col in (COL_DURATION, COL_SIZE, COL_RESULT):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and col == COL_RESULT and row.ratio is not None and row.ratio > 1:
            return WARNING_COLOR
        if role == Qt.ToolTipRole:
            if col == COL_FILE:
                return str(row.src)
            if col == COL_SETTINGS and row.out_size is not None:
                return describe_settings(row.settings, detailed=True) or tr("Made before settings were recorded.")
            if col == COL_STATUS:
                timing = self.timing_text(row)
                other = self.data(index, Qt.ToolTipRole + 1000)   # the tooltip without the times
                return "\n".join(text for text in (timing, other) if text) or None
        if role == Qt.ToolTipRole + 1000:
            warnings = []
            if col == COL_STATUS and row.blocky:
                result, original = row.block_check
                warnings.append(tr("Flat areas may show visible blocks: score {result:.1f}, original {original}. Compare it with "
                                   "the original; if it shows, the CPU encoder (libx265) or a higher quality helps.",
                                   result=result, original="–" if original is None else f"{original:.1f}"))
            if col == COL_STATUS and row.metadata_missing:
                shown = row.metadata_missing[:12]
                more = len(row.metadata_missing) - len(shown)
                warnings.append(tr("Metadata of the original that is not in the result (keep the original if you need it):")
                                + "\n" + "\n".join("  " + name for name in shown)
                                + ("\n  " + tr("… and {count} more", count=more) if more else ""))
            if warnings:
                return "\n\n".join(warnings)
            if row.note:
                return row.note
        return None

    def timing_text(self, row):
        """How long a running video has been compressing and about how long it still needs; how long a finished one took."""
        if row.status == "running" and row.started_at is not None:
            elapsed = max(0.0, (self.paused_since or time.monotonic()) - row.started_at)
            text = tr("Running for {time}", time=format_elapsed(elapsed))
            if 0 < row.progress < 100:
                left = elapsed * (100 - row.progress) / row.progress
                return text + " · " + tr("about {time} left", time=format_elapsed(left))
            return text + " · " + tr("estimating time left…")
        if row.status == "done" and row.took is not None:
            return tr("Took {time}", time=format_elapsed(row.took))
        return ""


class VideoFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.status_filter = None
        self.text_filter = ""
        self.setSortRole(Qt.UserRole)

    def set_status_filter(self, status):
        self.beginFilterChange()
        self.status_filter = status
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_text_filter(self, text):
        self.beginFilterChange()
        self.text_filter = text.lower().strip()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        # Row numbers count the rows as shown, which is also the order they are processed in.
        if orientation == Qt.Vertical:
            return section + 1 if role == Qt.DisplayRole else None
        return super().headerData(section, orientation, role)

    def lessThan(self, left, right):
        # Compare in Python: Qt's default compares by the *left* value's type, so a file
        # under 2 GB (int) against a larger one (qlonglong) truncated the larger size.
        model = self.sourceModel()
        column = left.column()
        return model.sort_key(model.rows[left.row()], column) < model.sort_key(model.rows[right.row()], column)

    def filterAcceptsRow(self, source_row, source_parent):
        row = self.sourceModel().rows[source_row]
        if self.status_filter and row.status not in self.status_filter:
            return False
        if self.text_filter:
            return self.text_filter in row.src.name.lower() or self.text_filter in row.folder.lower()
        return True


class ThumbnailDelegate(QStyledItemDelegate):
    """The preview picture, as big as its column allows (the column can be dragged wider, the rows grow with it)."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index):
        return QSize(64, 44)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole + 1)
        self.initStyleOption(option, index)
        option.text = ""
        option.icon = QIcon()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(option.rect.adjusted(6, 4, -2, -4))
        if area.width() <= 0 or area.height() <= 0:
            painter.restore()
            return
        pixmap = self.model.thumbnails.get(str(row.src))
        if pixmap is not None and not pixmap.isNull():
            # the whole picture, never cropped: portrait videos stay portrait
            scaled = pixmap.scaled(area.size().toSize(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            left = area.left() + (area.width() - scaled.width()) / 2
            top = area.top() + (area.height() - scaled.height()) / 2
            painter.drawPixmap(int(left), int(top), scaled)
        else:
            # a 16:9 placeholder; the palette's Mid nearly vanishes on dark rows
            height = min(area.height(), area.width() * 9 / 16)
            placeholder = QRectF(area.left(), area.center().y() - height / 2, area.width(), height)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(128, 128, 128, 90))
            painter.drawRoundedRect(placeholder, 4, 4)
        painter.restore()


class FileDelegate(QStyledItemDelegate):
    """File name and sub folder in one cell."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index):
        return QSize(200, 44)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole + 1)
        self.initStyleOption(option, index)
        option.text = ""
        option.icon = QIcon()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        palette = option.palette

        selected = bool(option.state & QStyle.State_Selected)
        # the group matters: the selection of an inactive window is pale, and its text colour is dark instead of white
        group = QPalette.Active if option.state & QStyle.State_Active else QPalette.Inactive
        text_color = palette.color(group, QPalette.HighlightedText if selected else QPalette.Text)
        # laid out for the normal row height and centred in taller rows (big previews)
        line_box = option.rect.adjusted(8, 0, -4, 0)
        if line_box.height() > 46:
            line_box.setTop(option.rect.top() + (option.rect.height() - 46) // 2)
            line_box.setHeight(46)
        name_font = QFont(option.font)
        name_font.setWeight(QFont.DemiBold)
        painter.setFont(name_font)
        painter.setPen(text_color)
        top = line_box.adjusted(0, 5, 0, 0)
        name = painter.fontMetrics().elidedText(row.src.name, Qt.ElideMiddle, top.width())
        folder = row.folder
        if folder:
            painter.drawText(top.adjusted(0, 0, 0, -line_box.height() // 2), Qt.AlignLeft | Qt.AlignVCenter, name)
            small = QFont(option.font)
            small.setPointSizeF(option.font.pointSizeF() * 0.88)
            painter.setFont(small)
            muted = QColor(text_color)
            muted.setAlpha(150)
            painter.setPen(muted)
            folder = painter.fontMetrics().elidedText(folder, Qt.ElideMiddle, top.width())
            painter.drawText(top.adjusted(0, line_box.height() // 2 - 5, 0, -5), Qt.AlignLeft | Qt.AlignVCenter, folder)
        else:
            painter.drawText(top.adjusted(0, 0, 0, -5), Qt.AlignLeft | Qt.AlignVCenter, name)
        painter.restore()


class StatusDelegate(QStyledItemDelegate):
    """Status as a coloured pill; running rows get a thin progress bar underneath."""

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole + 1)
        self.initStyleOption(option, index)
        text = option.text
        option.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        color = CONFLICT_COLOR if row.conflict and row.status in ("planned", "error") else STATUS_COLORS[row.status]
        if row.blocky or row.metadata_missing:
            color = WARNING_COLOR
        font = QFont(option.font)
        font.setPointSizeF(option.font.pointSizeF() * 0.92)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        # Generous padding: an exact fit gets elided through rounding.
        width = metrics.horizontalAdvance(text) + 36
        height = 22
        top = option.rect.center().y() - height / 2 - (3 if row.status == "running" else 0)
        pill = QRectF(option.rect.left() + 8, top, min(width, option.rect.width() - 16), height)
        if option.state & QStyle.State_Selected:
            background = option.palette.color(QPalette.Base)  # stays readable on the selection colour
        else:
            background = QColor(color)
            background.setAlpha(38)
        painter.setPen(Qt.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(pill.left() + 9, pill.center().y() - 3, 6, 6))
        painter.setPen(color)
        painter.drawText(pill.adjusted(20, 0, -6, 0), Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(text, Qt.ElideRight, int(pill.width()) - 24))
        if row.status == "running":
            track = QRectF(pill.left(), pill.bottom() + 4, pill.width(), 3)
            faint = QColor(color)
            faint.setAlpha(45)
            painter.setPen(Qt.NoPen)
            painter.setBrush(faint)
            painter.drawRoundedRect(track, 1.5, 1.5)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * row.progress / 100, 3), 1.5, 1.5)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(190, 44)


def path_sort_key(row):
    """Sub folder by sub folder, then the file name: the videos of a folder stay together, its own files before
    those of its sub folders (comparing whole path strings would put "2022 - Nizza" between "2022" and "2022/Festival")."""
    folder = row.folder if row.root is not None else row.src.parent.as_posix()
    return tuple(part.lower() for part in folder.split("/") if part), row.src.name.lower()


def format_elapsed(seconds):
    """Seconds as "42 s", "12 min 5 s" or, from an hour on, "1 h 20 min"."""
    seconds = int(round(seconds))
    if seconds < 60:
        return tr("{seconds} s", seconds=seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return tr("{minutes} min {seconds} s", minutes=minutes, seconds=seconds)
    hours, minutes = divmod(minutes, 60)
    return tr("{hours} h {minutes} min", hours=hours, minutes=minutes)
