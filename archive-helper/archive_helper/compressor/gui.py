import csv
import json
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Slot
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMessageBox, QProgressBar, QPushButton, QStyle, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from video_helper_core.style import STYLE, caption
from video_helper_core.i18n import tr
from video_helper_core.paths import settings_file
from video_helper_core.keep_awake import KeepAwake
from video_helper_core.notify import notify
from .model import (
    COL_DURATION, COL_FILE, COL_RESULT, COL_SETTINGS, COL_SIZE, COL_STATUS, COL_THUMB,
    FileDelegate, StatusDelegate, finished_problems, ThumbnailDelegate, VideoFilterProxy, VideoRow, VideoTableModel,
)
from .utils import (
    EncodeSpeeds, check_dependencies, clean_postfix, clear_caches, collect_videos, common_parent,
    format_duration_long, format_size, parse_excludes, prune_thumbnails,
)
from .drawer import SettingsDrawer
from .row_actions import RowActions
from .run_control import RunControl
from .widgets import DropLineEdit, DropOverlay, dropped_paths
from .workers import DurationProbeWorker, ResultSettingsWorker, ThumbnailLoader, UnifiedScanWorker

def has_status(*statuses):
    return lambda row: row.status in statuses


# The filter chips: key -> which rows they show. "session" and "exists" split "done"; "problem" is what is worth a
# look at among the finished ones (metadata missing, visible blocks, larger than the original).
STATUS_FILTERS = [
    ("all", None), ("planned", has_status("planned")), ("running", has_status("running")),
    ("done", has_status("done", "exists")), ("session", has_status("done")), ("exists", has_status("exists")),
    ("problem", lambda row: bool(finished_problems(row))), ("skipped", has_status("skipped")),
    ("error", has_status("error")),
]
DONE_PARTS = {"session", "exists"}   # shown only when "done" holds both kinds


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


class ArchiverGUI(SettingsDrawer, RunControl, RowActions, QWidget):
    """The compressor page. Its parts live in mixins: the settings drawer (drawer.py), the run (run_control.py) and
    what can be done with the listed videos (row_actions.py); this file holds the layout, the scan and the summary."""
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
        self.undo_stack, self.redo_stack = [], []   # file actions (see row_actions)
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
        self.thumbnail_count = QLabel()   # how many previews are loaded, while they load
        self.thumbnail_count.setProperty("role", "hint")
        self.thumbnail_count.hide()
        bar.addWidget(self.thumbnail_count)
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


    DRAWER_WIDTH = 340


    # ------------------------------------------------------------ settings UI


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
            self.settings_worker.stop()   # results of the old scan are discarded
            self.settings_worker.wait()
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


    @Slot(str, object)
    def on_thumbnail_ready(self, path, image):
        self.model.set_thumbnail(path, QPixmap.fromImage(image) if image is not None and not image.isNull() else None)
        self.request_summary()   # the count of loaded previews

    @Slot(str, object)
    def on_settings_found(self, path, settings):
        self.model.update(path, settings=settings)

    @Slot(str, float)
    def on_duration_found(self, path, duration):
        self.model.update(path, duration=duration)
        if self.is_archiving() and self.table.horizontalHeader().sortIndicatorSection() == COL_DURATION:
            self.requeue_timer.start()


    # ---------------------------------------------------------------- summary
    def refresh_summary(self):
        rows = self.model.rows
        labels = {
            "all": tr("All"), "planned": tr("Planned"), "running": tr("Running"), "done": tr("Done"),
            "session": tr("Done in this session"), "exists": tr("Already archived"), "problem": tr("⚠ With problems"),
            "skipped": tr("Skipped"), "error": tr("Errors"),
        }
        counts = {key: len(rows) if test is None else sum(1 for r in rows if test(r)) for key, test in STATUS_FILTERS}
        for key, _ in STATUS_FILTERS:
            button = self.filter_buttons[key]
            button.setText(f"{labels[key]}  {counts[key]}")
            visible = 0 < counts[key] < counts["done"] if key in DONE_PARTS else key == "all" or counts[key] > 0
            button.setVisible(visible or button.isChecked())   # the chosen filter stays, even at 0

        tried = sum(r.thumbnail_tried for r in rows)
        self.thumbnail_count.setText(tr("Previews {loaded} / {total}", loaded=tried, total=len(rows)))
        self.thumbnail_count.setVisible(0 < len(rows) and tried < len(rows))

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


