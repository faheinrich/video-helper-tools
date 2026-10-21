"""Window shell shared by the Video Helper apps: title bar, tool overview page, language switch.

An app describes itself with an AppSpec and its tools with Tool entries; run() starts it.
"""
import html
import json
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QFont, QFontDatabase, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QToolButton, QVBoxLayout,
    QWidget,
)

from video_helper_core.i18n import DEFAULT_LANGUAGE, LANGUAGES, current_language, set_language, tr
from video_helper_core.paths import settings_file
from video_helper_core.updates import UpdateChecker

MAC_TITLE_BAR = sys.platform == "darwin"
REPO_URL = "https://github.com/faheinrich/video-helper-tools"
UPDATE_COLOR = "#d9730d"   # orange, same family as the warning colour of the lists


def resource_dir(app_root):
    """Folder with an app's icons: next to the app's main.py, or inside a frozen bundle."""
    return Path(getattr(sys, "_MEIPASS", app_root)) / "resources"


@dataclass
class Tool:
    key: str
    label: Callable[[], str]          # called when the page is built so it follows the language
    icon: str                         # file name in the resource folder
    factory: Callable[[], QWidget]


@dataclass
class AppSpec:
    name: str                         # window title and heading, e.g. "Archive Helper"
    version: str
    about: Callable[[str], str]       # version -> HTML text of the About dialog
    tools: list = field(default_factory=list)
    resources: Path = Path("resources")
    tag_prefix: str = ""              # e.g. "archive-v": releases with this tag prefix are checked for updates
    busy_message: Callable[[], str] = lambda: tr("Please stop running jobs first, then change the language.")


def load_settings():
    path = settings_file()
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def saved_language(settings):
    global_settings = settings.get('global', {})
    if 'language' in global_settings:
        return global_settings['language']
    # Older versions stored an index into ["Deutsch", "English", ...].
    legacy = {0: "de", 1: "en"}
    return legacy.get(global_settings.get('language_index'), DEFAULT_LANGUAGE)


class TitleBar(QWidget):
    """Top bar with the back button and the page name. On macOS it sits in the transparent
    title bar next to the window buttons and behaves like a title bar (drag, double-click)."""

    WINDOW_BUTTONS_WIDTH = 78  # the three macOS window buttons live in this strip

    def __init__(self, back_action):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(self.WINDOW_BUTTONS_WIDTH if MAC_TITLE_BAR else 8, 0, 12, 0)
        layout.setSpacing(4)
        self.back = QToolButton()
        self.back.setDefaultAction(back_action)
        self.back.setAutoRaise(True)
        self.back.setCursor(Qt.PointingHandCursor)
        self.back.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.back.setStyleSheet("""
            QToolButton { border: none; border-radius: 13px; padding: 0 14px; font-weight: 500;
                          background: rgba(128, 128, 128, 0.16); }
            QToolButton:hover { background: rgba(128, 128, 128, 0.28); }
            QToolButton:pressed { background: rgba(128, 128, 128, 0.38); }
        """)
        self.title = QLabel()
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)  # drag and double-click go to the bar
        self.title.setStyleSheet("font-weight: 600;")
        self.back.setFixedHeight(26)
        layout.addWidget(self.back, 0, Qt.AlignVCenter)
        layout.addWidget(self.title)
        layout.addStretch()
        self.setFixedHeight(28)

    def match_title_bar(self, height):
        self.setFixedHeight(max(28, height))

    def show_page(self, title):
        self.back.setVisible(title is not None)
        self.title.setText(title or "")

    def mousePressEvent(self, event):
        handle = self.window().windowHandle()
        if event.button() == Qt.LeftButton and handle is not None:
            handle.startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        window = self.window()
        window.showNormal() if window.isMaximized() else window.showMaximized()


class LandingPage(QWidget):
    def __init__(self, spec, open_tool):
        super().__init__()
        self.spec = spec
        self.open_tool = open_tool
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(18)

        top_bar = QHBoxLayout()
        top_bar.addStretch()
        top_bar.addWidget(QLabel(tr("Language:")))
        self.lang_combo = QComboBox()
        for code, name in LANGUAGES:
            self.lang_combo.addItem(name, code)
        self.lang_combo.setCurrentIndex(self.lang_combo.findData(current_language()))
        top_bar.addWidget(self.lang_combo)
        self.about_btn = QPushButton(tr("About"))
        top_bar.addWidget(self.about_btn)
        layout.addLayout(top_bar)

        logo = QLabel()
        logo_path = self.spec.resources / "logo.png"
        if logo_path.exists():
            logo.setPixmap(QPixmap(str(logo_path)).scaledToWidth(180, Qt.SmoothTransformation))
        logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo)

        title = QLabel(self.spec.name)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 28px; font-weight: bold;")
        layout.addWidget(title)

        subtitle = QLabel(tr("Choose a tool to start"))
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("font-size: 14px; color: #666;")
        layout.addWidget(subtitle)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(40)
        btn_row.setAlignment(Qt.AlignCenter)
        for tool in self.spec.tools:
            btn_row.addWidget(self.tool_button(tool))
        layout.addLayout(btn_row)
        layout.addStretch()

        bottom = QHBoxLayout()
        self.version_label = QLabel(f"v{self.spec.version}")
        self.version_label.setStyleSheet("color: #888;")
        self.update_label = QLabel()
        self.update_label.setOpenExternalLinks(True)
        self.update_label.setStyleSheet(f"color: {UPDATE_COLOR}; font-weight: 600;")
        self.update_label.hide()
        bottom.addWidget(self.version_label)
        bottom.addWidget(self.update_label)
        bottom.addStretch()
        layout.addLayout(bottom)

    def show_update(self, version, url):
        self.update_label.setText(f'<a href="{url}" style="color: {UPDATE_COLOR}; text-decoration: none;">'
                                  + tr("New version v{version} available", version=version) + "</a>")
        self.update_label.show()

    def tool_button(self, tool):
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setAlignment(Qt.AlignCenter)
        vbox.setSpacing(10)

        btn = QPushButton()
        btn.setObjectName(f"open_{tool.key}")
        btn.setFixedSize(140, 140)
        btn.setStyleSheet("""
            QPushButton { border: none; background: transparent; border-radius: 20px; }
            QPushButton:hover { background: rgba(0, 0, 0, 0.05); }
            QPushButton:pressed { background: rgba(0, 0, 0, 0.1); }
        """)
        icon_path = self.spec.resources / tool.icon
        if icon_path.exists():
            btn.setIcon(QIcon(str(icon_path)))
            btn.setIconSize(QSize(120, 120))
        btn.clicked.connect(lambda: self.open_tool(tool.key))

        lbl = QLabel(tool.label())
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("font-size: 16px; font-weight: bold;")

        # Both centred on the same axis: the label is often wider than the 140 px button.
        vbox.addWidget(btn, 0, Qt.AlignHCenter)
        vbox.addWidget(lbl, 0, Qt.AlignHCenter)
        return container


class SuiteWindow(QMainWindow):
    def __init__(self, spec):
        super().__init__()
        self.spec = spec
        self.tool_specs = {tool.key: tool for tool in spec.tools}
        # On macOS the native title would be drawn over our own title bar (and swallow clicks on the back button).
        self.setWindowTitle("" if MAC_TITLE_BAR else spec.name)
        self.resize(1100, 850)

        icon_path = spec.resources / "app-icon.png"
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            self.setWindowIcon(icon)
            app = QApplication.instance()
            if app is not None:
                app.setWindowIcon(icon)

        if MAC_TITLE_BAR:
            # Let the content reach into the transparent title bar, like native Mac apps.
            with warnings.catch_warnings():  # PySide warns: same value as a deprecated flag
                warnings.simplefilter("ignore", DeprecationWarning)
                self.setWindowFlag(Qt.WindowType.ExpandedClientAreaHint, True)
                self.setWindowFlag(Qt.WindowType.NoTitleBarBackgroundHint, True)
            # Qt otherwise pushes the content below the title bar again (safe area).
            self.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        self._safe_area_connected = False

        set_language(saved_language(load_settings()))
        self.build_toolbar()
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        outer = QVBoxLayout(self.central_widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.toolbar)
        content = QWidget()
        outer.addWidget(content, stretch=1)
        self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(0, 0, 0, 0)  # pages bring their own margins
        self.tools = {}  # key -> tool widget
        self.landing_page = None
        self.update_release = None   # (version, url) once the update check found a newer release
        self.build_landing_page()
        if spec.tag_prefix:
            self.update_checker = UpdateChecker(spec.tag_prefix, spec.version, self)
            self.update_checker.found.connect(self.on_update_found)
            self.update_checker.start()

    def on_update_found(self, version, url):
        self.update_release = (version, url)
        self.landing_page.show_update(version, url)

    def build_toolbar(self):
        self.back_action = QAction(self)
        self.back_action.setShortcut(QKeySequence.Back)  # Cmd+[ on macOS
        self.back_action.triggered.connect(self.show_landing)
        self.addAction(self.back_action)  # shortcut works even when the button is hidden
        self.toolbar = TitleBar(self.back_action)
        self.tool_title = self.toolbar.title
        self.retranslate_toolbar()

    def retranslate_toolbar(self):
        self.back_action.setText("‹ " + tr("Back"))
        self.back_action.setToolTip(tr("Back to the overview ({shortcut})",
                                       shortcut=QKeySequence(QKeySequence.Back).toString(QKeySequence.NativeText)))

    def showEvent(self, event):
        super().showEvent(event)
        handle = self.windowHandle()
        if MAC_TITLE_BAR and handle is not None and not self._safe_area_connected:
            self._safe_area_connected = True
            handle.safeAreaMarginsChanged.connect(lambda m: self.toolbar.match_title_bar(m.top()))
            self.toolbar.match_title_bar(handle.safeAreaMargins().top())

    def build_landing_page(self):
        self.landing_page = LandingPage(self.spec, self.show_tool)
        if self.update_release:
            self.landing_page.show_update(*self.update_release)
        self.landing_page.about_btn.clicked.connect(self.show_about)
        self.landing_page.lang_combo.currentIndexChanged.connect(self.change_language)
        self.show_landing()

    def clear_content(self):
        while self.layout.count():
            widget = self.layout.takeAt(0).widget()
            if widget:
                widget.setParent(None)

    def show_landing(self):
        self.clear_content()
        self.toolbar.show_page(None)
        self.back_action.setEnabled(False)
        self.layout.addWidget(self.landing_page)

    def show_tool(self, name):
        self.clear_content()
        if name not in self.tools:
            self.tools[name] = self.tool_specs[name].factory()
        self.toolbar.show_page(self.tool_specs[name].label())
        self.back_action.setEnabled(True)
        self.layout.addWidget(self.tools[name])

    def busy_tools(self):
        return [widget for widget in self.tools.values() if getattr(widget, "is_busy", lambda: False)()]

    def show_about(self):
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle(tr("About {name}", name=self.spec.name))
        msg.setTextFormat(Qt.RichText)
        msg.setTextInteractionFlags(Qt.TextBrowserInteraction)  # makes the link clickable
        checker = getattr(self, "update_checker", None)
        update_line = f"<p><small>{html.escape(checker.describe())}</small></p>" if checker is not None else ""
        msg.setText(f'{self.spec.about(self.spec.version)}<p><a href="{REPO_URL}">{REPO_URL.removeprefix("https://")}</a></p>{update_line}')
        msg.exec()

    def change_language(self, index):
        code = self.landing_page.lang_combo.itemData(index)
        if code == current_language():
            return
        if self.busy_tools():
            # Rebuilding the tools would abort their running jobs.
            combo = self.landing_page.lang_combo
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(current_language()))
            combo.blockSignals(False)
            QMessageBox.information(self, tr("Language"), self.spec.busy_message())
            return

        set_language(code)
        self.retranslate_toolbar()
        try:
            settings = load_settings()
            settings.setdefault('global', {})['language'] = code
            settings['global'].pop('language_index', None)
            with open(settings_file(), 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
        except OSError as e:
            print(f"Could not save the language setting: {e}")

        # Tool pages translate their texts when built, so rebuild everything.
        self.shutdown_tools()
        for widget in self.tools.values():
            widget.deleteLater()
        self.tools.clear()
        old_landing = self.landing_page
        self.build_landing_page()
        old_landing.deleteLater()

    def closeEvent(self, event):
        busy = self.busy_tools()
        if busy:
            reply = QMessageBox.question(
                self, tr("Jobs still running"),
                tr("A job is still running. Quit anyway?\nRunning compressions are aborted and their unfinished output files deleted."),
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        self.shutdown_tools()
        event.accept()

    def shutdown_tools(self):
        # Embedded tool widgets never receive closeEvent, so stop their jobs/servers here.
        for widget in self.tools.values():
            if hasattr(widget, "shutdown"):
                widget.shutdown()


def run(spec, font=None):
    """Starts the app window; returns only through sys.exit."""
    # Fix PATH for bundled macOS apps so they can find ffmpeg, ffprobe, and exiftool
    if sys.platform == 'darwin':
        paths = os.environ.get('PATH', '').split(os.pathsep)
        for path in ['/usr/local/bin', '/opt/homebrew/bin']:
            if path not in paths:
                paths.insert(0, path)
        os.environ['PATH'] = os.pathsep.join(paths)

    app = QApplication(sys.argv)
    app.setApplicationName(spec.name)

    if font and os.path.exists(font):
        font_id = QFontDatabase.addApplicationFont(font)
        if font_id != -1:
            app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0]))

    window = SuiteWindow(spec)
    window.show()
    sys.exit(app.exec())
