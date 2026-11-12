import sys
from pathlib import Path
import shutil
import tempfile
import librosa
import numpy as np
import matplotlib.pyplot as plt
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QLineEdit, QFileDialog, QSlider, QGridLayout,
                             QSizePolicy, QMessageBox, QFrame, QStyle)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QPixmap, QPainter, QPen

from video_helper_core.i18n import tr
from video_helper_core.media import MediaPlayer
from video_helper_core.style import STYLE, caption
from analysis_helper.sync.info import SyncInfoDialog
from analysis_helper.sync.video_synch import extract_audio_tracks, calculate_shift_fft, trim_video

SYNC_STYLE = """
QFrame#videoCard { background: black; border-radius: 8px; }
QLabel#waveform { background: palette(base); border: 1px solid palette(mid); border-radius: 8px; color: #858b95; }
QPushButton[role="segment"]:first-of-type { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }
"""


class VideoSyncGUI(QWidget):
    def __init__(self):
        super().__init__()
        # TemporaryDirectory cleans up on garbage collection or at exit; closeEvent never
        # fires for this embedded widget.
        self._temp = tempfile.TemporaryDirectory(prefix="video-sync-")
        self.temp_dir = self._temp.name
        self.shift = 0.0  # Shift in seconds (vid2 - vid1)
        self.vid1_path = None
        self.vid2_path = None
        self.target_dir = None
        
        self.duration = 0  # Max duration in ms
        self.is_scrubbing = False

        self.init_ui()

    def path_field(self, title, edit, button):
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(caption(title))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(edit, 1)
        row.addWidget(button)
        column.addLayout(row)
        return column

    def video_card(self, title, widget):
        card = QFrame(objectName="videoCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(widget)
        return card

    def init_ui(self):
        self.setStyleSheet(STYLE + SYNC_STYLE)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 10, 12, 12)
        main_layout.setSpacing(10)

        # File selection
        self.vid1_edit = QLineEdit()
        self.vid1_edit.setPlaceholderText(tr("Video 1:").rstrip(":"))
        self.vid1_btn = QPushButton(tr("Browse…"))
        self.vid1_btn.clicked.connect(lambda: self.browse_file(self.vid1_edit))
        self.vid2_edit = QLineEdit()
        self.vid2_edit.setPlaceholderText(tr("Video 2:").rstrip(":"))
        self.vid2_btn = QPushButton(tr("Browse…"))
        self.vid2_btn.clicked.connect(lambda: self.browse_file(self.vid2_edit))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText(tr("Target folder:").rstrip(":"))
        self.target_btn = QPushButton(tr("Browse…"))
        self.target_btn.clicked.connect(self.browse_folder)

        files = QGridLayout()
        files.setHorizontalSpacing(14)
        files.setVerticalSpacing(6)
        files.addLayout(self.path_field(tr("Video 1:").rstrip(":"), self.vid1_edit, self.vid1_btn), 0, 0)
        files.addLayout(self.path_field(tr("Video 2:").rstrip(":"), self.vid2_edit, self.vid2_btn), 0, 1)
        files.addLayout(self.path_field(tr("Target folder:").rstrip(":"), self.target_edit, self.target_btn), 1, 0, 1, 2)
        main_layout.addLayout(files)

        # Load, sync and save
        top_btn_layout = QHBoxLayout()
        top_btn_layout.setSpacing(8)
        main_layout.addLayout(top_btn_layout)

        self.load_btn = QPushButton(tr("Load videos"))
        self.load_btn.clicked.connect(self.load_videos)
        top_btn_layout.addWidget(self.load_btn)

        self.sync_btn = QPushButton(tr("Sync (calculate shift)"), objectName="runButton")
        self.sync_btn.clicked.connect(self.calculate_sync)
        top_btn_layout.addWidget(self.sync_btn)

        self.shift_label = QLabel(tr("Shift: {seconds:.2f} s", seconds=0.0))
        self.shift_label.setProperty("role", "value")
        top_btn_layout.addSpacing(10)
        top_btn_layout.addWidget(self.shift_label)
        top_btn_layout.addStretch()

        self.info_btn = QPushButton("ⓘ  " + tr("Info"))
        self.info_btn.setToolTip(tr("How the shift is calculated and how accurate it is"))
        self.info_btn.clicked.connect(self.show_info)
        top_btn_layout.addWidget(self.info_btn)

        self.reset_btn = QPushButton(tr("Reset"))
        self.reset_btn.clicked.connect(self.reset_all)
        top_btn_layout.addWidget(self.reset_btn)

        self.save_btn = QPushButton(tr("Save synced videos"))
        self.save_btn.clicked.connect(self.save_synced_videos)
        top_btn_layout.addWidget(self.save_btn)

        # Video Players
        video_layout = QHBoxLayout()
        video_layout.setSpacing(10)
        main_layout.addLayout(video_layout, 1)

        self.player1 = MediaPlayer(self)
        self.audio1 = QAudioOutput(self)
        self.player1.setAudioOutput(self.audio1)
        self.video_widget1 = QVideoWidget()
        video_layout.addWidget(self.video_card("1", self.video_widget1))
        self.player1.setVideoOutput(self.video_widget1)

        self.player2 = MediaPlayer(self)
        self.audio2 = QAudioOutput(self)
        self.player2.setAudioOutput(self.audio2)
        self.video_widget2 = QVideoWidget()
        video_layout.addWidget(self.video_card("2", self.video_widget2))
        self.player2.setVideoOutput(self.video_widget2)

        # Waveforms
        waveform_layout = QHBoxLayout()
        waveform_layout.setSpacing(10)
        main_layout.addLayout(waveform_layout)

        self.waveform_label1 = QLabel(tr("Waveform 1"))
        self.waveform_label2 = QLabel(tr("Waveform 2"))
        for label in (self.waveform_label1, self.waveform_label2):
            label.setObjectName("waveform")
            label.setAlignment(Qt.AlignCenter)
            label.setFixedHeight(100)
            label.setMinimumWidth(100)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            waveform_layout.addWidget(label)

        # Scrubbing slider
        self.scrub_slider = QSlider(Qt.Horizontal)
        self.scrub_slider.setRange(0, 1000)
        self.scrub_slider.sliderPressed.connect(self.scrub_start)
        self.scrub_slider.sliderReleased.connect(self.scrub_stop)
        self.scrub_slider.sliderMoved.connect(self.scrub_move)
        main_layout.addWidget(self.scrub_slider)

        # Controls
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(8)
        main_layout.addLayout(controls_layout)

        style = self.style()
        self.play_btn = QPushButton(tr("Play"))
        self.play_btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.clicked.connect(self.play_sync)
        controls_layout.addWidget(self.play_btn)

        self.stop_btn = QPushButton(tr("Stop"))
        self.stop_btn.setIcon(style.standardIcon(QStyle.SP_MediaStop))
        self.stop_btn.clicked.connect(self.stop_playback)
        controls_layout.addWidget(self.stop_btn)

        self.mute_btn = QPushButton(tr("Mute"))
        self.mute_btn.clicked.connect(self.toggle_mute)
        controls_layout.addWidget(self.mute_btn)

        controls_layout.addStretch()
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(50)
        self.vol_slider.setMaximumWidth(220)
        self.vol_slider.valueChanged.connect(self.set_volume)
        controls_layout.addWidget(caption(tr("Volume:").rstrip(":")))
        controls_layout.addWidget(self.vol_slider)

        # Timer for slider update
        self.timer = QTimer()
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_slider)
        self.timer.start()

        self.player1.durationChanged.connect(self.update_duration)
        self.player2.durationChanged.connect(self.update_duration)

        # Manual adjustment: earlier on the left, later on the right
        adj_layout = QHBoxLayout()
        adj_layout.setSpacing(0)
        main_layout.addLayout(adj_layout)
        adj_layout.addWidget(caption(tr("Manual adjustment:").rstrip(":")))
        adj_layout.addSpacing(12)
        adj_layout.addStretch()

        steps = [-5, -1, -0.1, -0.01, 0.01, 0.1, 1, 5]
        for val in steps:
            btn = QPushButton(f"{val:+g}s")
            btn.setProperty("role", "segment")
            btn.setMinimumWidth(64)
            btn.clicked.connect(lambda _, v=val: self.adjust_shift(v))
            adj_layout.addWidget(btn)
        adj_layout.addStretch()

    def show_info(self):
        SyncInfoDialog(self).exec()

    def browse_file(self, edit_widget):
        path, _ = QFileDialog.getOpenFileName(self, tr("Select video"), "", tr("Videos (*.mp4 *.mkv *.avi *.mov)"))
        if path:
            edit_widget.setText(path)

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr("Select target folder"))
        if path:
            self.target_edit.setText(path)

    def load_videos(self):
        p1 = self.vid1_edit.text()
        p2 = self.vid2_edit.text()
        if not p1 or not p2:
            return
        
        self.vid1_path = Path(p1)
        self.vid2_path = Path(p2)

        # Playback only reads the files, so no temporary copies are needed.
        self.player1.setSource(QUrl.fromLocalFile(str(self.vid1_path)))
        self.player2.setSource(QUrl.fromLocalFile(str(self.vid2_path)))

        self.generate_waveforms()

    def generate_waveforms(self):
        # Generate waveforms for both videos
        self.wf_pixmaps = [None, None]
        for i, vid_path in enumerate([self.vid1_path, self.vid2_path], 1):
            aud_path = Path(self.temp_dir) / f"wf_aud{i}.wav"
            extract_audio_tracks(vid_path, aud_path)
            
            sig, sr = librosa.load(str(aud_path), sr=8000, mono=True)
            
            # Constant scale: 100 pixels per second
            pixels_per_second = 100
            duration_s = len(sig)/sr
            
            # Use a fixed width for the plot based on duration and pixels_per_second
            # plt.figure figsize is in inches. At 100 DPI, 1 inch = 100 pixels.
            fig_width = duration_s * pixels_per_second / 100
            
            plt.figure(figsize=(fig_width, 1), dpi=100)
            ax = plt.axes([0, 0, 1, 1], frameon=False)
            ax.get_xaxis().set_visible(False)
            ax.get_yaxis().set_visible(False)
            plt.plot(np.linspace(0, duration_s, len(sig)), sig, color='blue')
            plt.xlim(0, duration_s)
            plt.ylim(-1, 1)
            
            wf_img_path = Path(self.temp_dir) / f"waveform{i}.png"
            # Ensure we don't use bbox_inches='tight' because it recalculates the bounding box
            # and might change the width from what we specified in figsize.
            plt.savefig(wf_img_path, pad_inches=0, transparent=True, dpi=100)
            plt.close()
            
            self.wf_pixmaps[i-1] = QPixmap(str(wf_img_path))
        
        self.update_waveforms(0)

    def update_waveforms(self, pos_ms):
        # Update waveform display based on current synchronized position (pos_ms)
        for i in range(2):
            pixmap = self.wf_pixmaps[i]
            if pixmap is None or pixmap.isNull():
                continue
                
            label = self.waveform_label1 if i == 0 else self.waveform_label2
            
            # Calculate time for this specific video
            if i == 0:
                # Video 1
                vid_time_ms = pos_ms + (int(self.shift * 1000) if self.shift > 0 else 0)
            else:
                # Video 2
                vid_time_ms = pos_ms + (int(-self.shift * 1000) if self.shift < 0 else 0)
            
            player = self.player1 if i == 0 else self.player2
            duration = player.duration()
            if duration <= 0:
                continue
                
            # Calculate center_x directly using the 100 pixels per second scale
            pixels_per_second = 100
            center_x = int((vid_time_ms / 1000.0) * pixels_per_second)
            
            view_width = label.width()
            full_width = pixmap.width()
            
            start_x = center_x - view_width // 2
            
            # Create a base pixmap for the label to handle out-of-bounds areas (padding)
            # Use label.rect().size() to ensure we match the current visual size exactly
            display_pixmap = QPixmap(label.rect().size())
            display_pixmap.fill(Qt.transparent)
            
            painter = QPainter(display_pixmap)
            
            # Source rectangle in the waveform pixmap
            src_x = max(0, start_x)
            src_w = view_width - (src_x - start_x)
            if src_x + src_w > full_width:
                src_w = full_width - src_x
                
            # Destination rectangle in the display pixmap
            dst_x = 0 if start_x >= 0 else -start_x
            dst_w = src_w
            
            if src_w > 0:
                painter.drawPixmap(dst_x, 0, dst_w, label.height(), 
                                   pixmap, src_x, 0, src_w, pixmap.height())
            
            # Draw center indicator line
            painter.setPen(QPen(Qt.red, 2))
            painter.drawLine(view_width // 2, 0, view_width // 2, label.height())
            painter.end()
            
            label.setPixmap(display_pixmap)

    def calculate_sync(self):
        if not self.vid1_path or not self.vid2_path:
            return
        
        aud1 = Path(self.temp_dir) / "aud1.wav"
        aud2 = Path(self.temp_dir) / "aud2.wav"
        
        extract_audio_tracks(self.vid1_path, aud1)
        extract_audio_tracks(self.vid2_path, aud2)
        
        sr = 16000
        sig1, _ = librosa.load(str(aud1), sr=sr, mono=True)
        sig2, _ = librosa.load(str(aud2), sr=sr, mono=True)

        shift_samples = calculate_shift_fft(sig1, sig2)
        self.shift = shift_samples / sr
        self.update_shift_display()

    def update_shift_display(self):
        self.shift_label.setText(tr("Shift: {seconds:.2f} s", seconds=self.shift))

    def adjust_shift(self, delta):
        self.shift += delta
        self.update_shift_display()

    def update_duration(self):
        d1 = self.player1.duration()
        d2 = self.player2.duration()
        self.duration = max(d1, d2)
        if self.duration > 0:
            self.scrub_slider.setRange(0, self.duration)

    def update_slider(self):
        if not self.is_scrubbing and (self.player1.playbackState() == QMediaPlayer.PlaybackState.PlayingState or self.player2.playbackState() == QMediaPlayer.PlaybackState.PlayingState):
            # We base the slider on the "master" position (logic from play_sync)
            if self.shift > 0:
                pos = self.player1.position() - int(self.shift * 1000)
            else:
                pos = self.player1.position()
            self.scrub_slider.setValue(pos)
            self.update_waveforms(pos)

    def scrub_start(self):
        self.is_scrubbing = True

    def scrub_stop(self):
        self.is_scrubbing = False
        self.scrub_move(self.scrub_slider.value())

    def scrub_move(self, pos):
        # pos is the relative synchronized position
        if self.shift > 0:
            self.player1.setPosition(pos + int(self.shift * 1000))
            self.player2.setPosition(pos)
        else:
            self.player1.setPosition(pos)
            self.player2.setPosition(pos + int(-self.shift * 1000))
        self.update_waveforms(pos)

    def play_sync(self):
        # We need to play them such that they are synchronized.
        # Shift is (vid2_start - vid1_start).
        # If shift > 0, vid1 starts earlier. To sync, we should start vid2 at 0 and vid1 at shift.
        # Actually, it's easier to use the players' position.
        
        self.player1.stop()
        self.player2.stop()
        
        if self.shift > 0:
            # Vid 1 starts earlier. We want to align the points in time.
            # If shift is positive, vid2 starts 'shift' seconds after vid1.
            # To sync them, we start player1 at shift and player2 at 0.
            self.player1.setPosition(int(self.shift * 1000))
            self.player2.setPosition(0)
            initial_pos = 0
        else:
            # Vid 2 starts earlier.
            self.player1.setPosition(0)
            self.player2.setPosition(int(-self.shift * 1000))
            initial_pos = 0
            
        self.player1.play()
        self.player2.play()
        self.update_waveforms(initial_pos)

    def stop_playback(self):
        self.player1.stop()
        self.player2.stop()

    def set_volume(self, val):
        self.audio1.setVolume(val / 100)
        self.audio2.setVolume(val / 100)

    def toggle_mute(self):
        muted = not self.audio1.isMuted()
        self.audio1.setMuted(muted)
        self.audio2.setMuted(muted)
        self.mute_btn.setText(tr("Unmute") if muted else tr("Mute"))

    def reset_all(self):
        self.stop_playback()
        self.player1.setSource(QUrl())
        self.player2.setSource(QUrl())
        self.vid1_edit.clear()
        self.vid2_edit.clear()
        self.target_edit.clear()
        self.vid1_path = None
        self.vid2_path = None
        self.shift = 0.0
        self.update_shift_display()
        self.waveform_label1.clear()
        self.waveform_label1.setText(tr("Waveform 1"))
        self.waveform_label2.clear()
        self.waveform_label2.setText(tr("Waveform 2"))
        self.scrub_slider.setValue(0)

    def save_synced_videos(self):
        target_dir_str = self.target_edit.text()
        if not target_dir_str or not self.vid1_path or not self.vid2_path:
            QMessageBox.warning(self, tr("Save synced videos"), tr("Please load both videos and choose a target folder first."))
            return
        
        target_dir = Path(target_dir_str)
        target_dir.mkdir(parents=True, exist_ok=True)

        if self.shift > 0:
            # vid_1 starts before vid_2, so we need to trim vid_1 and copy vid_2
            start_time = self.shift
            print(f"Trimming {self.vid1_path} by {start_time:.2f} seconds.")
            shutil.copy(self.vid2_path, target_dir / (self.vid2_path.stem + "_sync.mp4"))
            trim_video(self.vid1_path, start_time=start_time, output_path=target_dir / (self.vid1_path.stem + "_sync.mp4"))
        else:
            # vid_2 starts before vid_1, so we need to trim vid_2 and copy vid_1
            start_time = -self.shift
            print(f"Trimming {self.vid2_path} by {start_time:.2f} seconds.")
            shutil.copy(self.vid1_path, target_dir / (self.vid1_path.stem + "_sync.mp4"))
            trim_video(self.vid2_path, start_time=start_time, output_path=target_dir / (self.vid2_path.stem + "_sync.mp4"))
        
        QMessageBox.information(self, tr("Save synced videos"), tr("Synced videos saved to:\n{folder}", folder=target_dir))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoSyncGUI()
    window.show()
    sys.exit(app.exec())
