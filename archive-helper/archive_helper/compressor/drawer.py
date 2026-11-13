"""The settings drawer: encoder, quality, limits, options, and the saved defaults."""
import json
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSlider, QToolButton, QVBoxLayout, QWidget,
)
from video_helper_core.style import caption
from video_helper_core.i18n import tr
from video_helper_core.paths import settings_file
from .utils import DEFAULT_EXCLUDES, DEFAULT_POSTFIX
from .options import FRAME_RATES, QUALITY_PRESETS, RENDERERS, RESOLUTIONS, X265_PRESETS


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



class SettingsDrawer:
    """Part of ArchiverGUI (see gui.py): the settings drawer: encoder, quality, limits, options, and the saved defaults."""

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

    def fit_drawer_width(self):
        """At least DRAWER_WIDTH, wider if the content needs it: with other fonts (Windows) the options did not fit
        and were cut off at the right, as the drawer scrolls only up and down."""
        content = self.drawer_scroll.widget()
        content.layout().activate()
        needed = content.minimumSizeHint().width() + self.drawer_scroll.verticalScrollBar().sizeHint().width()
        self.drawer_frame.setFixedWidth(max(self.DRAWER_WIDTH, needed))

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
