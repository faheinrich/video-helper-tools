"""Starting, pausing and stopping a run, and what the worker reports about each video."""
import time
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QMessageBox
from video_helper_core.i18n import tr
from video_helper_core.dialogs import confirm_after_delay
from .workers import ArchiveWorker, BlockCheckWorker


class RunControl:
    """Part of ArchiverGUI (see gui.py): starting, pausing and stopping a run, and what the worker reports about each video."""

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

    def move_up_rows(self, rows):
        """Several videos to the front of the queue, keeping their order in the table."""
        for row in reversed([row for row in rows if row.status == "planned"]):
            self.move_up(row)

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
