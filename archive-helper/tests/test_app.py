import os
import sys

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QMessageBox

from video_helper_core.shell import REPO_URL

from archive_helper import __version__, app


def test_main_starts_the_window_and_applies_the_macos_path_fix(qapp, monkeypatch):
    from video_helper_core import shell

    shown = []
    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(sys, "platform", "darwin")

    class FakeApplication:
        def __new__(cls, argv):
            return qapp

        instance = staticmethod(lambda: qapp)

    monkeypatch.setattr(shell, "QApplication", FakeApplication)
    monkeypatch.setattr(qapp, "exec", lambda: 0)
    monkeypatch.setattr(shell.SuiteWindow, "show", lambda self: shown.append(self))
    monkeypatch.setenv("PATH", "/usr/bin")
    with pytest.raises(SystemExit) as stop:
        app.main()
    assert stop.value.code == 0 and len(shown) == 1
    assert "/opt/homebrew/bin" in os.environ["PATH"].split(os.pathsep)
    shown[0].close()


def test_title_bar_double_click_toggles_maximized(qapp):
    from video_helper_core.shell import TitleBar

    window = app.ArchiveHelperWindow()
    bar = window.findChild(TitleBar)
    event = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    calls = []
    window.showMaximized = lambda: calls.append("max")
    window.showNormal = lambda: calls.append("normal")
    bar.mouseDoubleClickEvent(event)
    assert calls == ["max"]
    window.close()


def test_about_dialog_names_the_app_and_version(qapp, monkeypatch):
    texts = []
    monkeypatch.setattr(QMessageBox, "setText", lambda self, text: texts.append(text))
    window = app.ArchiveHelperWindow()
    window.show_about()
    assert any("Archive Helper" in t and __version__ in t and REPO_URL in t for t in texts)
    window.close()


def test_version_matches_pyproject():
    import tomllib
    from pathlib import Path

    declared = tomllib.loads((Path(app.__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == declared


def test_suite_opens_every_tool_and_switches_language(qapp, monkeypatch):
    # PySide6 only reports exceptions raised inside slots via sys.excepthook.
    slot_errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: slot_errors.append(exc))

    window = app.ArchiveHelperWindow()
    window.show()
    for tool in app.SPEC.tools:
        window.show_tool(tool.key)
        qapp.processEvents()
    window.show_landing()

    # Regression: switching language used to abort the app from inside a Qt slot.
    for index in range(window.landing_page.lang_combo.count()):
        window.landing_page.lang_combo.setCurrentIndex(index)
        qapp.processEvents()
    window.close()
    assert slot_errors == []


def test_back_lives_in_the_title_bar_and_tools_start_below_it(qapp):
    window = app.ArchiveHelperWindow()
    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    assert window.toolbar.height() <= 40
    assert not window.toolbar.back.isVisible()

    window.show_tool("compressor")
    qapp.processEvents()
    assert window.toolbar.back.isVisible() and window.tool_title.text() == "Komprimieren & Archivieren"
    # The tool starts right below the slim title bar: no extra row, no doubled margins.
    top = window.compressor_tab.mapTo(window, window.compressor_tab.rect().topLeft()).y()
    assert top == window.toolbar.height()

    window.back_action.trigger()
    qapp.processEvents()
    assert not window.toolbar.back.isVisible()


def test_language_change_is_refused_while_a_job_runs(qapp, monkeypatch):
    window = app.ArchiveHelperWindow()
    window.show_tool("compressor")
    monkeypatch.setattr(window.compressor_tab, "is_busy", lambda: True)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    combo = window.landing_page.lang_combo
    combo.setCurrentIndex(combo.findData("en"))
    assert shown and combo.currentData() == "de"
    monkeypatch.undo()   # a busy tool would make close() ask whether to quit
    window.close()


def test_landing_page_shows_the_version_and_announces_a_newer_release(qapp, monkeypatch):
    from video_helper_core import updates

    window = app.ArchiveHelperWindow()
    window.show()
    qapp.processEvents()
    landing = window.landing_page
    assert landing.version_label.text() == f"v{__version__}" and landing.version_label.isVisible()
    assert landing.update_label.isHidden()

    window.update_checker.found.emit("9.9.9", "https://example/release")
    assert not landing.update_label.isHidden()
    assert "v9.9.9" in landing.update_label.text() and "https://example/release" in landing.update_label.text()
    assert "d9730d" in landing.update_label.styleSheet()

    # the announcement survives a language switch (the landing page is rebuilt)
    window.landing_page.lang_combo.setCurrentIndex(window.landing_page.lang_combo.findData("en"))
    qapp.processEvents()
    assert "New version v9.9.9 available" in window.landing_page.update_label.text()
    window.close()


def test_the_check_looks_for_this_apps_tags(qapp, monkeypatch):
    window = app.ArchiveHelperWindow()
    assert (window.update_checker.tag_prefix, window.update_checker.current_version) == (app.SPEC.tag_prefix, __version__)
    assert app.SPEC.tag_prefix.endswith("-v") and app.SPEC.tag_prefix.startswith("archive")
    window.close()


def test_tool_icons_and_names_share_one_centre_line(qapp):
    window = app.ArchiveHelperWindow()
    window.resize(900, 600)
    window.show()
    qapp.processEvents()
    for tool in app.SPEC.tools:
        button = window.landing_page.findChild(type(window.landing_page.about_btn), f"open_{tool.key}")
        label = button.parentWidget().findChildren(type(window.landing_page.version_label))[0]
        assert abs(button.geometry().center().x() - label.geometry().center().x()) <= 1
    window.close()


def test_about_dialog_tells_how_the_update_check_went(qapp, monkeypatch):
    texts = []
    monkeypatch.setattr(QMessageBox, "setText", lambda self, text: texts.append(text))
    window = app.ArchiveHelperWindow()
    window.show_about()
    assert "ausgeschaltet" in texts[-1]                       # the tests switch the check off

    window.update_checker.status, window.update_checker.detail = "current", "9.9.9"
    window.show_about()
    assert "neueste Version" in texts[-1] and "9.9.9" in texts[-1]
    window.update_checker.status, window.update_checker.detail = "failed", "SSLError: <bad>"
    window.show_about()
    assert "fehlgeschlagen" in texts[-1] and "&lt;bad&gt;" in texts[-1]   # shown as text, not as markup
    window.close()
