"""Shared pytest setup for the Video Helper apps: use it with `pytest_plugins = ["video_helper_core.testing"]`."""
import os
import shutil

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)
requires_exiftool = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool not installed")


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    # Never touch the real ~/Library settings/cache or leave files in the repo.
    from video_helper_core.paths import HOME_OVERRIDE_ENV

    monkeypatch.setenv(HOME_OVERRIDE_ENV, str(tmp_path / "app-home"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch):
    # The apps ask GitHub for new releases at start; tests must not use the network.
    from video_helper_core.updates import DISABLE_ENV

    monkeypatch.setenv(DISABLE_ENV, "1")


@pytest.fixture(autouse=True)
def silent_audio(monkeypatch):
    """Tests play real videos; keep them quiet. The real output stays at volume 0 while volume() and isMuted() still
    report what the code under test set, so the volume and mute logic can be tested."""
    import sys

    from PySide6 import QtMultimedia

    class SilentAudioOutput(QtMultimedia.QAudioOutput):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._volume, self._muted = 1.0, False
            super().setVolume(0.0)
            super().setMuted(True)

        def setVolume(self, volume):
            self._volume = volume

        def volume(self):
            return self._volume

        def setMuted(self, muted):
            self._muted = muted

        def isMuted(self):
            return self._muted

    monkeypatch.setattr(QtMultimedia, "QAudioOutput", SilentAudioOutput)
    for module in list(sys.modules.values()):   # modules that imported the class by name before this test
        # vars(), not getattr(): lazy modules (transformers) would import half the world on any attribute lookup
        if vars(module).get("QAudioOutput") is QtMultimedia.QAudioOutput.__mro__[1]:
            monkeypatch.setattr(module, "QAudioOutput", SilentAudioOutput)


@pytest.fixture(autouse=True)
def reset_language():
    from video_helper_core import i18n

    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.fixture(scope="session")
def qapp():
    import gc

    from PySide6.QtCore import QEvent, QThreadPool
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    # Tidy up while Python is still fully alive: Qt objects that are left to the interpreter's shutdown can crash it
    # (seen on macOS with coverage switched on).
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QThreadPool.globalInstance().waitForDone(10_000)
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    app.processEvents()


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))


def spin(ms):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(signal, timeout_ms=120_000):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    signal.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


# --- translation checks shared by the apps' tests ---------------------------------------------------------------------
def tr_literals(*roots):
    """Every string literal passed to tr() under the given folders, with its location."""
    import ast
    from pathlib import Path

    found = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr":
                    arg = node.args[0]
                    assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                        f"tr() needs a string literal: {path.name}:{node.lineno}"
                    found.setdefault(arg.value, f"{path.name}:{node.lineno}")
    return found


def placeholders(text):
    import string

    return sorted(field for _, field, _, _ in string.Formatter().parse(text) if field is not None)


def visible_texts(root):
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QTabWidget, QWidget

    texts = []
    for widget in [root, *root.findChildren(QWidget)]:
        if isinstance(widget, (QLabel, QAbstractButton)):
            texts.append(widget.text())
        if isinstance(widget, QGroupBox):
            texts.append(widget.title())
        if isinstance(widget, QLineEdit):
            texts.append(widget.placeholderText())
        if isinstance(widget, QComboBox):
            texts.extend(widget.itemText(i) for i in range(widget.count()))
        if isinstance(widget, QTabWidget):
            texts.extend(widget.tabText(i) for i in range(widget.count()))
        texts.append(widget.toolTip())
        texts.append(widget.windowTitle())
    return [t for t in texts if t]
