"""Compression lab: try several compression settings on a short part of one video and compare the results.

Each variant is encoded for the chosen part only (same ffmpeg call as the real run, see
ArchiveWorker.build_ffmpeg_command), so the size of the whole video can be estimated and one frame of every
variant can be looked at next to the original, at 100 % or 200 %, with the views scrolling together.
"""
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QGridLayout,
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from video_helper_core.i18n import tr
from video_helper_core.style import STYLE, caption
from .options import FRAME_RATES, QUALITY_PRESETS, RENDERERS, RESOLUTIONS, X265_PRESETS
from .utils import (
    EncodeSpeeds, format_duration_long, format_size, get_display_rotation, get_hdr_color, get_resolution_and_fps, get_video_info,
    parse_ffmpeg_time,
)
from .workers import ArchiveWorker

MAX_VARIANTS = 8
DEFAULT_VARIANTS = 3
DEFAULT_SEGMENT_SECONDS = 10
MIN_COLUMN_WIDTH = 300
ZOOM_LEVELS = [0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0]
FRAME_STEPS = [-10, -5, -1, 1, 5, 10]


def estimate_total_size(segment_bytes, segment_seconds, total_seconds):
    """The size the whole video would get at this bitrate (the part is assumed to be typical)."""
    if not segment_seconds or not total_seconds:
        return None
    return int(segment_bytes * total_seconds / segment_seconds)


def default_segment(duration, length=DEFAULT_SEGMENT_SECONDS):
    """(start, length) in seconds: the first seconds of the video."""
    if duration:
        length = int(min(length, max(1, duration)))
    return 0, length


def next_zoom(current, direction):
    """The zoom one step in or out (direction +1 / -1); from "fit" (None) the first step is 100 % in, 50 % out."""
    if current is None:
        return 1.0 if direction > 0 else 0.5
    levels = [z for z in ZOOM_LEVELS if (z > current + 1e-9 if direction > 0 else z < current - 1e-9)]
    if not levels:
        return current
    return min(levels) if direction > 0 else max(levels)


def step_position(position, frames, fps, length):
    """The frame position (seconds into the part) after moving by `frames`, kept inside the part."""
    fps = fps or 25
    last = max(0.0, length - 1 / fps)
    return round(min(last, max(0.0, position + frames / fps)), 4)


def display_size(width, height, rotation):
    """Size of the picture as shown: portrait phone clips are stored as landscape and carry a rotation."""
    return (height, width) if rotation % 180 == 90 else (width, height)


def next_variant(settings):
    """A further variant: the last one with better quality (lower CRF, higher VideoToolbox quality)."""
    more = dict(settings)
    more['crf'] = max(0, settings['crf'] - 3)
    more['vt_quality'] = min(100, settings['vt_quality'] + 10)
    return more


def estimate_remaining(finished, running_progress, running_elapsed, waiting):
    """Seconds still to wait, or None while nothing has been measured. finished: encoding times of the finished
    variants; running_progress: 0..1 of the running variant (None if none runs); waiting: variants not started yet.
    A variant is assumed to need about as long as the ones measured so far."""
    measured = list(finished)
    running_total = None
    if running_progress is not None and running_progress > 0.02 and running_elapsed > 0:
        running_total = running_elapsed / running_progress
        measured.append(running_total)
    if not measured:
        return None
    typical = sum(measured) / len(measured)
    if running_progress is None:
        left = 0.0
    else:
        left = (running_total if running_total is not None else typical) - running_elapsed
    return max(0.0, left) + waiting * typical


def variant_defaults(base):
    """Settings of the variants: the drawer's settings with the quality of Small, Archive and High."""
    variants = []
    for crf, vt in QUALITY_PRESETS.values():
        variants.append({**base, 'crf': crf, 'vt_quality': vt, 'overwrite': False, 'dry_run': False,
                         'low_priority': False})
    return variants[:DEFAULT_VARIANTS]


def kill_process(process):
    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


class LabWorker(QThread):
    """Encodes the given variants one after the other. jobs: list of (uid, settings). Signals: variant_started(uid),
    variant_progress(uid, percent), variant_done(uid, path, bytes, seconds), variant_failed(uid, message)."""
    variant_started = Signal(int)
    variant_progress = Signal(int, int)
    variant_done = Signal(int, str, int, float)
    variant_failed = Signal(int, str)

    def __init__(self, src, segment, jobs, outdir, src_fps, parent=None):
        super().__init__(parent)
        self.src, self.segment, self.jobs = Path(src), segment, list(jobs)
        self.outdir, self.src_fps = Path(outdir), src_fps
        self._stopped = False
        self.process = None

    def stop(self):
        self._stopped = True
        if self.process is not None:
            kill_process(self.process)

    def run(self):
        for index, settings in self.jobs:
            if self._stopped:
                return
            self.variant_started.emit(index)
            out = self.outdir / f"variant_{index}.mp4"
            out.unlink(missing_ok=True)
            try:
                command = ArchiveWorker(self.src.parent, self.outdir, 1, [], settings).build_ffmpeg_command(
                    self.src, out, self.src_fps, self.segment)
                started = time.monotonic()
                self.process = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    preexec_fn=os.setsid if sys.platform != "win32" else None)
                if self._stopped:
                    # stopped while the command was being prepared (ffprobe): stop() found no process to kill yet
                    kill_process(self.process)
                last_line = self.read_progress(index)
            except Exception as e:
                self.variant_failed.emit(index, str(e))
                continue
            process, self.process = self.process, None
            process.wait()
            if self._stopped:
                return
            if process.returncode != 0 or not out.exists():
                self.variant_failed.emit(index, last_line or f"ffmpeg exit code {process.returncode}")
            else:
                self.variant_progress.emit(index, 100)
                self.variant_done.emit(index, str(out), out.stat().st_size, time.monotonic() - started)

    def read_progress(self, index):
        """Reads ffmpeg's status lines (separated by carriage returns) and reports how much of the part is done."""
        length = self.segment[1] or 1
        buffer, last_line, last_percent = b"", "", -1
        while True:
            chunk = self.process.stderr.read(512)
            if not chunk:
                return last_line
            buffer += chunk
            *lines, buffer = buffer.replace(b"\r", b"\n").split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                last_line = line
                seconds = parse_ffmpeg_time(line)
                if seconds is not None:
                    percent = min(99, int(seconds / length * 100))
                    if percent > last_percent:   # the time stamps of audio and video can jump back a little
                        last_percent = percent
                        self.variant_progress.emit(index, percent)


class FrameWorker(QThread):
    """Takes one frame out of each given video. jobs: list of (key, path, seconds, size or None, hdr); size scales the
    frame (so variants that were encoded smaller are shown at the size of the original); for HDR (BT.2020) sources the
    colour matrix is given, otherwise the colours would be read as BT.601. frame_ready(key, QImage)."""
    frame_ready = Signal(str, QImage)

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        for key, path, seconds, size, hdr in self.jobs:
            if self._stopped:
                return
            command = ["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1"]
            scale = []
            if size:
                scale.append(f"scale={size[0]}:{size[1]}:flags=bicubic")
            if hdr:
                # No tone mapping (this ffmpeg may lack zscale): the frame keeps the HDR brightness curve, so it looks
                # a little flat, but the colours are decoded with the right matrix.
                scale = [f"scale={size[0]}:{size[1]}:flags=bicubic:in_color_matrix=bt2020" if size
                         else "scale=in_color_matrix=bt2020"]
            if scale:
                command += ["-vf", ",".join(scale)]
            command += ["-f", "image2pipe", "-vcodec", "png", "-"]
            try:
                data = subprocess.run(command, capture_output=True, timeout=120).stdout
            except (OSError, subprocess.TimeoutExpired):
                continue
            image = QImage.fromData(data) if data else QImage()
            if not image.isNull() and not self._stopped:
                self.frame_ready.emit(key, image)


class ZoomImageView(QGraphicsView):
    """One frame that can be shown fitted or at a fixed zoom, turned by 90° steps, and dragged around."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.SmoothPixmapTransform)
        self.setMinimumSize(220, 160)
        self.zoom = None   # None = fit into the view
        self.angle = 0
        self.setStyleSheet("background: #1d1d1f; border: none;")

    def set_image(self, image):
        self.item.setPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(self.item.boundingRect())
        self.apply_zoom()

    def has_image(self):
        return not self.item.pixmap().isNull()

    def clear_image(self):
        self.item.setPixmap(QPixmap())

    def set_zoom(self, zoom):
        self.zoom = zoom
        self.apply_zoom()

    def set_angle(self, angle):
        self.angle = angle % 360
        self.apply_zoom()

    def apply_zoom(self):
        self.resetTransform()
        if not self.has_image():
            return
        self.rotate(self.angle)
        if self.zoom is None:
            self.fitInView(self.item, Qt.KeepAspectRatio)
        else:
            self.scale(self.zoom, self.zoom)

    def shown_rect(self):
        """The picture's area in view coordinates (for tests and for fitting)."""
        return self.mapFromScene(self.item.sceneBoundingRect()).boundingRect()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.zoom is None:
            self.apply_zoom()


class VariantColumn(QFrame):
    """The settings of one variant, its progress and its result. The frames are shown in a row of their own below all
    columns, so every view has the same size and the views scroll together exactly."""

    changed = Signal()
    _uids = itertools.count(1)

    def __init__(self, title, settings, parent=None):
        super().__init__(parent)
        self.uid = next(self._uids)
        self.setObjectName("detailBar")
        self.setMinimumWidth(MIN_COLUMN_WIDTH)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        head = QHBoxLayout()
        self.title = caption(title)
        head.addWidget(self.title, 1)
        self.remove_button = QPushButton("×")
        self.remove_button.setToolTip(tr("Remove this variant"))
        self.remove_button.setFixedWidth(28)
        head.addWidget(self.remove_button)
        layout.addLayout(head)

        self.combo_encoder = QComboBox()
        self.combo_encoder.addItem(tr("CPU (libx265)"), RENDERERS[0])
        self.combo_encoder.addItem(tr("Mac GPU (VideoToolbox)"), RENDERERS[1])
        self.spin_quality = QSpinBox()
        self.combo_preset = QComboBox()
        self.combo_preset.addItems(X265_PRESETS)
        self.combo_res = QComboBox()
        for value in RESOLUTIONS:
            self.combo_res.addItem(tr("No limit") if value is None else f"{value} px", value)
        self.combo_fps = QComboBox()
        for value in FRAME_RATES:
            self.combo_fps.addItem(tr("No limit") if value is None else f"{value} fps", value)
        self.quality_label = QLabel()
        self.preset_label = QLabel(tr("Preset"))
        for label, widget in ((None, self.combo_encoder), (self.quality_label, self.spin_quality),
                              (self.preset_label, self.combo_preset), (QLabel(tr("Resolution")), self.combo_res),
                              (QLabel(tr("Frame rate")), self.combo_fps)):
            row = QHBoxLayout()
            if label is not None:
                label.setMinimumWidth(80)
                row.addWidget(label)
            row.addWidget(widget, 1)
            layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        layout.addWidget(self.progress)
        self.result = QLabel(tr("Not encoded yet"))
        self.result.setProperty("role", "hint")
        self.result.setWordWrap(True)
        self.result.setMinimumHeight(self.result.fontMetrics().lineSpacing() * 4)
        self.result.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.result)
        self.use_button = QPushButton(tr("Use these settings"))
        layout.addWidget(self.use_button)

        self.combo_encoder.currentIndexChanged.connect(self.encoder_changed)
        self.set_settings(settings)
        for signal in (self.combo_encoder.currentIndexChanged, self.spin_quality.valueChanged,
                       self.combo_preset.currentIndexChanged, self.combo_res.currentIndexChanged,
                       self.combo_fps.currentIndexChanged):
            signal.connect(lambda *_: self.changed.emit())

    def settings_key(self):
        """Identifies the settings (for telling whether a variant has to be encoded again)."""
        return json.dumps(self.settings(), sort_keys=True)

    def set_title(self, text):
        self.title.setText(text.upper())

    def reset_result(self):
        self.progress.setValue(0)
        self.result.setText(tr("Not encoded yet"))

    def encoder_changed(self):
        cpu = self.combo_encoder.currentData() == RENDERERS[0]
        self.quality_label.setText(tr("CRF (lower = better)") if cpu else tr("Quality (higher = better)"))
        self.spin_quality.setRange(0, 51) if cpu else self.spin_quality.setRange(1, 100)
        self.preset_label.setVisible(cpu)
        self.combo_preset.setVisible(cpu)

    def set_settings(self, settings):
        self.combo_encoder.setCurrentIndex(self.combo_encoder.findData(settings['renderer']))
        self.encoder_changed()
        self.spin_quality.setValue(settings['crf'] if settings['renderer'] == RENDERERS[0] else settings['vt_quality'])
        self.combo_preset.setCurrentText(settings['preset'])
        res = settings['max_res'] if settings['limit_res'] else None
        fps = settings['max_fps'] if settings['limit_fps'] else None
        self.combo_res.setCurrentIndex(max(0, self.combo_res.findData(res)))
        self.combo_fps.setCurrentIndex(max(0, self.combo_fps.findData(fps)))
        self.base = dict(settings)

    def settings(self):
        cpu = self.combo_encoder.currentData() == RENDERERS[0]
        res, fps = self.combo_res.currentData(), self.combo_fps.currentData()
        settings = dict(self.base)
        settings.update(
            renderer=self.combo_encoder.currentData(), preset=self.combo_preset.currentText(),
            limit_res=res is not None, max_res=res or 0, limit_fps=fps is not None, max_fps=fps or 0)
        settings['crf' if cpu else 'vt_quality'] = self.spin_quality.value()
        return settings


class CompressionLabDialog(QDialog):
    """Opens for one video. apply_settings(settings) is called when the user takes a variant's settings.
    queue=(number of videos, seconds of video) of the folder to be compressed, for the time estimate of the whole run."""

    def __init__(self, src, duration, base_settings, apply_settings, parent=None, queue=None):
        super().__init__(parent)
        self.setStyleSheet(STYLE)
        self.setWindowTitle(tr("Inspect different compressions: {name}", name=Path(src).name))
        self.resize(1500, 860)
        self.src = Path(src)
        self.apply_settings = apply_settings
        self.queue = queue
        self.speeds = EncodeSpeeds()
        probed_duration, self.fps, _ = get_video_info(self.src)
        self.duration = duration or probed_duration
        width, height, source_fps = get_resolution_and_fps(self.src)
        self.rotation = get_display_rotation(self.src)
        self.frame_size = display_size(int(width), int(height), self.rotation) if width and height else None
        self.source_fps = source_fps or self.fps
        self.hdr = bool(get_hdr_color(self.src))
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vht-lab-"))
        self.lab_worker = self.frame_worker = None
        self.closed = False
        self.variant_files = {}      # uid -> path of the encoded part
        self.results = {}            # uid -> (bytes, seconds)
        self.result_text = {}        # uid -> text shown when the variant is up to date
        self.encoded = {}            # uid -> (settings key, part) it was encoded with
        self.encoding_info = {}      # uid -> (settings key, settings, part) of the running job
        self.to_encode = []          # uids of the current run
        self.finished_seconds = []   # encoding times of the variants done in this run
        self.running = None          # (uid, started at, progress 0..1)
        self.frame_position = 0.0    # seconds into the part
        self.angle = 0
        self.current_zoom = None
        self.frame_timer = QTimer(self, singleShot=True, interval=250)
        self.frame_timer.timeout.connect(self.load_frames)
        self.syncing = False

        layout = QVBoxLayout(self)
        layout.addLayout(self.build_controls())
        layout.addLayout(self.build_view_controls())

        # One grid for the settings and the frames, in a scroll area: with many variants it scrolls sideways.
        self.grid = QGridLayout()
        content = QWidget()
        content.setLayout(self.grid)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)

        self.original = self.build_original_column()
        self.original_view = self.new_view()
        self.columns, self.column_views = [], []
        for settings in variant_defaults(base_settings):
            self.add_column(settings)
        self.arrange()

        self.status = QLabel(tr("Choose the part and the settings, then press “Encode”."))
        self.status.setProperty("role", "hint")
        layout.addWidget(self.status)
        self.update_frame_label()
        self.load_frames()

    # ---------------------------------------------------------------------------------- layout
    @property
    def views(self):
        return [self.original_view] + self.column_views

    def build_controls(self):
        row = QHBoxLayout()
        start, length = default_segment(self.duration)
        limit = int(self.duration) if self.duration else 3600
        row.addWidget(QLabel(tr("Part of the video: from second")))
        self.spin_start = QSpinBox()
        self.spin_start.setRange(0, max(0, limit - 1))
        self.spin_start.setValue(start)
        self.spin_start.setMinimumWidth(80)
        row.addWidget(self.spin_start)
        row.addWidget(QLabel(tr("length (seconds)")))
        self.spin_length = QSpinBox()
        self.spin_length.setRange(1, max(1, min(3600, limit)))
        self.spin_length.setValue(length)
        self.spin_length.setMinimumWidth(80)
        row.addWidget(self.spin_length)
        for spin in (self.spin_start, self.spin_length):
            spin.valueChanged.connect(self.part_changed)
        self.btn_encode = QPushButton(tr("Encode"))
        self.btn_encode.clicked.connect(self.encode)
        self.btn_cancel = QPushButton(tr("Cancel all"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.stop_encoding)
        self.btn_add = QPushButton(tr("+ Variant"))
        self.btn_add.clicked.connect(self.add_variant)
        for button in (self.btn_encode, self.btn_cancel, self.btn_add):
            row.addWidget(button)
        self.estimate_label = QLabel()
        self.estimate_label.setProperty("role", "hint")
        row.addWidget(self.estimate_label)
        row.addStretch()
        return row

    def build_view_controls(self):
        """Which frame is shown, how large, and turned how."""
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Frame")))
        self.slider_frame = QSlider(Qt.Horizontal)
        self.slider_frame.setRange(0, 1000)
        self.slider_frame.setFixedWidth(220)
        self.slider_frame.setValue(0)
        self.slider_frame.valueChanged.connect(self.slider_moved)
        row.addWidget(self.slider_frame)
        self.step_buttons = []
        for frames in FRAME_STEPS:
            button = QPushButton(f"{frames:+d}")
            button.setToolTip(tr("{count} frames", count=f"{frames:+d}"))
            button.setFixedWidth(40)
            button.clicked.connect(lambda _=False, n=frames: self.step_frame(n))
            self.step_buttons.append(button)
            row.addWidget(button)
        self.frame_label = QLabel()
        self.frame_label.setProperty("role", "hint")
        row.addWidget(self.frame_label)
        row.addStretch()

        row.addWidget(QLabel(tr("Zoom")))
        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.clicked.connect(lambda: self.set_zoom(next_zoom(self.current_zoom, -1)))
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.clicked.connect(lambda: self.set_zoom(next_zoom(self.current_zoom, +1)))
        for button in (self.btn_zoom_out, self.btn_zoom_in):
            button.setFixedWidth(34)
        row.addWidget(self.btn_zoom_out)
        self.zoom_group = QButtonGroup(self)
        self.zoom_group.setExclusive(False)
        self.zoom_buttons = {}
        for text, zoom in ((tr("Fit"), None), ("50 %", 0.5), ("100 %", 1.0), ("200 %", 2.0)):
            button = QPushButton(text)
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, z=zoom: self.set_zoom(z))
            self.zoom_group.addButton(button)
            self.zoom_buttons[zoom] = button
            row.addWidget(button)
        row.addWidget(self.btn_zoom_in)
        self.btn_rotate = QPushButton("⟳")
        self.btn_rotate.setToolTip(tr("Rotate by 90°"))
        self.btn_rotate.setFixedWidth(34)
        self.btn_rotate.clicked.connect(lambda: self.set_angle(self.angle + 90))
        row.addWidget(self.btn_rotate)
        self.update_zoom_buttons()
        return row

    def build_original_column(self):
        frame = QFrame(objectName="detailBar")
        frame.setMinimumWidth(MIN_COLUMN_WIDTH)
        layout = QVBoxLayout(frame)
        layout.addWidget(caption(tr("Original")))
        size = self.src.stat().st_size if self.src.exists() else 0
        parts = [format_size(size)]
        if self.frame_size:
            parts.append(f"{self.frame_size[0]} × {self.frame_size[1]}")
        if self.source_fps:
            parts.append(f"{self.source_fps:g} fps")
        if self.hdr:
            parts.append("HDR")
        info = QLabel(" · ".join(parts))
        info.setProperty("role", "hint")
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch(1)
        return frame

    def new_view(self):
        view = ZoomImageView()
        view.horizontalScrollBar().valueChanged.connect(lambda value, v=view: self.sync_scroll(v, "horizontalScrollBar", value))
        view.verticalScrollBar().valueChanged.connect(lambda value, v=view: self.sync_scroll(v, "verticalScrollBar", value))
        view.zoom, view.angle = self.current_zoom, self.angle
        return view

    def sync_scroll(self, source, bar_name, value):
        """Moving one view moves them all, so the same spot of every variant is compared."""
        if self.syncing:
            return
        self.syncing = True
        try:
            for view in self.views:
                if view is not source:
                    getattr(view, bar_name)().setValue(value)
        finally:
            self.syncing = False

    def add_column(self, settings):
        column = VariantColumn("", settings)
        column.use_button.clicked.connect(lambda _=False, c=column: self.use_variant(self.columns.index(c)))
        column.remove_button.clicked.connect(lambda _=False, c=column: self.remove_variant(self.columns.index(c)))
        column.changed.connect(self.refresh_changed)
        self.columns.append(column)
        self.column_views.append(self.new_view())

    def arrange(self):
        """Puts the original, the variants and their frames into the grid and renames the variants A, B, C…"""
        while self.grid.count():
            self.grid.takeAt(0)
        panels = [self.original] + self.columns
        for position, (panel, view) in enumerate(zip(panels, self.views)):
            self.grid.addWidget(panel, 0, position)
            self.grid.addWidget(view, 1, position)
            self.grid.setColumnMinimumWidth(position, MIN_COLUMN_WIDTH)
            self.grid.setColumnStretch(position, 1)
        self.grid.setRowStretch(1, 1)
        for index, column in enumerate(self.columns):
            column.set_title(f"{tr('Variant')} {chr(65 + index)}")
            column.remove_button.setVisible(len(self.columns) > 1)
        self.btn_add.setEnabled(len(self.columns) < MAX_VARIANTS and not self.encoding())

    def add_variant(self):
        if self.encoding() or len(self.columns) >= MAX_VARIANTS:
            return
        self.add_column(next_variant(self.columns[-1].settings()))
        self.arrange()
        self.scroll.horizontalScrollBar().setValue(self.scroll.horizontalScrollBar().maximum())

    def remove_variant(self, index):
        if self.encoding() or len(self.columns) <= 1:
            return
        column, view = self.columns.pop(index), self.column_views.pop(index)
        for table in (self.variant_files, self.results, self.result_text, self.encoded):
            table.pop(column.uid, None)
        column.hide()
        view.hide()
        column.deleteLater()
        view.deleteLater()
        self.arrange()

    # ---------------------------------------------------------------------------------- view controls
    def set_zoom(self, zoom):
        self.current_zoom = zoom
        for view in self.views:
            view.set_zoom(zoom)
        self.update_zoom_buttons()

    def update_zoom_buttons(self):
        for zoom, button in self.zoom_buttons.items():
            button.setChecked(zoom == self.current_zoom)
        self.btn_zoom_in.setEnabled(self.current_zoom is None or self.current_zoom < ZOOM_LEVELS[-1])
        self.btn_zoom_out.setEnabled(self.current_zoom is None or self.current_zoom > ZOOM_LEVELS[0])

    def set_angle(self, angle):
        self.angle = angle % 360
        for view in self.views:
            view.set_angle(self.angle)

    def segment(self):
        return self.spin_start.value(), self.spin_length.value()

    def frame_seconds(self):
        """Second inside the part (0 .. length) of the chosen frame."""
        return self.frame_position

    def slider_moved(self, value):
        self.frame_position = round(self.segment()[1] * value / 1000, 3)
        self.update_frame_label()
        self.frame_timer.start()

    def step_frame(self, frames):
        self.frame_position = step_position(self.frame_position, frames, self.source_fps, self.segment()[1])
        self.sync_slider()
        self.update_frame_label()
        self.frame_timer.start()

    def sync_slider(self):
        length = self.segment()[1] or 1
        self.slider_frame.blockSignals(True)
        self.slider_frame.setValue(int(round(self.frame_position / length * 1000)))
        self.slider_frame.blockSignals(False)

    def update_frame_label(self):
        fps = self.source_fps or 25
        self.frame_label.setText(tr("{seconds:.2f} s into the part (frame {number})",
                                    seconds=self.frame_position, number=int(round(self.frame_position * fps))))

    def load_frames(self):
        if self.closed:
            return   # a timer or a finished encoding after closing: no new ffmpeg for a dialog that is gone
        if self.frame_worker is not None:
            self.frame_worker.stop()
            self.frame_worker.wait()
        start, _ = self.segment()
        seconds = self.frame_seconds()
        jobs = [("original", self.src, start + seconds, None, self.hdr)]
        for uid, path in sorted(self.variant_files.items()):
            jobs.append((str(uid), path, seconds, self.frame_size, self.hdr))
        self.frame_worker = FrameWorker(jobs, self)
        self.frame_worker.frame_ready.connect(self.on_frame_ready)
        self.frame_worker.start()

    def on_frame_ready(self, key, image):
        if key == "original":
            self.original_view.set_image(image)
            return
        for column, view in zip(self.columns, self.column_views):
            if column.uid == int(key):
                view.set_image(image)

    # ---------------------------------------------------------------------------------- encoding
    def encoding(self):
        return self.lab_worker is not None and self.lab_worker.isRunning()

    def column_by_uid(self, uid):
        return next((c for c in self.columns if c.uid == uid), None)

    def is_up_to_date(self, column):
        return self.encoded.get(column.uid) == (column.settings_key(), self.segment())

    def part_changed(self):
        """A different part makes every encoded variant out of date and moves the frame slider's range."""
        self.frame_position = min(self.frame_position, max(0.0, self.segment()[1] - 1 / (self.source_fps or 25)))
        self.sync_slider()
        self.update_frame_label()
        self.refresh_changed()

    def refresh_changed(self):
        """Marks variants whose settings or part differ from what was encoded; restores the result if changed back."""
        if self.encoding():
            return
        for column in self.columns:
            if column.uid not in self.encoded:
                continue
            column.result.setText(self.result_text[column.uid] if self.is_up_to_date(column)
                                  else tr("Changed – press “Encode” to update this variant."))

    def encode(self):
        """Encodes the variants that are new or changed; the others keep their result."""
        if self.closed:
            return
        if self.encoding():
            return
        pending = [c for c in self.columns if not self.is_up_to_date(c)]
        if not pending:
            self.status.setText(tr("Nothing to do: every variant is up to date."))
            return
        part = self.segment()
        self.finished_seconds = []
        self.running = None
        self.encoding_info = {}
        for column in pending:
            for table in (self.variant_files, self.results, self.result_text, self.encoded):
                table.pop(column.uid, None)
            column.reset_result()
            column.result.setText(tr("Waiting…"))
            self.column_views[self.columns.index(column)].clear_image()
            self.encoding_info[column.uid] = (column.settings_key(), column.settings(), part)
        self.to_encode = [c.uid for c in pending]
        self.btn_encode.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_add.setEnabled(False)
        for column in self.columns:
            column.remove_button.setEnabled(False)
        self.status.setText(tr("Encoding {count} variants of the part of the video…", count=len(pending)))
        jobs = [(c.uid, c.settings()) for c in pending]
        self.lab_worker = LabWorker(self.src, part, jobs, self.tmpdir, self.fps, self)
        self.lab_worker.variant_started.connect(self.on_variant_started)
        self.lab_worker.variant_progress.connect(self.on_variant_progress)
        self.lab_worker.variant_done.connect(self.on_variant_done)
        self.lab_worker.variant_failed.connect(self.on_variant_failed)
        self.lab_worker.finished.connect(self.on_encoding_finished)
        self.lab_worker.start()
        self.load_frames()   # the original at the new part
        self.show_estimate()

    def stop_encoding(self):
        if self.lab_worker is not None:
            self.lab_worker.stop()
            for column in self.columns:
                if column.uid in self.to_encode and column.uid not in self.results:
                    column.result.setText(tr("Cancelled"))
            self.status.setText(tr("Cancelled."))

    def on_variant_started(self, uid):
        column = self.column_by_uid(uid)
        if column:
            column.result.setText(tr("Encoding…"))
        self.running = (uid, time.monotonic(), 0.0)

    def on_variant_progress(self, uid, percent):
        column = self.column_by_uid(uid)
        if column:
            column.progress.setValue(percent)
        if self.running and self.running[0] == uid:
            self.running = (uid, self.running[1], percent / 100)
        self.show_estimate()

    def on_variant_done(self, uid, path, size, seconds):
        column = self.column_by_uid(uid)
        key, settings, part = self.encoding_info.get(uid, (None, None, None))
        self.variant_files[uid] = path
        self.results[uid] = (size, seconds)
        self.finished_seconds.append(seconds)
        self.running = None
        if settings is not None:
            self.encoded[uid] = (key, part)
            self.speeds.record(settings, 1, part[1], seconds)
        if column:
            column.progress.setValue(100)
            self.result_text[uid] = self.describe_result(size, seconds, part[1])
            column.result.setText(self.result_text[uid])
        self.load_frames()
        self.show_estimate()

    def on_variant_failed(self, uid, message):
        self.running = None
        self.failed = getattr(self, "failed", set()) | {uid}
        column = self.column_by_uid(uid)
        if column:
            column.result.setText(tr("❌ Failed: {error}", error=message))
        self.show_estimate()

    def on_encoding_finished(self):
        self.running = None
        self.btn_encode.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        for column in self.columns:
            column.remove_button.setEnabled(True)
        self.arrange()
        self.estimate_label.setText("")
        if self.results:
            self.status.setText(tr("Done. Drag a frame to move all views, or choose a zoom; “Use these settings” takes a variant over."))

    def remaining_seconds(self):
        done = len(self.finished_seconds) + len(getattr(self, "failed", set()) & set(self.to_encode))
        waiting = max(0, len(self.to_encode) - done - (1 if self.running else 0))
        if self.running:
            return estimate_remaining(self.finished_seconds, self.running[2], time.monotonic() - self.running[1], waiting)
        return estimate_remaining(self.finished_seconds, None, 0, waiting) if waiting else 0.0

    def show_estimate(self):
        if not self.encoding():
            self.estimate_label.setText("")
            return
        left = self.remaining_seconds()
        self.estimate_label.setText(tr("about {seconds} s left", seconds=int(left + 0.5)) if left is not None
                                    else tr("estimating the time…"))

    def describe_result(self, size, seconds, part_length):
        total = estimate_total_size(size, part_length, self.duration)
        text = tr("Part: {size} · encoded in {seconds:.0f} s", size=format_size(size), seconds=seconds)
        if total:
            original = self.src.stat().st_size if self.src.exists() else 0
            text += "\n" + tr("Whole video: about {size}", size=format_size(total))
            if original:
                text += f" ({total / original * 100:.0f} %)"
        if self.queue and self.queue[0] and self.queue[1] and part_length:
            folder = seconds / part_length * self.queue[1]
            text += "\n" + tr("All {count} videos: about {time}", count=self.queue[0], time=format_duration_long(folder))
        return text

    # ---------------------------------------------------------------------------------- result
    def use_variant(self, index):
        self.apply_settings(self.columns[index].settings())
        self.status.setText(tr("Settings of variant {name} are now in the settings drawer.", name=chr(65 + index)))

    def done(self, result):
        self.cleanup()
        super().done(result)

    def cleanup(self):
        self.closed = True
        self.frame_timer.stop()
        for worker in (self.lab_worker, self.frame_worker):
            if worker is not None and worker.isRunning():
                worker.stop()
                worker.wait()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)
