import csv
import json
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Slot
from PySide6.QtGui import QFontDatabase, QIntValidator, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSlider, QStyle, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from video_helper_core.style import STYLE, caption
from video_helper_core.i18n import tr
from video_helper_core.paths import settings_file
from video_helper_core.trash import move_to_trash
from .model import (
    COL_DURATION, COL_FILE, COL_RESULT, COL_SETTINGS, COL_SIZE, COL_STATUS, COL_THUMB,
    FileDelegate, StatusDelegate, ThumbnailDelegate, VideoFilterProxy, VideoRow, VideoTableModel,
)
from video_helper_core.dialogs import confirm_after_delay
from video_helper_core.keep_awake import KeepAwake
from video_helper_core.notify import notify
from .utils import (
    DEFAULT_EXCLUDES, DEFAULT_POSTFIX, EncodeSpeeds, check_dependencies, clean_postfix, clear_caches, collect_videos, common_parent, format_duration_long, format_size, open_in_finder, parse_excludes,
    prune_thumbnails,
)
from .lab import CompressionLabDialog
from .options import FRAME_RATES, QUALITY_PRESETS, RENDERERS, RESOLUTIONS, X265_PRESETS
from .widgets import CompareVideoDialog, DropLineEdit, DropOverlay, dropped_paths
from .workers import ArchiveWorker, BlockCheckWorker, DurationProbeWorker, ResultSettingsWorker, ThumbnailLoader, UnifiedScanWorker

STATUS_FILTERS = [
    ("all", None), ("planned", {"planned"}), ("running", {"running"}),
    ("done", {"done", "exists"}), ("skipped", {"skipped"}), ("error", {"error"}),
]


def describe_crf(value):
    """What a libx265 CRF value means for archives (lower = better quality, larger files)."""
    if value <= 17:
        return tr("Practically lossless, very large files.")
    if value <= 19:
        return tr("Visually lossless.")
    if value <= 23:
        return tr("The sweet spot for archives.")
    if value <= 28:
        return tr("Smaller files, slight visible loss.")
    return tr("Clearly visible loss.")


def describe_vt(value):
    """What a VideoToolbox quality value means (higher = better quality, larger files)."""
    if value < 45:
        return tr("Small files, visible loss.")
    if value < 60:
        return tr("Good balance of size and quality.")
    if value < 80:
        return tr("Very high quality, almost visually lossless.")
    return tr("Visually lossless; files can get very large.")


def nearest_existing(path):
    path = Path(path).expanduser()
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return candidate
    return None


class ResizeWatcher(QObject):
    """Calls a function whenever the watched widget changes its size."""

    def __init__(self, widget, callback):
        super().__init__(widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self.callback()
        return False


class JobsBox(QComboBox):
    """Common values to pick from, or type any number."""
    CHOICES = [1, 2, 3, 4, 6, 8]

    def __init__(self):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.addItems([str(c) for c in self.CHOICES])
        self.setValidator(QIntValidator(1, 64, self))
        self.setValue(2)

    def value(self):
        try:
            return max(1, min(64, int(self.currentText())))
        except ValueError:
            return 1

    def setValue(self, value):
        self.setCurrentText(str(int(value)))


class LogDialog(QDialog):
    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.path = str(row.src)
        self.setWindowTitle(tr("Log: {name}", name=row.src.name))
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.text.setPlainText("\n".join(row.log) or tr("No log output yet."))
        layout.addWidget(self.text)

    def append(self, line):
        self.text.appendPlainText(line)


class ArchiverGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("Compress & Archive"))
        self.setStyleSheet(STYLE)
        self.resize(1200, 820)

        self.worker = None
        self.scan_worker = None
        self.duration_worker = None
        self.settings_worker = None
        self.block_worker = None
        self.block_checks = {}  # source path -> result of the check; rows are rebuilt by every scan
        self.log_dialog = None
        self.video_data_list = []
        self.scan_items = []
        self.moved_up = []
        self.total_src_bytes = 0
        self.total_dst_bytes = 0
        self.run_started = None
        self.run_bytes = 0
        self.run_paths = set()
        self.rescan_pending = False
        self.selection = None          # hand-picked videos (dropped files) instead of a source folder
        self.folder_source_text = ""   # the source folder to return to
        self.folder_target_text = ""   # and the target folder
        self.skipped_drops = 0
        self.setAcceptDrops(True)

        self.model = VideoTableModel(self)
        # Background signals go to methods of this widget, never to lambdas (see ThumbnailRunnable).
        self.thumbnails = ThumbnailLoader(self)
        self.thumbnails.ready.connect(self.on_thumbnail_ready)
        self.proxy = VideoFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.scan_timer = QTimer(self, singleShot=True, interval=400)
        self.scan_timer.timeout.connect(self.auto_scan)
        self.summary_timer = QTimer(self, singleShot=True, interval=200)
        self.summary_timer.timeout.connect(self.refresh_summary)
        # Durations arrive in the background, also during a run: the waiting videos are put in order again when they do.
        self.requeue_timer = QTimer(self, singleShot=True, interval=300)
        self.requeue_timer.timeout.connect(self.reorder_queue)
        self.speeds = EncodeSpeeds()   # how long encodes took before: the basis of the time estimate
        self.file_started = {}
        self.run_settings, self.run_jobs, self.run_paused = None, 1, False
        self.keep_awake = KeepAwake(reason="Videos are being compressed")
        self.notifier = notify   # replaced in tests
        self.stopped_by_user = False
        self.paused_at = None
        self.eta_timer = QTimer(self, interval=1000)
        self.eta_timer.timeout.connect(self.refresh_summary)

        self.init_ui()
        self.model.changed.connect(self.request_summary)
        self.load_defaults()
        self.refresh_summary()
        self.check_system_dependencies()
        prune_thumbnails()

    # ------------------------------------------------------------------ layout
    FIXED_COLUMNS = {COL_DURATION: 80, COL_SIZE: 100, COL_RESULT: 150, COL_STATUS: 160}
    SETTINGS_COLUMN_WIDTH, MIN_SETTINGS_COLUMN_WIDTH, MIN_FILE_COLUMN_WIDTH, SMALLEST_FILE_COLUMN_WIDTH = 320, 100, 200, 120

    def fit_columns(self):
        """Columns can be dragged. Until the user drags one, the file name gets the room that is left; the
        settings column takes what remains of that and disappears when it is too little (it comes back when the
        window gets wider or the settings drawer is closed)."""
        header = self.table.horizontalHeader()
        self.fitting_columns = True
        try:
            # The last column stretches to the edge, so its own width says nothing about what it needs.
            others = sum(header.sectionSize(c) if c in self.dragged_columns or c != COL_STATUS else self.FIXED_COLUMNS[c]
                         for c in self.FIXED_COLUMNS) + header.sectionSize(COL_THUMB)
            width = self.table.viewport().width()
            spare = width - others - self.MIN_FILE_COLUMN_WIDTH
            if COL_SETTINGS in self.dragged_columns:
                header.showSection(COL_SETTINGS)
            elif spare < self.MIN_SETTINGS_COLUMN_WIDTH:
                header.hideSection(COL_SETTINGS)
            else:
                header.showSection(COL_SETTINGS)
                header.resizeSection(COL_SETTINGS, min(self.SETTINGS_COLUMN_WIDTH, spare))
            if COL_FILE not in self.dragged_columns:
                settings = 0 if header.isSectionHidden(COL_SETTINGS) else header.sectionSize(COL_SETTINGS)
                header.resizeSection(COL_FILE, max(self.SMALLEST_FILE_COLUMN_WIDTH, width - others - settings))
        finally:
            self.fitting_columns = False

    # The preview column can be dragged wider; the rows grow with it (16:9 pictures). The width is kept for next time.
    THUMB_WIDTH, MIN_THUMB_WIDTH, MAX_THUMB_WIDTH, ROW_HEIGHT = 64, 40, 320, 46

    def on_thumb_column_resized(self, column, old_size, new_size):
        if column != COL_THUMB:
            return
        width = min(max(new_size, self.MIN_THUMB_WIDTH), self.MAX_THUMB_WIDTH)
        if width != new_size:
            self.table.horizontalHeader().resizeSection(COL_THUMB, width)   # comes back here with the clamped width
            return
        self.table.verticalHeader().setDefaultSectionSize(max(self.ROW_HEIGHT, round((width - 8) * 9 / 16) + 8))
        if not self.fitting_columns:
            self.view_save_timer.start()
            self.fit_columns()   # the file name gives up (or gets back) the room

    def save_view(self):
        try:
            path = settings_file()
            settings = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            settings['compressor_view'] = {'thumb_width': self.table.horizontalHeader().sectionSize(COL_THUMB)}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(settings, indent=4), encoding='utf-8')
        except (OSError, json.JSONDecodeError) as e:
            print(f"Failed to save the view: {e}")

    def saved_thumb_width(self):
        try:
            path = settings_file()
            view = json.loads(path.read_text(encoding='utf-8')).get('compressor_view', {}) if path.exists() else {}
            return int(view.get('thumb_width', self.THUMB_WIDTH))
        except (OSError, ValueError, TypeError, AttributeError):
            return self.THUMB_WIDTH

    def remember_dragged_column(self, column, old_size, new_size):
        if not self.fitting_columns and not self.table.horizontalHeader().isSectionHidden(column):
            self.dragged_columns.add(column)

    def init_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        main = QVBoxLayout()
        main.setContentsMargins(12, 4, 12, 12)
        main.setSpacing(10)
        main.addLayout(self.build_header())
        main.addWidget(self.build_summary())
        main.addWidget(self.build_selection_bar())
        main.addLayout(self.build_toolbar())
        main.addWidget(self.build_table(), stretch=1)
        self.detail_bar = QFrame(objectName="detailBar")
        self.detail_layout = QHBoxLayout(self.detail_bar)
        self.detail_layout.setContentsMargins(12, 8, 12, 8)
        self.detail_bar.hide()
        main.addWidget(self.detail_bar)
        root.addLayout(main, stretch=1)

        self.drawer = self.build_drawer()
        root.addWidget(self.drawer)

        for signal in (self.combo_renderer.currentIndexChanged, self.combo_preset.currentTextChanged,
                       self.combo_res.currentIndexChanged, self.combo_fps.currentIndexChanged,
                       self.spin_jobs.currentTextChanged, self.cb_dry_run.toggled):
            signal.connect(self.request_summary)   # the time estimate depends on them
        self.drop_overlay = DropOverlay(self, self.drawer)
        self.drop_overlay.dropped.connect(self.on_paths_dropped)
        # The fields would take a drop themselves; all drops go through the overlay so Source/Target are clear.
        self.txt_src.setAcceptDrops(False)
        self.txt_dst.setAcceptDrops(False)

        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.start_unified_scan)
        self.set_run_mode(False)

    # ---------------------------------------------------------- drag and drop, hand-picked videos
    def build_selection_bar(self):
        self.selection_bar = QFrame(objectName="detailBar")
        layout = QHBoxLayout(self.selection_bar)
        layout.setContentsMargins(12, 6, 12, 6)
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        back = QPushButton(tr("Back to folder mode"))
        back.clicked.connect(lambda: self.leave_selection())
        layout.addWidget(self.selection_label, 1)
        layout.addWidget(back)
        self.selection_bar.hide()
        return self.selection_bar

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.is_busy():
            self.drop_overlay.describe(dropped_paths(event.mimeData()))
            self.drop_overlay.setGeometry(self.rect())
            self.drop_overlay.raise_()
            self.drop_overlay.show()
            event.acceptProposedAction()
        else:
            event.ignore()   # while compressing, the queue does not change

    def on_paths_dropped(self, zone, paths):
        if self.is_busy():
            return
        if zone == "target":
            self.txt_dst.setText(str(paths[0] if paths[0].is_dir() else paths[0].parent))
        elif len(paths) == 1 and paths[0].is_dir():
            self.leave_selection(rescan=False)
            changed = self.txt_src.text() != str(paths[0])
            self.txt_src.setText(str(paths[0]))
            if not changed:
                self.schedule_scan()
        else:
            self.set_selection(paths)

    def set_selection(self, paths):
        videos, others = collect_videos(paths)
        if not videos:
            QMessageBox.information(self, tr("No videos"), tr("The dropped items contain no video files."))
            return
        if self.selection is None:
            self.folder_source_text = self.txt_src.text()
            self.folder_target_text = self.txt_dst.text()
            # Videos from different folders: by default every result goes next to its original.
            self.txt_dst.blockSignals(True)
            self.txt_dst.setText("")
            self.txt_dst.blockSignals(False)
            self.txt_dst.setPlaceholderText(tr("Next to each original (or choose one folder)"))
        self.selection, self.skipped_drops = videos, others
        self.txt_src.blockSignals(True)
        self.txt_src.setText(tr("{count} videos selected", count=len(videos)))
        self.txt_src.blockSignals(False)
        self.txt_src.setReadOnly(True)
        self.txt_src.setToolTip("\n".join(p.name for p in videos[:30]) + ("\n…" if len(videos) > 30 else ""))
        text = tr("Hand-picked videos: the results go next to the originals unless you choose a target folder.")
        if others:
            text += " " + tr("{count} dropped items were no videos and are ignored.", count=others)
        self.selection_label.setText(text)
        self.selection_bar.show()
        self.update_run_enabled()
        self.schedule_scan()

    def leave_selection(self, rescan=True):
        if self.selection is None:
            return
        self.selection = None
        self.txt_src.setReadOnly(False)
        self.selection_bar.hide()
        self.txt_src.blockSignals(True)
        self.txt_src.setText(self.folder_source_text)
        self.txt_src.blockSignals(False)
        self.txt_src.setToolTip(self.folder_source_text)
        self.txt_dst.blockSignals(True)
        self.txt_dst.setText(self.folder_target_text)
        self.txt_dst.blockSignals(False)
        self.txt_dst.setPlaceholderText(tr("Where the compressed videos go"))
        if rescan:
            self.schedule_scan()

    def has_source(self):
        return self.selection is not None or bool(self.txt_src.text().strip())

    def has_target(self):
        return self.selection is not None or bool(self.txt_dst.text().strip())   # none: next to the originals

    def scan_root(self):
        return common_parent(self.selection) if self.selection is not None else self.src_dir()

    def folder_field(self, label, placeholder):
        column = QVBoxLayout()
        column.setSpacing(4)
        column.addWidget(caption(label))
        row = QHBoxLayout()
        edit = DropLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setToolTip(tr("Type a path, drop a folder here, or choose one."))
        button = QPushButton(tr("Choose…"))
        button.clicked.connect(lambda: self.browse_folder(edit))
        row.addWidget(edit, stretch=1)
        row.addWidget(button)
        column.addLayout(row)
        return column, edit

    def build_header(self):
        header = QHBoxLayout()
        header.setSpacing(12)
        src_col, self.txt_src = self.folder_field(tr("Source"), tr("Folder with the original videos"))
        dst_col, self.txt_dst = self.folder_field(tr("Target"), tr("Where the compressed videos go"))
        arrow = QLabel("→")
        arrow.setAlignment(Qt.AlignBottom)
        arrow.setContentsMargins(0, 0, 0, 6)
        header.addLayout(src_col, stretch=1)
        header.addWidget(arrow)
        header.addLayout(dst_col, stretch=1)
        for edit in (self.txt_src, self.txt_dst):
            edit.textChanged.connect(self.schedule_scan)

        actions = QVBoxLayout()
        actions.addStretch()
        buttons = QHBoxLayout()
        self.btn_settings = QPushButton(tr("Settings"))
        self.btn_settings.setCheckable(True)
        self.btn_settings.setChecked(True)
        self.btn_settings.toggled.connect(lambda visible: self.drawer.setVisible(visible))
        more = QToolButton()
        more.setText("⋯")
        more.setToolTip(tr("More actions"))
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        # The shortcut itself is a window-wide QShortcut; the menu only shows it.
        shortcut = QKeySequence("Ctrl+R").toString(QKeySequence.NativeText)
        menu.addAction(f"{tr('Scan again')}\t{shortcut}", self.start_unified_scan)
        menu.addAction(tr("Export list as CSV…"), self.export_logs)
        menu.addAction(tr("Clear thumbnail and duration cache"), self.clear_cache)
        more.setMenu(menu)
        self.btn_run = QPushButton(objectName="runButton")
        self.btn_run.clicked.connect(self.toggle_run)
        self.btn_pause = QPushButton(tr("Pause"))
        self.btn_pause.setToolTip(tr("Freezes the running encodes; they continue where they stopped."))
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.hide()
        buttons.addWidget(self.btn_settings)
        buttons.addWidget(more)
        buttons.addWidget(self.btn_pause)
        buttons.addWidget(self.btn_run)
        actions.addLayout(buttons)
        self.cb_keep_awake = QCheckBox(tr("Keep computer awake"))
        self.cb_keep_awake.setToolTip(tr("Prevents sleep while videos are being compressed (not while paused)."))
        self.cb_keep_awake.setChecked(False)
        self.cb_keep_awake.toggled.connect(self.update_keep_awake)
        actions.addWidget(self.cb_keep_awake, 0, Qt.AlignRight)
        header.addLayout(actions)
        return header

    def build_summary(self):
        frame = QFrame(objectName="summary")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(28)
        self.summary_values = {}
        for key, label in (("count", tr("Videos")), ("size", tr("Size")), ("saving", tr("Savings")), ("time", tr("Estimated time")),
                           ("free", tr("Free at target"))):
            column = QVBoxLayout()
            column.setSpacing(0)
            caption_label = caption(label)
            value = QLabel("–")
            value.setProperty("role", "value")
            column.addWidget(caption_label)
            column.addWidget(value)
            layout.addLayout(column)
            self.summary_values[key] = (caption_label, value)
        progress = QVBoxLayout()
        progress.setSpacing(6)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_text = QLabel()
        self.progress_text.setProperty("role", "hint")
        self.progress_text.setWordWrap(True)
        progress.addWidget(self.progress_bar)
        progress.addWidget(self.progress_text)
        layout.addLayout(progress, stretch=1)
        return frame

    def build_toolbar(self):
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons = {}
        for key, _ in STATUS_FILTERS:
            button = QPushButton()
            button.setCheckable(True)
            button.setProperty("role", "chip")
            button.setFocusPolicy(Qt.TabFocus)
            self.filter_group.addButton(button)
            self.filter_buttons[key] = button
            button.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            bar.addWidget(button)
        self.filter_buttons["all"].setChecked(True)
        bar.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search files"))
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(240)
        self.search.textChanged.connect(self.proxy.set_text_filter)
        bar.addWidget(self.search)
        return bar

    def build_table(self):
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_SIZE, Qt.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)   # Cmd/Ctrl and Shift select several
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        # The vertical header numbers the rows as shown, i.e. in processing order, whatever the sort.
        vheader = self.table.verticalHeader()
        vheader.setDefaultSectionSize(46)
        vheader.setSectionResizeMode(QHeaderView.Fixed)
        vheader.setMinimumWidth(36)
        vheader.setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
        vheader.setHighlightSections(False)
        vheader.setStyleSheet("QHeaderView { background: transparent; } QHeaderView::section { background: transparent; border: none; color: #8a8f98; padding-right: 8px; }")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_FILE, QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.dragged_columns, self.fitting_columns = set(), False
        for col, width in self.FIXED_COLUMNS.items():
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            header.resizeSection(col, width)
        header.setSectionResizeMode(COL_SETTINGS, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_THUMB, QHeaderView.Interactive)
        self.view_save_timer = QTimer(self, singleShot=True, interval=500)
        self.view_save_timer.timeout.connect(self.save_view)
        header.sectionResized.connect(self.on_thumb_column_resized)
        self.fitting_columns = True   # the saved width is not a new drag
        header.resizeSection(COL_THUMB, self.saved_thumb_width())
        self.on_thumb_column_resized(COL_THUMB, 0, header.sectionSize(COL_THUMB))
        self.fitting_columns = False
        header.sectionResized.connect(self.remember_dragged_column)
        self.column_watcher = ResizeWatcher(self.table.viewport(), self.fit_columns)
        header.setHighlightSections(False)
        self.table.setItemDelegateForColumn(COL_THUMB, ThumbnailDelegate(self.model, self.table))
        self.table.setItemDelegateForColumn(COL_FILE, FileDelegate(self.model, self.table))
        self.table.setItemDelegateForColumn(COL_STATUS, StatusDelegate(self.table))
        self.table.selectionModel().selectionChanged.connect(self.update_detail_bar)
        header.sortIndicatorChanged.connect(self.reorder_queue)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_row_menu)
        header.setToolTip(tr("Videos are processed in this order. Click a column to change it."))
        self.table.doubleClicked.connect(self.on_row_double_clicked)
        return self.table

    def slider_row(self, label, minimum, maximum, scale, describe):
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        value = QLabel()
        value.setMinimumWidth(24)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Visible guidance (a tooltip alone is easy to miss): what the current value means,
        # then the whole scale.
        current = QLabel()
        current.setWordWrap(True)
        legend = QLabel(scale)
        legend.setProperty("role", "hint")
        legend.setWordWrap(True)

        def show(v):
            value.setText(str(v))
            current.setText(tr("Current: {meaning}", meaning=describe(v)))

        slider.valueChanged.connect(show)
        row.addWidget(slider, stretch=1)
        row.addWidget(value)
        column.addLayout(row)
        column.addWidget(current)
        column.addWidget(legend)
        return box, slider, current

    def segment(self, text, key):
        button = QPushButton(text)
        button.setCheckable(True)
        button.setProperty("role", "segment")
        button.clicked.connect(lambda: self.apply_quality_preset(key))
        return button

    def build_drawer(self):
        drawer = self.drawer_frame = QFrame(objectName="drawer")
        drawer.setFixedWidth(self.DRAWER_WIDTH)
        outer = QVBoxLayout(drawer)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        self.drawer_scroll = scroll
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(16)

        title = QLabel(tr("Settings"))
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(title)

        # Quality presets
        quality = QVBoxLayout()
        quality.setSpacing(6)
        quality.addWidget(caption(tr("Quality")))
        segments = QHBoxLayout()
        segments.setSpacing(0)
        self.quality_buttons = {
            "small": self.segment(tr("Small"), "small"),
            "archive": self.segment(tr("Archive"), "archive"),
            "high": self.segment(tr("High"), "high"),
        }
        for button in self.quality_buttons.values():
            segments.addWidget(button)
        quality.addLayout(segments)
        self.quality_hint = QLabel()
        self.quality_hint.setProperty("role", "hint")
        self.quality_hint.setWordWrap(True)
        # Room for the longest text (two lines), so the controls below do not jump when the slider
        # passes a preset and the hint gets longer.
        self.quality_hint.setMinimumHeight(self.quality_hint.fontMetrics().lineSpacing() * 2)
        self.quality_hint.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        quality.addWidget(self.quality_hint)
        layout.addLayout(quality)

        # Encoder
        encoder = QVBoxLayout()
        encoder.setSpacing(6)
        encoder.addWidget(caption(tr("Encoder")))
        self.combo_renderer = QComboBox()
        self.combo_renderer.addItems([tr("CPU (libx265, smaller files)"), tr("Mac GPU (VideoToolbox, fast)")])
        self.combo_renderer.currentIndexChanged.connect(self.update_quality_state)
        encoder.addWidget(self.combo_renderer)
        layout.addLayout(encoder)

        # Limits and parallelism
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(caption(tr("Limits")), 0, 0, 1, 2)
        self.combo_res = QComboBox()
        for value in RESOLUTIONS:
            self.combo_res.addItem(tr("No limit") if value is None else tr("{pixels} px (long side)", pixels=value), value)
        self.combo_res.setCurrentIndex(RESOLUTIONS.index(1920))
        self.combo_fps = QComboBox()
        for value in FRAME_RATES:
            self.combo_fps.addItem(tr("No limit") if value is None else f"{value} fps", value)
        self.combo_fps.setCurrentIndex(FRAME_RATES.index(30))
        self.spin_jobs = JobsBox()
        self.spin_jobs.setToolTip(tr("More parallel videos only help while CPU or GPU still have headroom."))
        grid.addWidget(QLabel(tr("Resolution")), 1, 0)
        grid.addWidget(self.combo_res, 1, 1)
        grid.addWidget(QLabel(tr("Frame rate")), 2, 0)
        grid.addWidget(self.combo_fps, 2, 1)
        grid.addWidget(QLabel(tr("Parallel videos")), 3, 0)
        grid.addWidget(self.spin_jobs, 3, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        photos = QVBoxLayout()
        photos.setSpacing(4)
        photos.addWidget(caption(tr("Apple Photos")))
        photos_hint = QLabel(tr("Output is always HEVC (hvc1) with AAC audio; capture date and location are kept."))
        photos_hint.setProperty("role", "hint")
        photos_hint.setWordWrap(True)
        photos.addWidget(photos_hint)
        layout.addLayout(photos)

        # Search: which videos of the source folder are listed at all.
        search = QVBoxLayout()
        search.setSpacing(6)
        search.addWidget(caption(tr("Search")))
        self.cb_skip_hidden = QCheckBox(tr("Ignore hidden files/folders"))
        self.cb_skip_hidden.setToolTip(tr("Skips everything whose name starts with a dot, e.g. app data in .hermes or .Trash."))
        self.cb_skip_hidden.setChecked(True)
        self.cb_skip_hidden.toggled.connect(self.schedule_scan)
        search.addWidget(self.cb_skip_hidden)
        self.txt_excludes = QLineEdit(DEFAULT_EXCLUDES)
        self.txt_excludes.setToolTip(tr("Source videos whose name ends with one of these (separated by commas) are skipped, "
                                        "e.g. results of earlier runs. The result postfix is always skipped."))
        self.txt_excludes.textChanged.connect(self.schedule_scan)
        search.addWidget(QLabel(tr("Skip names ending in")))
        search.addWidget(self.txt_excludes)
        layout.addLayout(search)

        # Advanced
        self.btn_advanced = QToolButton()
        self.btn_advanced.setText(tr("Advanced"))
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_advanced.setArrowType(Qt.RightArrow)
        self.btn_advanced.setAutoRaise(True)
        self.btn_advanced.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.btn_advanced)
        self.advanced = QWidget()
        adv = QVBoxLayout(self.advanced)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(8)

        # Only the controls of the selected encoder are shown (see update_quality_state).
        self.cpu_quality, self.slider_crf, self.crf_hint = self.slider_row(
            tr("CRF"), 0, 51,
            tr("Lower means better quality and larger files; 18-19 is visually lossless, 20-23 the sweet spot for archives."),
            describe_crf)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel(tr("Preset")))
        self.combo_preset = QComboBox()
        self.combo_preset.addItems(X265_PRESETS)
        self.combo_preset.setCurrentText("slow")
        preset_row.addWidget(self.combo_preset, stretch=1)
        self.cpu_quality.layout().addLayout(preset_row)
        preset_hint = QLabel(tr("Slower presets give smaller files at the same quality but take longer."))
        preset_hint.setProperty("role", "hint")
        preset_hint.setWordWrap(True)
        self.cpu_quality.layout().addWidget(preset_hint)
        self.gpu_quality, self.slider_vt, self.vt_hint = self.slider_row(
            tr("Quality"), 1, 100,
            tr("Higher means better quality and larger files; 45-59 is balanced, 60-79 almost visually lossless, 80+ visually lossless."),
            describe_vt)
        adv.addWidget(self.cpu_quality)
        adv.addWidget(self.gpu_quality)
        for slider in (self.slider_crf, self.slider_vt):
            slider.valueChanged.connect(self.update_quality_state)

        # Short labels: QCheckBox does not wrap, so long texts would widen the whole drawer.
        self.cb_copy_aac = QCheckBox(tr("Copy AAC audio 1:1"))
        self.cb_copy_aac.setToolTip(tr("Lossless and faster; only applies when the source already has AAC audio."))
        self.cb_copy_aac.setChecked(True)
        self.cb_flatten = QCheckBox(tr("Don't keep subfolders"))
        self.cb_flatten.setToolTip(tr("All videos go directly into the target folder."))
        self.cb_flatten.toggled.connect(self.schedule_scan)
        self.cb_overwrite = QCheckBox(tr("Overwrite existing results"))
        self.cb_overwrite.setToolTip(tr("Videos that already have a result in the target folder are compressed again."))
        self.cb_overwrite.toggled.connect(self.update_run_enabled)
        self.cb_overwrite.toggled.connect(self.request_summary)
        self.cb_low_priority = QCheckBox(tr("Low priority"))
        self.cb_low_priority.setToolTip(tr("Encodes with lower priority so the computer stays responsive; the run may take a little longer."))
        self.cb_low_priority.setChecked(True)
        self.cb_dry_run = QCheckBox(tr("Test run (first second only)"))
        self.cb_dry_run.setToolTip(tr("Compresses only the first second of each video to check the settings quickly."))
        adv.addWidget(caption(tr("Encoding")))
        for box in (self.cb_copy_aac, self.cb_low_priority, self.cb_dry_run):
            adv.addWidget(box)
        adv.addWidget(caption(tr("Output")))
        for box in (self.cb_flatten, self.cb_overwrite):
            adv.addWidget(box)

        self.txt_postfix = QLineEdit(DEFAULT_POSTFIX)
        self.txt_postfix.setToolTip(tr("Appended to the name of each result, e.g. clip{postfix}.mp4.", postfix=DEFAULT_POSTFIX))
        self.txt_postfix.textChanged.connect(self.schedule_scan)
        adv.addWidget(QLabel(tr("Result postfix")))
        adv.addWidget(self.txt_postfix)
        self.advanced.hide()
        self.btn_advanced.toggled.connect(self.advanced.setVisible)
        self.btn_advanced.toggled.connect(lambda on: self.btn_advanced.setArrowType(Qt.DownArrow if on else Qt.RightArrow))
        layout.addWidget(self.advanced)

        layout.addStretch()
        self.btn_save_defaults = QPushButton(tr("Save as default"))
        self.btn_save_defaults.clicked.connect(self.save_defaults)
        layout.addWidget(self.btn_save_defaults)

        self.apply_quality_preset("archive")
        self.btn_advanced.toggled.connect(lambda _on: QTimer.singleShot(0, self.fit_drawer_width))
        self.fit_drawer_width()
        return drawer

    DRAWER_WIDTH = 340

    def fit_drawer_width(self):
        """At least DRAWER_WIDTH, wider if the content needs it: with other fonts (Windows) the options did not fit
        and were cut off at the right, as the drawer scrolls only up and down."""
        content = self.drawer_scroll.widget()
        content.layout().activate()
        needed = content.minimumSizeHint().width() + self.drawer_scroll.verticalScrollBar().sizeHint().width()
        self.drawer_frame.setFixedWidth(max(self.DRAWER_WIDTH, needed))

    # ------------------------------------------------------------ settings UI
    def apply_quality_preset(self, key):
        crf, vt = QUALITY_PRESETS[key]
        self.slider_crf.setValue(crf)
        self.slider_vt.setValue(vt)
        self.update_quality_state()

    def update_quality_state(self):
        cpu = self.combo_renderer.currentIndex() == 0
        # A preset counts as active when the selected encoder's value matches it.
        position, value = (0, self.slider_crf.value()) if cpu else (1, self.slider_vt.value())
        active = next((k for k, v in QUALITY_PRESETS.items() if v[position] == value), None)
        for key, button in self.quality_buttons.items():
            button.setChecked(key == active)
        hints = {
            "small": tr("Much smaller files, some visible loss of detail."),
            "archive": tr("Hardly any visible difference to the original."),
            "high": tr("Practically lossless; files stay large."),
        }
        detail = tr("CRF {value}", value=value) if cpu else tr("VideoToolbox quality {value}", value=value)
        self.quality_hint.setText(f"{hints[active]} ({detail})" if active else tr("Custom: {detail}", detail=detail))
        changed = (self.cpu_quality.isVisibleTo(self.advanced) != cpu
                   or self.gpu_quality.isVisibleTo(self.advanced) == cpu)
        self.cpu_quality.setVisible(cpu)
        self.gpu_quality.setVisible(not cpu)
        if changed:
            # Word-wrapped labels keep a height computed for another width after being
            # hidden; recompute so the section does not get uneven gaps.
            self.drawer_scroll.widget().layout().invalidate()
            self.fit_drawer_width()

    def browse_folder(self, line_edit):
        folder = QFileDialog.getExistingDirectory(self, tr("Choose folder"), line_edit.text())
        if folder:
            line_edit.setText(folder)

    def check_system_dependencies(self):
        missing = check_dependencies()
        if missing:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Critical)
            msg.setWindowTitle(tr("Missing system dependencies"))
            msg.setText(tr("The following required tools were not found:\n\n{tools}", tools=", ".join(missing)))
            msg.setInformativeText(tr("Please install them (e.g. via Homebrew: 'brew install ffmpeg exiftool') so the app works."))
            msg.exec()

    # ------------------------------------------------------------------ state
    def src_dir(self):
        return Path(self.txt_src.text().strip()).expanduser()

    def dst_dir(self):
        return Path(self.txt_dst.text().strip()).expanduser()

    def postfix(self):
        return clean_postfix(self.txt_postfix.text())

    def is_archiving(self):
        return self.worker is not None and self.worker.isRunning()

    def is_scanning(self):
        return self.scan_worker is not None and self.scan_worker.isRunning()

    def is_busy(self):
        return self.is_archiving() or self.is_scanning()

    def shutdown(self):
        self.scan_timer.stop()
        self.thumbnails.shutdown()
        self.keep_awake.stop()
        for worker in (self.worker, self.scan_worker, self.duration_worker, self.settings_worker, self.block_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "stop"):
                    worker.stop()
                worker.wait()

    def set_run_mode(self, running):
        self.btn_pause.setVisible(running)
        self.btn_pause.setText(tr("Pause"))
        self.btn_run.setProperty("mode", "stop" if running else "start")
        self.btn_run.setText(tr("Stop") if running else tr("Start archiving"))
        self.btn_run.setIcon(self.style().standardIcon(QStyle.SP_MediaStop if running else QStyle.SP_MediaPlay))
        self.btn_run.style().unpolish(self.btn_run)
        self.btn_run.style().polish(self.btn_run)
        self.update_run_enabled()

    def run_blocked_reason(self):
        """Why the start button is disabled, or "" when it is not."""
        if self.is_archiving():
            return ""
        if self.is_scanning():
            return tr("The folders are still being scanned.")
        if not self.has_source() or not self.has_target():
            return tr("Choose a source and a target folder first.")
        if not self.queue_items():
            if self.scan_items:
                return tr("Nothing to do: all videos already have a result. Turn on “Overwrite existing results” to compress them again.")
            return tr("No videos found in the source folder.")
        return ""

    def update_run_enabled(self):
        reason = self.run_blocked_reason()
        self.btn_run.setEnabled(not reason)
        self.btn_run.setToolTip(reason)
        for widget in (self.spin_jobs, self.txt_src, self.txt_dst, self.cb_flatten, self.txt_postfix, self.txt_excludes,
                       self.cb_skip_hidden):
            widget.setEnabled(not self.is_archiving())

    def request_summary(self):
        # Progress updates arrive many times per second; coalesce them.
        if not self.summary_timer.isActive():
            self.summary_timer.start()

    def set_filter(self, key):
        self.proxy.set_status_filter(dict(STATUS_FILTERS)[key])

    # ------------------------------------------------------------------- scan
    def schedule_scan(self):
        if not self.is_archiving():
            self.scan_timer.start()

    def auto_scan(self):
        src = self.txt_src.text().strip()
        if self.selection is not None or (src and self.txt_dst.text().strip() and self.src_dir().is_dir()):
            self.start_unified_scan()
        else:
            self.model.reset([])
            self.scan_items = []
            self.video_data_list = []
            self.update_run_enabled()

    def start_unified_scan(self):
        self.scan_timer.stop()
        if self.is_archiving():
            return
        if self.is_scanning():
            self.rescan_pending = True
            return
        if self.duration_worker is not None and self.duration_worker.isRunning():
            self.duration_worker.stop()
            self.duration_worker.wait()
        if self.settings_worker is not None:
            self.settings_worker.wait()  # a single exiftool call; results of the old scan are discarded
        self.total_src_bytes = 0
        self.total_dst_bytes = 0
        self.run_paths = set()
        if self.selection is not None:
            self.scan_worker = UnifiedScanWorker(
                self.scan_root(), self.dst_dir() if self.txt_dst.text().strip() else None, self.cb_flatten.isChecked(),
                postfix=self.postfix(), excludes=parse_excludes(self.txt_excludes.text()), files=self.selection,
                skip_hidden=self.cb_skip_hidden.isChecked())
        else:
            self.scan_worker = UnifiedScanWorker(
                self.src_dir(), self.dst_dir(), self.cb_flatten.isChecked(),
                postfix=self.postfix(), excludes=parse_excludes(self.txt_excludes.text()),
                skip_hidden=self.cb_skip_hidden.isChecked())
        self.scan_worker.scan_finished.connect(self.on_unified_scan_finished)
        # The result arrives while the thread is still ending, so is_scanning() is still true in that handler.
        self.scan_worker.finished.connect(self.update_run_enabled)
        self.scan_worker.start()
        self.update_run_enabled()
        self.refresh_summary()

    @Slot(list)
    def on_unified_scan_finished(self, items):
        src_root = self.scan_root()
        rows = []
        for item in items:
            exists = item.get('exists_compressed')
            rows.append(VideoRow(
                src=item['path'], dst=item['dst_path'], size=item['size'], root=src_root,
                status="exists" if exists else "planned",
                out_size=item['comp_size'] if exists else None,
                conflict=bool(item.get('conflict')),
                block_check=self.block_checks.get(str(item['path'])) if exists else None,
            ))
        counts = {}
        for row in rows:
            counts[row.dst] = counts.get(row.dst, 0) + 1
        for row in rows:
            row.duplicate = counts[row.dst] > 1
        self.scan_items = items
        self.moved_up = []
        self.video_data_list = [item for item in items if not item.get('exists_compressed')]
        self.model.reset(rows)
        self.detail_bar.hide()
        self.update_run_enabled()
        self.refresh_summary()

        # In table order, so the rows on screen get their pictures first.
        order = [self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in range(self.proxy.rowCount())]
        seen = set(order)
        order += [i for i in range(len(rows)) if i not in seen]   # filtered out: last
        self.thumbnails.load([rows[i].src for i in order])
        self.duration_worker = DurationProbeWorker([row.src for row in rows])
        self.duration_worker.duration_found.connect(self.on_duration_found)
        self.duration_worker.finished.connect(self.request_summary)
        self.duration_worker.start()
        self.settings_worker = ResultSettingsWorker([(row.src, row.dst) for row in rows if row.status == "exists"])
        self.settings_worker.settings_found.connect(self.on_settings_found)
        self.settings_worker.start()

        if self.rescan_pending:
            self.rescan_pending = False
            self.start_unified_scan()

    # ---------------------------------------------------------------- archive
    def toggle_run(self):
        if self.is_archiving():
            self.stop_archiving()
        else:
            self.start_archiving()

    def queue_items(self):
        """Videos to process, in the table's sort order with moved-up videos first."""
        # Failed videos are retried; with "overwrite", every video with a result is compressed again.
        wanted = {"planned", "error"}
        if self.cb_overwrite.isChecked():
            wanted |= {"exists", "done", "skipped"}
        items = [item for item in self.scan_items if self.model.row_for(item['path']).status in wanted]
        header = self.table.horizontalHeader()
        column = header.sortIndicatorSection()
        items.sort(key=lambda item: self.model.sort_key(self.model.row_for(item['path']), column),
                   reverse=header.sortIndicatorOrder() == Qt.DescendingOrder)
        rank = {path: i for i, path in enumerate(self.moved_up)}
        items.sort(key=lambda item: rank.get(str(item['path']), len(rank)))  # stable: keeps table order
        return items

    def reorder_queue(self):
        if self.is_archiving():
            self.worker.set_queue(self.queue_items())

    def move_up(self, row):
        path = str(row.src)
        if path in self.moved_up:
            self.moved_up.remove(path)
        self.moved_up.insert(0, path)
        self.model.update(row.src, moved_up=True)
        self.reorder_queue()
        self.update_detail_bar()

    def current_run_settings(self):
        """The compression settings as the drawer shows them, in the form the worker takes."""
        return {
            'limit_res': self.combo_res.currentData() is not None,
            'max_res': self.combo_res.currentData() or 0,
            'limit_fps': self.combo_fps.currentData() is not None,
            'max_fps': self.combo_fps.currentData() or 0,
            'renderer': RENDERERS[self.combo_renderer.currentIndex()],
            'crf': self.slider_crf.value(),
            'preset': self.combo_preset.currentText(),
            'vt_quality': self.slider_vt.value(),
            'overwrite': self.cb_overwrite.isChecked(),
            'copy_aac': self.cb_copy_aac.isChecked(),
            'dry_run': self.cb_dry_run.isChecked(),
            'low_priority': self.cb_low_priority.isChecked(),
        }

    def apply_run_settings(self, settings):
        """Puts settings (as from current_run_settings, e.g. chosen in the compression lab) into the drawer."""
        self.combo_renderer.setCurrentIndex(RENDERERS.index(settings['renderer']))
        self.slider_crf.setValue(settings['crf'])
        self.slider_vt.setValue(settings['vt_quality'])
        self.combo_preset.setCurrentText(settings['preset'])
        res = settings['max_res'] if settings['limit_res'] else None
        fps = settings['max_fps'] if settings['limit_fps'] else None
        self.combo_res.setCurrentIndex(RESOLUTIONS.index(res) if res in RESOLUTIONS else 0)
        self.combo_fps.setCurrentIndex(FRAME_RATES.index(fps) if fps in FRAME_RATES else 0)
        self.update_quality_state()

    def start_archiving(self):
        if self.is_archiving() or not self.queue_items():
            return
        if self.txt_dst.text().strip():
            try:
                self.dst_dir().mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, tr("Target folder not available"),
                                     tr("The target folder cannot be used: {error}\n\nIs the drive connected?", error=e))
                return
        replacing = [item for item in self.queue_items() if item.get('conflict')]
        if replacing and not confirm_after_delay(
                self, tr("Source = target!"),
                tr("{count} videos have the same source and target file. If you continue, these original files are "
                   "REPLACED by their compressed versions and the originals go to the trash.\n\nCancel and set a result "
                   "postfix or another target folder to keep them.", count=len(replacing)),
                tr("Replace originals")):
            return
        if self.cb_overwrite.isChecked():
            reply = QMessageBox.question(
                self, tr("Overwrite existing results?"),
                tr("“Overwrite existing results” is on: existing files in the target folder will be replaced. Continue?"),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        settings = self.current_run_settings()
        queue = self.queue_items()
        for item in queue:  # re-runs (overwrite, retry) start from a clean row
            self.model.update(item['path'], status="planned", progress=0, speed="", note="")
        self.run_settings, self.run_jobs, self.run_paused, self.file_started = settings, self.spin_jobs.value(), False, {}
        self.run_paths = {str(item['path']) for item in queue}
        self.run_bytes = sum(item['size'] for item in queue)
        self.run_started = time.monotonic()

        self.worker = ArchiveWorker(self.scan_root(), self.dst_dir() if self.txt_dst.text().strip() else self.scan_root(),
                                    self.spin_jobs.value(), queue, settings)
        self.worker.status_update_path.connect(self.on_status_update)
        self.worker.file_duration_discovered_path.connect(self.on_duration_found)
        self.worker.file_progress_path.connect(self.on_file_progress)
        self.worker.ffmpeg_log_line_path.connect(self.on_log_line)
        # QThread.finished, not finished_all: the latter is emitted while the thread still
        # runs, so the controls would still treat the run as active.
        self.worker.finished.connect(self.on_finished_all)
        self.stopped_by_user = False
        self.paused_at = None
        self.worker.start()
        self.update_keep_awake()
        self.set_run_mode(True)
        self.eta_timer.start()
        self.refresh_summary()

    def update_keep_awake(self):
        """Awake while a run is going and not paused, and only if the box next to Start is ticked."""
        if self.is_archiving() and not self.worker.paused and self.cb_keep_awake.isChecked():
            self.keep_awake.start()
        else:
            self.keep_awake.stop()

    def is_paused(self):
        return self.is_archiving() and self.worker.paused

    def toggle_pause(self):
        if not self.is_archiving():
            return
        if self.worker.paused:
            paused = time.monotonic() - self.paused_at
            self.run_started += paused   # paused time does not count for the estimate
            self.paused_at = self.model.paused_since = None
            for row in self.model.rows:   # nor for how long a video has been running
                if row.status == "running" and row.started_at is not None:
                    row.started_at += paused
            self.worker.resume()
            self.update_keep_awake()
            self.btn_pause.setText(tr("Pause"))
        else:
            self.paused_at = self.model.paused_since = time.monotonic()
            self.run_paused = True   # the times of this run no longer say how fast encoding is
            self.worker.pause()
            self.update_keep_awake()                                # nothing runs, the computer may sleep
            self.btn_pause.setText(tr("Resume"))
        self.refresh_summary()

    def stop_archiving(self):
        if self.is_archiving():
            self.stopped_by_user = True
            self.btn_run.setEnabled(False)
            self.worker.stop()
            self.worker.wait()

    @Slot(str, str, str, dict)
    def on_status_update(self, path, status, reason, data):
        if status == "running":
            started = self.file_started.setdefault(path, time.monotonic())
            self.model.update(path, status="running", speed=reason, started_at=started)
            return
        if status == "finished":
            row = self.model.row_for(path)
            if row is not None and row.started_at is not None:
                self.model.update(path, took=time.monotonic() - row.started_at)
            self.record_speed(path)
        values = {"status": status, "note": reason, "speed": ""}
        if status in ("finished", "skipped") and data:
            values["out_size"] = data.get('dst_size')
            if status == "finished":
                values["settings"] = data.get('settings')
            self.total_src_bytes += data.get('src_size', 0)
            self.total_dst_bytes += data.get('dst_size', 0)
        if status == "finished":
            values["status"] = "done"
        self.model.update(path, **values)
        self.update_detail_bar()
        if status == "finished":
            self.block_checks.pop(path, None)
            self.model.update(path, block_check=None)
            if data and data.get('diff_size'):  # not when the original was kept: nothing new to look at
                self.check_blocks(path)

    def check_blocks(self, path):
        row = self.model.row_for(path)
        if row is None or not row.dst.exists():
            return
        if self.block_worker is None:
            self.block_worker = BlockCheckWorker()
            self.block_worker.checked.connect(self.on_block_checked)
        self.block_worker.add(row.src, row.dst, same_file=row.src == row.dst)

    @Slot(str, object)
    def on_block_checked(self, path, check):
        self.block_checks[path] = check
        self.model.update(path, block_check=check)

    def record_speed(self, path):
        """Remembers how long this video took, for the time estimate of the next folder."""
        started = self.file_started.pop(path, None)
        row = self.model.row_for(path)
        if (started is None or row is None or not row.duration or self.run_paused or not self.run_settings
                or self.run_settings.get('dry_run')):
            return
        self.speeds.record(self.run_settings, self.run_jobs, row.duration, time.monotonic() - started)

    @Slot(str, object)
    def on_thumbnail_ready(self, path, image):
        if image is not None and not image.isNull():
            self.model.set_thumbnail(path, QPixmap.fromImage(image))

    @Slot(str, object)
    def on_settings_found(self, path, settings):
        self.model.update(path, settings=settings)

    @Slot(str, float)
    def on_duration_found(self, path, duration):
        self.model.update(path, duration=duration)
        if self.is_archiving() and self.table.horizontalHeader().sortIndicatorSection() == COL_DURATION:
            self.requeue_timer.start()

    @Slot(str, int)
    def on_file_progress(self, path, percent):
        self.model.update(path, progress=percent)

    @Slot(str, str)
    def on_log_line(self, path, line):
        self.model.append_log(path, line)
        if self.log_dialog is not None and self.log_dialog.path == path and self.log_dialog.isVisible():
            self.log_dialog.append(line)

    @Slot()
    def on_finished_all(self):
        for row in self.model.rows:
            if row.status == "running":
                self.model.update(row.src, status="planned", progress=0, speed="")
        self.eta_timer.stop()
        self.keep_awake.stop()
        self.set_run_mode(False)
        self.refresh_summary()
        if not self.stopped_by_user:
            self.notify_finished()

    def notify_finished(self):
        run = [r for r in self.model.rows if str(r.src) in self.run_paths]
        done = sum(1 for r in run if r.status in ("done", "skipped"))
        errors = sum(1 for r in run if r.status == "error")
        message = tr("{done} of {total} finished", done=done, total=len(run))
        if errors:
            message += " · " + tr("{count} with errors.", count=errors)
        self.notifier(self.window(), tr("Archive Helper"), message)

    # ---------------------------------------------------------------- summary
    def refresh_summary(self):
        rows = self.model.rows
        counts = self.model.counts()
        labels = {
            "all": tr("All"), "planned": tr("Planned"), "running": tr("Running"),
            "done": tr("Done"), "skipped": tr("Skipped"), "error": tr("Errors"),
        }
        for key, statuses in STATUS_FILTERS:
            count = len(rows) if statuses is None else sum(counts.get(s, 0) for s in statuses)
            button = self.filter_buttons[key]
            button.setText(f"{labels[key]}  {count}")
            button.setVisible(key == "all" or count > 0)

        planned = [r for r in rows if r.status in ("planned", "running")]
        measured = [r for r in rows if r.ratio is not None]
        self.set_summary("count", str(len(rows)))
        self.set_summary("size", format_size(sum(r.size for r in rows)) if rows else "–")

        caption_label, value = self.summary_values["saving"]
        ratio = sum(r.out_size for r in measured) / sum(r.size for r in measured) if measured else None
        if ratio is None:
            caption_label.setText(tr("Savings").upper())
            self.set_summary("saving", "–", tooltip=tr("Estimated once the first video is finished."))
        else:
            saved = sum(r.size - r.out_size for r in measured)
            planned_bytes = sum(r.size for r in planned)
            estimate = bool(planned_bytes)
            saved += planned_bytes * (1 - ratio)
            total = sum(r.size for r in measured) + planned_bytes
            caption_label.setText((tr("Savings (estimated)") if estimate else tr("Savings")).upper())
            prefix = "≈ " if estimate else ""
            percent = abs(saved) / total * 100
            wording = tr("{percent:.0f} % smaller → {size}") if saved >= 0 else tr("{percent:.0f} % larger → {size}")
            self.set_summary("saving", prefix + wording.format(percent=percent, size=format_size(total - saved)),
                             tone="good" if saved >= 0 else "warn",
                             tooltip=tr("Based on the videos already compressed (ratio {ratio:.0%}).", ratio=ratio))

        self.refresh_time_estimate()

        if self.selection is not None and not self.txt_dst.text().strip():
            target = nearest_existing(self.scan_root())
        else:
            target = nearest_existing(self.dst_dir()) if self.txt_dst.text().strip() else None
        if target is None:
            self.set_summary("free", "–")
        else:
            free = shutil.disk_usage(target).free
            needed = sum(r.size for r in planned) * (ratio if ratio is not None else 1.0)
            low = free < needed
            self.set_summary("free", format_size(free), tone="warn" if low else None,
                             tooltip=tr("Probably not enough space: about {needed} needed.", needed=format_size(needed)) if low else "")

        self.progress_text.setText(self.progress_message(rows, counts))
        self.update_run_enabled()

    def queue_video_seconds(self):
        """(seconds of video still to compress, number of videos whose length is not known yet)."""
        rows = [self.model.row_for(item['path']) for item in self.queue_items()]
        known = [row.duration for row in rows if row.duration]
        total = sum(known)
        unknown = len(rows) - len(known)
        if unknown and known:
            total += unknown * total / len(known)    # videos not read yet count like the average one
        return total, unknown, len(rows)

    def refresh_time_estimate(self):
        seconds, unknown, count = self.queue_video_seconds()
        if not count:
            self.set_summary("time", "–")
            return
        estimate = self.speeds.estimate(self.current_run_settings(), self.spin_jobs.value(), seconds) if seconds else None
        if estimate is None:
            hint = tr("Unknown until a video was compressed with these settings, or tried out with “Inspect different compressions”.")
            self.set_summary("time", "–", tooltip=hint)
            return
        total, exact = estimate
        text = f"≈ {format_duration_long(total)}"
        tooltip = tr("Based on how long encodes with these settings took before.")
        if not exact:
            tooltip += " " + tr("Measured with a different number of parallel videos, so this is a cautious guess.")
        if unknown:
            tooltip += " " + tr("{count} videos have not been read yet; they count like the average.", count=unknown)
        self.set_summary("time", text, tooltip=tooltip)

    def set_summary(self, key, text, tone=None, tooltip=""):
        _, label = self.summary_values[key]
        label.setText(text)
        label.setToolTip(tooltip)
        if label.property("tone") != tone:
            label.setProperty("tone", tone)
            label.style().unpolish(label)
            label.style().polish(label)

    def progress_message(self, rows, counts):
        if self.is_scanning():
            self.progress_bar.setRange(0, 0)
            return tr("Scanning folders…")
        self.progress_bar.setRange(0, 1000)
        if not rows:
            self.progress_bar.setValue(0)
            if not self.has_source() or not self.has_target():
                return tr("Choose a source and a target folder; the scan starts automatically.")
            return tr("No videos found in the source folder.")

        run_rows = [r for r in rows if str(r.src) in self.run_paths]
        finished = [r for r in run_rows if r.status in ("done", "skipped", "error")]
        done_bytes = sum(r.size for r in finished) + sum(r.size * r.progress / 100 for r in run_rows if r.status == "running")
        self.progress_bar.setValue(int(done_bytes / self.run_bytes * 1000) if self.run_bytes else 0)

        if self.is_paused():
            return tr("Paused: {done} of {total} finished", done=len(finished), total=len(run_rows))
        if self.is_archiving():
            text = tr("{done} of {total} finished", done=len(finished), total=len(run_rows))
            elapsed = time.monotonic() - self.run_started
            if done_bytes > 0 and elapsed > 15:
                rate = done_bytes / elapsed
                text += " · " + tr("about {time} left", time=format_duration_long((self.run_bytes - done_bytes) / rate))
            else:
                text += " · " + tr("estimating time left…")
            return text
        parts = []
        if run_rows:
            errors = counts.get("error", 0)
            parts.append(tr("Stopped: {done} of {total} finished.", done=len(finished), total=len(run_rows))
                         if counts.get("planned") else tr("Finished: {done} videos processed.", done=len(finished)))
            if errors:
                parts.append(tr("{count} with errors.", count=errors))
            blocky = sum(r.blocky for r in run_rows)
            if blocky:
                parts.append(tr("⚠ {count} may show blocks.", count=blocky))
            lacking = sum(bool(r.metadata_missing) for r in run_rows)
            if lacking:
                parts.append(tr("⚠ {count} lack metadata of the original.", count=lacking))
        queue = self.queue_items()
        conflicts = sum(1 for item in queue if item.get('conflict'))
        if conflicts:
            parts.append(tr("⚠ {count} with source = target!", count=conflicts))
        replacing = sum(1 for item in queue if self.model.row_for(item['path']).out_size is not None)
        if queue and replacing:
            parts.append(tr("Ready: {count} to compress, {replacing} of them replace an existing result. Order: as sorted in the table.",
                            count=len(queue), replacing=replacing))
        elif queue:
            parts.append(tr("Ready: {count} to compress. Order: as sorted in the table.", count=len(queue)))
        elif not run_rows:
            parts.append(tr("Nothing to do: all {count} videos already have a result. Turn on “Overwrite existing results” to compress them again.",
                            count=len(rows)))
        if self.duration_worker is not None and self.duration_worker.isRunning():
            parts.append(tr("Reading durations: {done} of {total}…", done=sum(1 for r in rows if r.duration), total=len(rows)))
        return " ".join(parts)

    # ------------------------------------------------------------- row detail
    def selected_rows(self):
        """The selected rows in the order they are shown."""
        indexes = sorted(self.table.selectionModel().selectedRows(), key=lambda index: index.row())
        return [index.data(Qt.UserRole + 1) for index in indexes]

    @staticmethod
    def file_action_applies(action, row):
        """Not for the video being compressed; deleting or replacing the original needs a result to fall back on."""
        if row.status == "running" or not row.has_result:
            return False
        return action == "del_comp" or row.src.exists()

    def add_file_actions(self, menu, rows):
        """Replace/swap/delete for one or several rows; also offered during a run, except for the video being compressed."""
        rows = rows if isinstance(rows, list) else [rows]
        for text, action in (
            (tr("Replace original with result…"), "overwrite"),
            (tr("Swap original and result…"), "swap"),
            (tr("Move original to trash…"), "del_orig"),
            (tr("Move result to trash…"), "del_comp"),
        ):
            count = sum(self.file_action_applies(action, row) for row in rows)
            label = text if len(rows) == 1 else f"{text} ({count})"
            act = menu.addAction(label, lambda a=action: self.handle_compare_action(a, rows))
            act.setEnabled(count > 0)

    def move_up_rows(self, rows):
        """Several videos to the front of the queue, keeping their order in the table."""
        for row in reversed([row for row in rows if row.status == "planned"]):
            self.move_up(row)

    def build_row_menu(self, row):
        """Everything that can be done with a row; used for the right-click menu."""
        menu = QMenu(self)
        src_ok, dst_ok = row.src.exists(), row.dst.exists() and row.out_size is not None
        if row.status == "planned":
            menu.addAction(tr("Process next"), lambda: self.move_up(row))
        menu.addAction(tr("Inspect different compressions"), lambda: self.open_lab(row)).setEnabled(src_ok)
        if dst_ok:
            menu.addAction(tr("Compare"), lambda: self.compare(row)).setEnabled(src_ok and row.status != "running")
        else:
            menu.addAction(tr("Play original"), lambda: self.compare(row)).setEnabled(src_ok)
        menu.addAction(tr("Show original"), lambda: open_in_finder(row.src)).setEnabled(src_ok)
        menu.addAction(tr("Show result"), lambda: open_in_finder(row.dst)).setEnabled(dst_ok)
        menu.addAction(tr("Log"), lambda: self.show_log(row)).setEnabled(bool(row.log))
        menu.addSeparator()
        self.add_file_actions(menu, row)
        return menu

    def build_rows_menu(self, rows):
        """The right-click menu for several selected rows: what can be applied to all of them."""
        menu = QMenu(self)
        menu.addAction(tr("Process next"), lambda: self.move_up_rows(rows)).setEnabled(
            any(row.status == "planned" for row in rows))
        menu.addSeparator()
        self.add_file_actions(menu, rows)
        return menu

    def show_row_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        rows = self.selected_rows()
        clicked = index.data(Qt.UserRole + 1)
        if len(rows) > 1 and clicked in rows:   # right-click inside a multiple selection keeps it
            menu = self.build_rows_menu(rows)
        else:
            self.table.selectRow(index.row())
            menu = self.build_row_menu(clicked)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def update_detail_bar(self, *_):
        rows = self.selected_rows()
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not rows:
            self.detail_bar.hide()
            return

        def add(text, handler, enabled=True):
            button = QPushButton(text)
            button.setEnabled(enabled)
            button.clicked.connect(handler)
            self.detail_layout.addWidget(button)
            return button

        if len(rows) > 1:
            label = QLabel(f"<b>{tr('{count} videos selected', count=len(rows))}</b>")
            label.setTextFormat(Qt.RichText)
            self.detail_layout.addWidget(label, stretch=1)
            add(tr("Process next"), lambda: self.move_up_rows(rows), any(r.status == "planned" for r in rows))
            self.btn_file_actions = QPushButton(tr("File actions"))
            menu = QMenu(self.btn_file_actions)
            self.add_file_actions(menu, rows)
            self.btn_file_actions.setMenu(menu)
            self.btn_file_actions.setEnabled(any(a.isEnabled() for a in menu.actions()))
            self.detail_layout.addWidget(self.btn_file_actions)
            self.detail_bar.show()
            return

        row = rows[0]
        name = QLabel(f"<b>{row.src.name}</b>" + (f" · {row.note}" if row.note else ""))
        name.setTextFormat(Qt.RichText)
        self.detail_layout.addWidget(name, stretch=1)

        src_ok, dst_ok = row.src.exists(), row.dst.exists() and row.out_size is not None
        if row.status == "planned":
            first = bool(self.moved_up) and self.moved_up[0] == str(row.src)
            add(tr("Process next"), lambda: self.move_up(row), not first)
        add(tr("Inspect different compressions"), lambda: self.open_lab(row), src_ok)
        if src_ok and dst_ok and row.status != "running":  # the result is being rewritten
            add(tr("Compare"), lambda: self.compare(row))
        elif src_ok and not dst_ok:
            add(tr("Play original"), lambda: self.compare(row))
        add(tr("Show original"), lambda: open_in_finder(row.src), src_ok)
        if dst_ok:
            add(tr("Show result"), lambda: open_in_finder(row.dst))
        if row.log:
            add(tr("Log"), lambda: self.show_log(row))
        if dst_ok:
            self.btn_file_actions = QPushButton(tr("File actions"))
            menu = QMenu(self.btn_file_actions)
            self.add_file_actions(menu, row)
            self.btn_file_actions.setMenu(menu)
            self.btn_file_actions.setEnabled(row.status != "running")
            self.detail_layout.addWidget(self.btn_file_actions)
        self.detail_bar.show()

    def on_row_double_clicked(self, index):
        row = index.data(Qt.UserRole + 1)
        if row.src.exists() and row.has_result and row.status != "running":
            self.compare(row)
        elif row.log or not row.src.exists():
            self.show_log(row)
        else:
            self.compare(row)  # nothing to compare with yet: just play the original

    def open_lab(self, row):
        """Opens the compression lab for this video; the drawer's settings are its starting point."""
        seconds, _, count = self.queue_video_seconds()
        self.lab_dialog = CompressionLabDialog(row.src, row.duration, self.current_run_settings(),
                                               self.apply_run_settings, self, queue=(count, seconds))
        self.lab_dialog.show()

    def compare(self, row):
        has_result = row.dst.exists() and row.out_size is not None
        self.thumbnails.pause()   # the viewer needs the disk and the CPU for smooth playback
        try:
            CompareVideoDialog(row.src, row.dst if has_result else None, self).exec()
        finally:
            self.thumbnails.resume()

    def show_log(self, row):
        self.log_dialog = LogDialog(row, self)
        self.log_dialog.show()

    def handle_compare_action(self, action, rows):
        """Replace/swap/delete for one row or a list of rows, after one confirmation; rows it does not apply to are left out."""
        rows = [row for row in (rows if isinstance(rows, list) else [rows]) if self.file_action_applies(action, row)]
        if not rows:
            return
        if len(rows) == 1:
            name = rows[0].src.name if action != "del_comp" else rows[0].dst.name
            title, text = {
                "overwrite": (tr("Overwrite"), tr("Replace the original '{name}' with the compressed version? The original goes to the trash.", name=name)),
                "swap": (tr("Swap"), tr("Really swap the original and the compressed version?")),
                "del_orig": (tr("Move to trash"), tr("Move the original '{name}' to the trash?", name=name)),
                "del_comp": (tr("Move to trash"), tr("Move the compressed version '{name}' to the trash?", name=name)),
            }[action]
        else:
            count = len(rows)
            title, text = {
                "overwrite": (tr("Overwrite"), tr("Replace {count} originals with their compressed versions? The originals go to the trash.", count=count)),
                "swap": (tr("Swap"), tr("Really swap original and compressed version of {count} videos?", count=count)),
                "del_orig": (tr("Move to trash"), tr("Move {count} originals to the trash?", count=count)),
                "del_comp": (tr("Move to trash"), tr("Move {count} compressed versions to the trash?", count=count)),
            }[action]
        if QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        failed = []
        for row in rows:
            try:
                self.apply_file_action(action, row)
            except OSError as e:
                failed.append(f"{row.src.name}: {e}" if len(rows) > 1 else str(e))
        if action == "del_comp" and any(row.src.exists() for row in rows):
            self.schedule_scan()
        if failed:
            QMessageBox.critical(self, tr("Error"), tr("The action failed: {error}", error="\n".join(failed)))
        self.update_detail_bar()

    def apply_file_action(self, action, row):
        orig_path, comp_path = row.src, row.dst
        if action == "overwrite":
            new_orig_path = orig_path.with_name(comp_path.name)
            if new_orig_path.exists() and new_orig_path.samefile(comp_path):   # result already next to it
                move_to_trash(orig_path)
            else:
                # Copied under a temporary name first: the original (and any other file with the result's name)
                # only goes to the trash once the copy is complete, and is never overwritten in place, even
                # when the result has the very same name.
                temp_path = new_orig_path.with_name(f".{new_orig_path.name}.replacing")
                shutil.copy2(comp_path, temp_path)
                try:
                    move_to_trash(orig_path)
                    if new_orig_path.exists():
                        move_to_trash(new_orig_path)
                except OSError:
                    temp_path.unlink(missing_ok=True)
                    raise
                temp_path.replace(new_orig_path)
            self.model.update(row.src, note=tr("original replaced"))
        elif action == "swap":
            temp_path = orig_path.with_name(orig_path.name + ".tmp")
            shutil.move(orig_path, temp_path)
            shutil.move(comp_path, orig_path)
            shutil.move(temp_path, comp_path)
            self.model.update(row.src, note=tr("swapped"))
        elif action == "del_orig":
            move_to_trash(orig_path)
            self.model.update(row.src, note=tr("original in the trash"))
        elif action == "del_comp":
            move_to_trash(comp_path)
            self.model.update(row.src, note=tr("result in the trash"), out_size=None,
                              status="planned" if orig_path.exists() else row.status)

    # ------------------------------------------------------------------ files
    def clear_cache(self):
        freed = clear_caches()
        QMessageBox.information(self, tr("Cache"), tr("Cache cleared ({size} freed). Thumbnails and durations are read again at the next scan.", size=format_size(freed)))

    def export_logs(self):
        file_path, _ = QFileDialog.getSaveFileName(self, tr("Export list as CSV"), "compression_log.csv", tr("CSV files (*.csv);;All files (*)"))
        if not file_path:
            return
        try:
            with open(file_path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["file", "folder", "status", "duration_s", "original_bytes", "result_bytes", "saved_bytes", "ratio"])
                for row in self.model.rows:
                    saved = row.size - row.out_size if row.out_size is not None else ""
                    writer.writerow([row.src.name, row.folder, row.status, f"{row.duration:.1f}" if row.duration else "",
                                     row.size, row.out_size if row.out_size is not None else "", saved,
                                     f"{row.ratio:.3f}" if row.ratio is not None else ""])
            QMessageBox.information(self, tr("Success"), tr("Log exported."))
        except OSError as e:
            QMessageBox.critical(self, tr("Error"), tr("Export failed: {error}", error=e))

    def save_defaults(self):
        res, fps = self.combo_res.currentData(), self.combo_fps.currentData()
        defaults = {
            'src_dir': self.folder_source_text if self.selection is not None else self.txt_src.text(),
            'dst_dir': self.folder_target_text if self.selection is not None else self.txt_dst.text(),
            'limit_res': res is not None,
            'max_res': str(res or 1920),
            'limit_fps': fps is not None,
            'max_fps': str(fps or 30),
            'renderer_index': self.combo_renderer.currentIndex(),
            'max_jobs': self.spin_jobs.value(),
            'copy_aac': self.cb_copy_aac.isChecked(),
            'crf': self.slider_crf.value(),
            'preset': self.combo_preset.currentText(),
            'vt_quality': self.slider_vt.value(),
            'flatten': self.cb_flatten.isChecked(),
            'postfix': self.txt_postfix.text(),
            'excludes': self.txt_excludes.text(),
            'skip_hidden': self.cb_skip_hidden.isChecked(),
            'overwrite': self.cb_overwrite.isChecked(),
            'low_priority': self.cb_low_priority.isChecked(),
            'keep_awake': self.cb_keep_awake.isChecked(),
            'dry_run': self.cb_dry_run.isChecked(),
        }
        try:
            path = settings_file()
            settings = {}
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
            settings['compressor'] = defaults
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
            QMessageBox.information(self, tr("Success"), tr("Default settings saved."))
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, tr("Error"), tr("Saving the settings failed: {error}", error=e))

    def load_defaults(self):
        path = settings_file()
        if not path.exists():
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                defaults = json.load(f).get('compressor', {})
        except (OSError, json.JSONDecodeError) as e:
            print(f"Failed to load settings: {e}")
            return

        def pick(combo, choices, value):
            if value in choices:
                combo.setCurrentIndex(choices.index(value))

        if 'limit_res' in defaults or 'max_res' in defaults:
            pick(self.combo_res, RESOLUTIONS, int(defaults.get('max_res', 1920)) if defaults.get('limit_res', True) else None)
        if 'limit_fps' in defaults or 'max_fps' in defaults:
            pick(self.combo_fps, FRAME_RATES, int(defaults.get('max_fps', 30)) if defaults.get('limit_fps', True) else None)
        if 'renderer_index' in defaults: self.combo_renderer.setCurrentIndex(defaults['renderer_index'])
        if 'max_jobs' in defaults: self.spin_jobs.setValue(defaults['max_jobs'])
        if 'copy_aac' in defaults: self.cb_copy_aac.setChecked(defaults['copy_aac'])
        if 'crf' in defaults: self.slider_crf.setValue(defaults['crf'])
        if 'preset' in defaults: self.combo_preset.setCurrentText(defaults['preset'])
        if 'vt_quality' in defaults: self.slider_vt.setValue(defaults['vt_quality'])
        if 'flatten' in defaults: self.cb_flatten.setChecked(defaults['flatten'])
        if 'postfix' in defaults: self.txt_postfix.setText(defaults['postfix'])
        if 'excludes' in defaults: self.txt_excludes.setText(defaults['excludes'])
        if 'skip_hidden' in defaults: self.cb_skip_hidden.setChecked(defaults['skip_hidden'])
        if 'overwrite' in defaults: self.cb_overwrite.setChecked(defaults['overwrite'])
        if 'low_priority' in defaults: self.cb_low_priority.setChecked(defaults['low_priority'])
        if 'keep_awake' in defaults: self.cb_keep_awake.setChecked(defaults['keep_awake'])
        if 'dry_run' in defaults: self.cb_dry_run.setChecked(defaults['dry_run'])
        # Paths last: setting them schedules the automatic scan.
        if 'src_dir' in defaults: self.txt_src.setText(defaults['src_dir'])
        if 'dst_dir' in defaults: self.txt_dst.setText(defaults['dst_dir'])
