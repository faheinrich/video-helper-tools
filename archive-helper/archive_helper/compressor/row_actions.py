"""What can be done with the videos in the list: menus, the detail bar, the viewers, file actions."""
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QDialog, QLabel, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout
from video_helper_core.i18n import tr
from .model import COL_STATUS
from .utils import open_in_finder
from . import file_actions
from .lab import CompressionLabDialog
from .metadata_view import MetadataDialog
from .widgets import CompareVideoDialog


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



class RowActions:
    """Part of ArchiverGUI (see gui.py): what can be done with the videos in the list: menus, the detail bar, the viewers, file actions."""

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
        menu.addAction(tr("Metadata"), lambda: self.show_metadata(row)).setEnabled(src_ok or dst_ok)
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
        if index.column() == COL_STATUS and row.metadata_missing:
            self.show_metadata(row)   # "⚠ metadata?": what is missing, side by side
        elif row.src.exists() and row.has_result and row.status != "running":
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

    def show_metadata(self, row):
        """Original and result side by side; the original alone while there is no result."""
        src = row.src if row.src.exists() else None
        dst = row.dst if row.dst.exists() and row.out_size is not None and row.status != "running" else None
        if src or dst:
            self.metadata_dialog = MetadataDialog(src, dst, self)
            self.metadata_dialog.show()

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
        failed, done = [], []
        for row in rows:
            before = replace(row)   # as it was, for Undo
            try:
                done.append((before, self.apply_file_action(action, row)))
            except OSError as e:
                failed.append(f"{row.src.name}: {e}" if len(rows) > 1 else str(e))
        if done:
            self.record_file_action(title, action, done)
        if action == "del_comp" and any(row.src.exists() for row in rows):
            self.schedule_scan()
        if failed:
            QMessageBox.critical(self, tr("Error"), tr("The action failed: {error}", error="\n".join(failed)))
        self.update_detail_bar()

    def apply_file_action(self, action, row):
        """Does the action for one row; returns its steps (see file_actions)."""
        steps = file_actions.apply(action, row.src, row.dst)
        note = {"overwrite": tr("original replaced"), "swap": tr("swapped"), "del_orig": tr("original in the trash"),
                "del_comp": tr("result in the trash")}[action]
        if action == "del_comp":
            self.model.update(row.src, note=note, out_size=None, status="planned" if row.src.exists() else row.status)
        else:
            self.model.update(row.src, note=note)
        return steps

    def record_file_action(self, title, action, done):
        """Remembers what an action did, so Undo can take it back. done: [(row before, steps)] per video."""
        self.undo_stack.append(FileActionRecord(title, action, done))
        self.redo_stack.clear()


@dataclass
class FileActionRecord:
    """A done file action: its title (for the menu), the action, and per video the row as it was before and the steps."""
    title: str
    action: str
    done: list
