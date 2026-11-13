import html
import os
import re
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox, QTextEdit,
    QFrame, QProgressBar, QStyle, QCheckBox,
)
from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QFontDatabase

from video_helper_core.i18n import tr
from video_helper_core.keep_awake import KeepAwake
from video_helper_core.notify import notify
from video_helper_core.style import STYLE, caption

RSYNC_PROGRAM = "rsync"   # replaced in tests
RSYNC_ARGS = ["-av", "--del", "--partial", "--progress", "--modify-window=1", "--size-only"]

# rsync output lines that are not file names.
NON_FILE_PREFIXES = ("sending ", "building file list", "sent ", "total size", "deleting ", "rsync:", "created directory",
                     "Transfer starting")  # openrsync (macOS)
PROGRESS_RE = re.compile(r"(\d+)%")


class RsyncSyncGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.files_done = 0
        self.started_at = 0.0
        self.keep_awake = KeepAwake(reason="A backup is running")
        self.notifier = notify   # replaced in tests
        self.stopped_by_user = False
        self.init_ui()

    def folder_field(self, title, placeholder, on_browse):
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(caption(title))
        row = QHBoxLayout()
        row.setSpacing(6)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        button = QPushButton(tr("Browse…"))
        button.clicked.connect(on_browse)
        row.addWidget(edit, 1)
        row.addWidget(button)
        column.addLayout(row)
        return column, edit

    def init_ui(self):
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        src_column, self.src_edit = self.folder_field(tr("Source folder:").rstrip(":"), tr("Folder to back up"), self.browse_source)
        tgt_column, self.tgt_edit = self.folder_field(tr("Target folder:").rstrip(":"), tr("Backup location, e.g. an external drive"), self.browse_target)
        arrow = QLabel("→")
        arrow.setProperty("role", "hint")
        header.addLayout(src_column, 1)
        header.addWidget(arrow)
        header.addLayout(tgt_column, 1)
        self.sync_btn = QPushButton(tr("Start sync"), objectName="runButton")
        self.sync_btn.clicked.connect(self.toggle_sync)
        run_column = QVBoxLayout()
        run_column.addStretch()
        run_column.addWidget(self.sync_btn)
        self.cb_keep_awake = QCheckBox(tr("Keep computer awake"))
        self.cb_keep_awake.setToolTip(tr("Prevents sleep while the backup is running."))
        self.cb_keep_awake.setChecked(False)
        self.cb_keep_awake.toggled.connect(self.update_keep_awake)
        run_column.addWidget(self.cb_keep_awake, 0, Qt.AlignRight)
        header.addLayout(run_column)
        layout.addLayout(header)

        info_label = QLabel(tr("Mirrors a folder to a backup location (e.g. an external drive) with rsync.\nOptions: {options}", options=" ".join(RSYNC_ARGS)))
        info_label.setProperty("role", "hint")
        layout.addWidget(info_label)

        layout.addWidget(self.build_summary())

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log_output.setPlaceholderText(tr("The rsync output appears here once the sync is running."))
        layout.addWidget(self.log_output, 1)

        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.handle_stdout)
        self.process.readyReadStandardError.connect(self.handle_stderr)
        self.process.finished.connect(self.process_finished)
        # rsync missing (as usually on Windows) or not startable: Qt then reports only this, never finished
        self.process.errorOccurred.connect(self.process_error)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_elapsed)
        self.set_status(tr("Ready"))
        self.set_run_mode(False)

    def build_summary(self):
        frame = QFrame(objectName="summary")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(28)
        self.values = {}
        for key, label in (("status", tr("Status")), ("files", tr("Files")), ("elapsed", tr("Elapsed"))):
            column = QVBoxLayout()
            column.setSpacing(0)
            value = QLabel("–")
            value.setProperty("role", "value")
            column.addWidget(caption(label))
            column.addWidget(value)
            row.addLayout(column)
            self.values[key] = value
        row.addStretch()
        outer.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        outer.addWidget(self.progress)
        return frame

    def set_status(self, text, tone=None):
        label = self.values["status"]
        label.setText(text)
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)

    def set_run_mode(self, running):
        self.sync_btn.setProperty("mode", "stop" if running else "start")
        self.sync_btn.setText(tr("Stop sync") if running else tr("Start sync"))
        self.sync_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop if running else QStyle.SP_MediaPlay))
        self.sync_btn.style().unpolish(self.sync_btn)
        self.sync_btn.style().polish(self.sync_btn)

    def update_elapsed(self):
        seconds = int(time.monotonic() - self.started_at)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        self.values["elapsed"].setText(f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}")

    def is_busy(self):
        return self.process.state() != QProcess.NotRunning

    def shutdown(self):
        self.stop_sync()
        self.process.waitForFinished(3000)

    def browse_source(self):
        folder = QFileDialog.getExistingDirectory(self, tr("Select source folder"))
        if folder:
            self.src_edit.setText(folder)

    def browse_target(self):
        folder = QFileDialog.getExistingDirectory(self, tr("Select target folder"))
        if folder:
            self.tgt_edit.setText(folder)

    def toggle_sync(self):
        if self.is_busy():
            self.stop_sync()
        else:
            self.start_sync()

    def start_sync(self):
        src = self.src_edit.text().strip()
        tgt = self.tgt_edit.text().strip()

        if not src or not tgt:
            QMessageBox.warning(self, tr("Error"), tr("Please select both source and target folders."))
            return

        if not os.path.exists(src):
            QMessageBox.warning(self, tr("Error"), tr("Source folder does not exist."))
            return

        reply = QMessageBox.question(
            self, tr("Start sync"),
            tr("Sync from:\n{src}\nto:\n{tgt}\n\nFiles in the target folder that no longer exist in the source folder will be deleted (--del). Continue?", src=src, tgt=tgt),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # A trailing slash makes rsync copy the folder's contents, not the folder itself.
        if not src.endswith('/'):
            src += '/'

        self.set_run_mode(True)
        self.log_output.clear()
        self.files_done = 0
        self.values["files"].setText("0")
        self.progress.setValue(0)
        self.started_at = time.monotonic()
        self.update_elapsed()
        self.timer.start(1000)
        self.set_status(tr("Running"))

        args = [*RSYNC_ARGS, src, tgt]
        self.append_log(tr("Running command: {command}", command="rsync " + " ".join(args)))
        self.stopped_by_user = False
        self.process.start(RSYNC_PROGRAM, args)
        self.update_keep_awake()

    def update_keep_awake(self):
        if self.process.state() != QProcess.NotRunning and self.cb_keep_awake.isChecked():
            self.keep_awake.start()
        else:
            self.keep_awake.stop()

    def stop_sync(self):
        # also while it is still starting: then "Running" is not reached yet and the stop was lost
        if self.process.state() != QProcess.NotRunning:
            self.stopped_by_user = True
            self.process.kill()
            self.append_log(tr("Process stopped by user."))

    def append_log(self, text, html_format="{}"):
        # rsync prints file names, which must not be interpreted as HTML.
        self.log_output.append(html_format.format(html.escape(text)))

    def handle_line(self, line):
        match = PROGRESS_RE.search(line)
        if match:
            self.progress.setValue(int(match.group(1)))
        elif line and not line.startswith(NON_FILE_PREFIXES) and not line.endswith("/"):
            self.files_done += 1
            self.values["files"].setText(str(self.files_done))

    def handle_stdout(self):
        data = self.process.readAllStandardOutput().data().decode('utf-8', errors='replace')
        for line in data.replace('\r', '\n').split('\n'):
            line = line.strip()
            if line:
                self.append_log(line)
                self.handle_line(line)

    def handle_stderr(self):
        data = self.process.readAllStandardError().data().decode('utf-8', errors='replace')
        self.append_log(data, "<font color='red'>{}</font>")

    def process_error(self, error):
        if error != QProcess.FailedToStart:
            return   # a crash or kill ends with finished as well
        self.keep_awake.stop()
        self.timer.stop()
        self.set_run_mode(False)
        self.set_status(tr("Error"), "warn")
        self.append_log(tr("rsync could not be started: {error}", error=self.process.errorString()), "<b>{}</b>")

    def process_finished(self, exitCode, exitStatus):
        self.keep_awake.stop()
        self.timer.stop()
        self.update_elapsed()
        self.set_run_mode(False)
        if self.stopped_by_user:
            self.set_status(tr("Stopped"), "warn")
        elif exitStatus == QProcess.NormalExit and exitCode == 0:
            self.progress.setValue(100)
            self.set_status(tr("Done"), "good")
            self.append_log(tr("Sync completed successfully!"), "<b>{}</b>")
        else:
            self.set_status(tr("Error"), "warn")
            self.append_log(tr("Sync finished with exit code {code}.", code=exitCode), "<b>{}</b>")
        if not self.stopped_by_user:
            self.notifier(self.window(), tr("Backup (rsync)"), tr("Done") if exitStatus == QProcess.NormalExit and exitCode == 0 else tr("Error"))
