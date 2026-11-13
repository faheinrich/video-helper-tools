"""Transcriber window and ELAN helpers with the Whisper server and the transcription itself replaced by fakes."""
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QMessageBox

from conftest import EXAMPLE_VIDEOS, spin
from analysis_helper.transcriber import gui as tgui


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


class SilentLogThread:
    """Stands in for ServerLogThread: a real QThread replaced while running would abort the interpreter."""
    def __init__(self, process):
        self.log_emitted = types.SimpleNamespace(connect=lambda slot: None)

    def start(self):
        pass


@pytest.fixture
def whisper(qapp, home, monkeypatch):
    monkeypatch.setattr(tgui, "ServerLogThread", SilentLogThread)
    """Window with a server that always answers /docs with 200."""
    monkeypatch.setattr(tgui.requests, "get", lambda url, timeout=0: types.SimpleNamespace(status_code=200))
    window = tgui.WhisperGui()
    yield window
    window.shutdown()


def fake_transcribe(calls, fail=None, segments=2):
    def transcribe(video, url, port, **kwargs):
        calls.append((str(video), url, port, kwargs))
        if fail:
            raise RuntimeError(fail)
        callback = kwargs["progress_callback"]
        callback(-2, -2, audio_signal=np.zeros(16000, dtype=np.float32), sample_rate=16000)
        callback(-1, -1, "Detecting speech segments with VAD...")
        callback(0, segments, "Found", speech_timestamps=[{"start": 0.1, "end": 0.5}])
        for index in range(segments):
            callback(index, segments, transcription=f"text {index}", start_sec=0.1 * index, end_sec=0.1 * index + 0.1)
        callback(segments, segments)
        Path(video).with_suffix(".eaf").write_text("<eaf/>")
    return transcribe


def finished_signals(thread):
    seen = {"finished": [], "progress": [], "audio": [], "segments": [], "vad": [], "updates": []}
    thread.finished.connect(lambda ok, msg: seen["finished"].append((ok, msg)))
    thread.progress.connect(seen["progress"].append)
    thread.audio_loaded.connect(lambda signal, rate: seen["audio"].append((len(signal), rate)))
    thread.segment_transcribed.connect(lambda a, b, text: seen["segments"].append(text))
    thread.vad_segments_detected.connect(seen["vad"].append)
    thread.progress_update.connect(lambda *a: seen["updates"].append(a))
    return seen


# --- TranscriptionThread ---------------------------------------------------------------------------------------------

def test_thread_forwards_progress_and_moves_the_eaf(qapp, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe(calls))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    target = tmp_path / "chosen.eaf"
    target.write_text("old")
    thread = tgui.TranscriptionThread(str(video), "host", 1234, str(target), padding_ms=50, tier_name="T")
    seen = finished_signals(thread)
    thread.run()
    assert seen["finished"] == [(True, "Transcription finished successfully.")] or seen["finished"][0][0] is True
    assert seen["audio"] == [(16000, 16000)] and seen["segments"] == ["text 0", "text 1"] and len(seen["vad"]) == 1
    assert target.read_text() == "<eaf/>" and not video.with_suffix(".eaf").exists()
    assert calls[0][1:3] == ("host", 1234) and calls[0][3]["padding_ms"] == 50 and calls[0][3]["tier_name"] == "T"


def test_thread_without_target_keeps_default_eaf(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe([]))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    thread = tgui.TranscriptionThread(str(video), "h", 1)
    seen = finished_signals(thread)
    thread.run()
    assert seen["finished"][0][0] is True and video.with_suffix(".eaf").exists()


def test_thread_reports_errors_and_stops(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe([], fail="server exploded"))
    thread = tgui.TranscriptionThread(str(tmp_path / "v.mp4"), "h", 1)
    seen = finished_signals(thread)
    thread.run()
    assert seen["finished"] == [(False, "server exploded")]

    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe([]))
    (tmp_path / "v.mp4").write_bytes(b"x")
    stopped = tgui.TranscriptionThread(str(tmp_path / "v.mp4"), "h", 1)
    seen = finished_signals(stopped)
    stopped.stop()
    stopped.run()
    assert seen["finished"][0][0] is False


def test_server_log_thread_emits_each_line():
    import io

    process = types.SimpleNamespace(stdout=io.StringIO("one\n\n two \n"))
    thread = tgui.ServerLogThread(process)
    lines = []
    thread.log_emitted.connect(lines.append)
    thread.run()
    assert lines == ["one", "", "two"]
    tgui.ServerLogThread(None).run()


def test_thumbnail_worker_delivers_a_scaled_image(qapp):
    worker = tgui.ThumbnailWorker(str(EXAMPLE_VIDEOS[0]))
    received = []
    worker.signals.finished.connect(lambda path, image: received.append((path, image)))
    worker.run()
    assert received and received[0][1].width() <= 100 and received[0][1].height() <= 75
    broken = tgui.ThumbnailWorker("/nope.mp4")
    broken.signals.finished.connect(lambda *a: received.append("unexpected"))
    broken.run()
    assert "unexpected" not in received


# --- waveform --------------------------------------------------------------------------------------------------------

def test_waveform_widget_state_and_painting(qapp):
    widget = tgui.WaveformWidget()
    widget.resize(800, 150)
    assert not widget.grab().isNull()  # "No Audio Data"
    widget.set_audio(np.sin(np.linspace(0, 200, 16000 * 4)).astype(np.float32), 16000)
    assert widget.width() == 4 * widget.pixels_per_second
    widget.set_vad_segments([{"start": 0.5, "end": 1.5}])
    widget.add_segment(0.5, 1.5, "hello world")
    widget.set_scale(2.0)
    assert widget.width() == 4 * widget.pixels_per_second * 2 and not widget.grab().isNull()
    widget.clear()
    assert widget.audio_signal is None and widget.vad_segments == [] and widget.transcribed_segments == {}


# --- window ----------------------------------------------------------------------------------------------------------

def test_server_check_updates_the_status(qapp, home, monkeypatch):
    window = tgui.WhisperGui()
    monkeypatch.setattr(tgui.requests, "get", lambda url, timeout=0: types.SimpleNamespace(status_code=200))
    assert window.check_server()
    assert "Online" in window.server_status_label.text() or "online" in window.server_status_label.text().lower()
    monkeypatch.setattr(tgui.requests, "get", lambda url, timeout=0: types.SimpleNamespace(status_code=500))
    assert not window.check_server() and "500" in window.server_status_label.text()

    def refuse(url, timeout=0):
        raise ConnectionError

    monkeypatch.setattr(tgui.requests, "get", refuse)
    assert not window.check_server()
    window.shutdown()


def test_settings_round_trip(whisper, home):
    whisper.server_url.setText("10.0.0.5")
    whisper.padding.setValue(321)
    whisper.tier_name_input.setText("Talk")
    whisper.save_settings()
    assert json.loads((home / ".whisper_gui_settings.json").read_text())["padding"] == 321

    other = tgui.WhisperGui()
    assert other.server_url.text() == "10.0.0.5" and other.padding.value() == 321 and other.tier_name_input.text() == "Talk"
    other.shutdown()


def test_broken_settings_file_is_ignored(qapp, home):
    (home / ".whisper_gui_settings.json").write_text("{broken")
    window = tgui.WhisperGui()
    assert window.padding.value() == 200
    window.shutdown()


def test_single_transcription_end_to_end(whisper, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe(calls))
    messages = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: messages.append(a[2])))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    whisper.video_input.setText(str(video))
    whisper.eaf_output.setText(str(tmp_path / "out.eaf"))

    whisper.start_transcription()
    deadline = 100
    while whisper.transcription_running() and deadline:
        spin(50)
        deadline -= 1
    spin(100)

    assert (tmp_path / "out.eaf").exists() and whisper.progress_bar.value() == 100
    assert whisper.start_btn.isEnabled() and not whisper.stop_btn.isEnabled() and whisper.show_finder_btn.isEnabled()
    assert messages and whisper.waveform_widget.audio_signal is not None


def test_failed_transcription_shows_the_error(whisper, tmp_path, monkeypatch):
    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe([], fail="boom"))
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a[2])))
    (tmp_path / "v.mp4").write_bytes(b"x")
    whisper.video_input.setText(str(tmp_path / "v.mp4"))
    whisper.start_transcription()
    for _ in range(100):
        if not whisper.transcription_running():
            break
        spin(50)
    spin(100)
    assert errors and "boom" in errors[0] and whisper.progress_bar.value() == 0


def test_start_without_a_video_warns(whisper, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    whisper.start_transcription()
    assert len(warned) == 1 and not whisper.transcription_running()


def test_offline_server_can_be_declined_or_fail_to_start(whisper, tmp_path, monkeypatch):
    def refuse(url, timeout=0):
        raise ConnectionError

    monkeypatch.setattr(tgui.requests, "get", refuse)
    (tmp_path / "v.mp4").write_bytes(b"x")
    whisper.video_input.setText(str(tmp_path / "v.mp4"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    whisper.start_transcription()
    assert not whisper.transcription_running()

    errors = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a[2])))
    monkeypatch.setattr(whisper, "start_local_server", lambda: None)
    whisper.start_transcription()
    assert errors and not whisper.transcription_running()


def test_local_server_command_uses_the_chosen_model(whisper, monkeypatch):
    commands = []

    class FakeProcess:
        stdout = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    monkeypatch.setattr(tgui.subprocess, "Popen", lambda cmd, **kwargs: commands.append(cmd) or FakeProcess())
    monkeypatch.setattr(tgui.time, "sleep", lambda s: None)
    informed = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: informed.append(a[2])))
    whisper.whisper_model.setCurrentText("small")
    whisper.start_local_server()  # the fake server already "answers", so it is not started again
    assert informed and commands == []

    def refuse(url, timeout=0):
        raise ConnectionError

    monkeypatch.setattr(tgui.requests, "get", refuse)
    whisper.start_local_server()
    assert commands and commands[0][-4:-2] == ["--model", "openai/whisper-small"] and "--language" in commands[0]
    assert whisper.is_busy()
    whisper.whisper_model.setCurrentText("my/custom-model")
    whisper.start_local_server()
    assert "my/custom-model" in commands[-1]


def test_frozen_app_starts_the_server_through_its_own_flag(whisper, monkeypatch):
    from analysis_helper.transcriber import WHISPER_SERVER_FLAG

    commands = []
    monkeypatch.setattr(tgui.subprocess, "Popen",
                        lambda cmd, **kwargs: commands.append(cmd) or types.SimpleNamespace(stdout=None, poll=lambda: 0))
    monkeypatch.setattr(tgui.time, "sleep", lambda s: None)
    monkeypatch.setattr(tgui.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    whisper.start_local_server()
    assert commands[0][1] == WHISPER_SERVER_FLAG


def test_progress_bar_modes(whisper):
    whisper.update_progress_bar(1, 4, "")
    assert whisper.progress_bar.value() == 25
    whisper.update_progress_bar(-1, -1, "busy")
    assert whisper.progress_bar.maximum() == 0
    whisper.update_progress_bar(0, 0, "")
    assert whisper.progress_bar.maximum() == 100


def test_zoom_slider_scales_the_waveform(whisper):
    whisper.waveform_widget.set_audio(np.zeros(16000 * 2, dtype=np.float32), 16000)
    whisper.zoom_slider.setValue(20)
    whisper.update_waveform_scale()
    assert whisper.waveform_widget.scale == 2.0


def test_stop_asks_the_running_thread_to_stop(whisper):
    stopped = []
    whisper.transcription_thread = types.SimpleNamespace(isRunning=lambda: True, stop=lambda: stopped.append(1))
    whisper.stop_transcription()
    assert stopped == [1] and not whisper.stop_btn.isEnabled()
    whisper.transcription_thread = types.SimpleNamespace(isRunning=lambda: False)
    whisper.stop_transcription()
    assert stopped == [1]


def test_toggle_buttons_show_and_hide_panels(whisper):
    whisper.server_log_btn.setChecked(True)
    whisper.toggle_server_log()
    assert not whisper.server_log_widget.isHidden()
    whisper.server_log_btn.setChecked(False)
    whisper.toggle_server_log()
    assert whisper.server_log_widget.isHidden()
    whisper.advanced_btn.setChecked(True)
    whisper.toggle_advanced()
    assert not whisper.advanced_widget.isHidden()
    whisper.advanced_btn.setChecked(False)
    whisper.toggle_advanced()
    assert whisper.advanced_widget.isHidden()


def test_browse_dialogs(whisper, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(EXAMPLE_VIDEOS[0]), "")))
    whisper.browse_video()
    assert whisper.video_input.text() == str(EXAMPLE_VIDEOS[0]) and whisper.eaf_output.text().endswith(".eaf")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "x.eaf"), "")))
    whisper.browse_eaf()
    assert whisper.eaf_output.text().endswith("x.eaf")
    whisper.video_input.setText("/missing.mp4")
    whisper.update_preview()
    whisper.video_input.setText(str(EXAMPLE_VIDEOS[0]))
    whisper.update_preview()
    assert whisper.preview_label.pixmap() is not None and not whisper.preview_label.pixmap().isNull()


def test_show_in_finder_and_open_folder(whisper, monkeypatch, tmp_path):
    ran, warned = [], []
    monkeypatch.setattr(tgui.subprocess, "run", lambda cmd, *a, **k: ran.append(cmd))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    whisper.eaf_output.setText(str(tmp_path / "missing.eaf"))
    whisper.show_in_finder()
    whisper.folder_input.setText("")
    whisper.open_batch_folder()
    assert len(warned) == 2 and ran == []
    (tmp_path / "found.eaf").write_text("x")
    whisper.eaf_output.setText(str(tmp_path / "found.eaf"))
    whisper.folder_input.setText(str(tmp_path))
    for platform in ("darwin", "linux", "win32"):
        monkeypatch.setattr(tgui.sys, "platform", platform)
        whisper.show_in_finder()
        whisper.open_batch_folder()
    assert len(ran) == 6


# --- batch mode ------------------------------------------------------------------------------------------------------

def test_folder_scan_lists_media_files_recursively(whisper, tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.mp4", "sub/b.WAV", "c.txt", "d.flac"):
        (tmp_path / name).write_bytes(b"x")
    whisper.folder_input.setText(str(tmp_path))
    whisper.check_folder()
    names = [whisper.video_list.item(i).text() for i in range(whisper.video_list.count())]
    assert names == ["a.mp4", "d.flac", str(Path("sub") / "b.WAV")]
    spin(300)


def test_folder_scan_warns_for_bad_or_empty_folders(whisper, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append("warning")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append("info")))
    whisper.folder_input.setText(str(tmp_path / "nope"))
    whisper.check_folder()
    whisper.folder_input.setText(str(tmp_path))
    whisper.check_folder()
    assert shown == ["warning", "info"]


def test_batch_transcribes_every_file_and_marks_them(whisper, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tgui, "transcribe_video", fake_transcribe(calls, segments=1))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).write_bytes(b"x")
    whisper.folder_input.setText(str(tmp_path))
    whisper.check_folder()
    spin(300)
    whisper.start_batch()
    for _ in range(200):
        if whisper.start_batch_btn.isEnabled():
            break
        spin(50)
    spin(100)
    assert [Path(c[0]).name for c in calls] == ["a.mp4", "b.mp4"]
    assert all(whisper.video_list.item(i).text().endswith("Done") or "Fertig" in whisper.video_list.item(i).text()
               for i in range(2))
    assert whisper.progress_bar.value() == 100


def test_batch_continues_after_a_failed_file(whisper, tmp_path, monkeypatch):
    results = iter(["boom", None])

    def sometimes_fail(video, url, port, **kwargs):
        error = next(results)
        if error:
            raise RuntimeError(error)
        fake_transcribe([], segments=1)(video, url, port, **kwargs)

    monkeypatch.setattr(tgui, "transcribe_video", sometimes_fail)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).write_bytes(b"x")
    whisper.folder_input.setText(str(tmp_path))
    whisper.check_folder()
    spin(300)
    whisper.video_list.item(0).setSelected(True)
    whisper.video_list.item(1).setSelected(True)
    whisper.start_batch()
    for _ in range(200):
        if whisper.start_batch_btn.isEnabled():
            break
        spin(50)
    spin(100)
    assert "Failed: boom" in whisper.log_output.toPlainText() or "boom" in whisper.log_output.toPlainText()
    assert whisper.start_batch_btn.isEnabled()


def test_batch_without_files_warns(whisper, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    whisper.start_batch()
    assert len(warned) == 1


# --- ELAN helper -----------------------------------------------------------------------------------------------------

def test_send_transcription_request_returns_text_or_raises(monkeypatch):
    from analysis_helper.transcriber import transcribe_video_to_elan as elan

    sent = []

    def post(url, json):
        sent.append((url, json))
        return types.SimpleNamespace(status_code=200, json=lambda: {"transcription_text": "hi"})

    monkeypatch.setattr(elan.requests, "post", post)
    assert elan.send_transcription_request("http://h:1", np.zeros(4, dtype=np.float32), 16000) == "hi"
    assert sent[0][0] == "http://h:1/transcribe/" and sent[0][1]["audio_rate"] == 16000 and len(sent[0][1]["signal"]) == 4

    monkeypatch.setattr(elan.requests, "post", lambda url, json: types.SimpleNamespace(status_code=500, text="bad"))
    with pytest.raises(Exception, match="500"):
        elan.send_transcription_request("http://h:1", np.zeros(4, dtype=np.float32), 16000)


def test_transcribe_video_needs_an_existing_file(tmp_path):
    from analysis_helper.transcriber.transcribe_video_to_elan import transcribe_video

    with pytest.raises(AssertionError):
        transcribe_video(tmp_path / "missing.mp4", "h", 1)


def test_cli_passes_its_arguments_on(monkeypatch):
    from analysis_helper.transcriber import transcribe_video_to_elan as elan

    calls = []
    monkeypatch.setattr(elan, "transcribe_video", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(sys, "argv", ["annotate", "-u", "1.2.3.4", "-p", "99", "-vp", "v.mp4", "--padding", "10",
                                      "--vad_threshold", "0.5", "--min_speech_duration", "7", "--tier_name", "T"])
    elan.main()
    assert calls == [(("v.mp4", "1.2.3.4", 99), {"padding_ms": 10, "vad_threshold": 0.5, "min_speech_duration_ms": 7,
                                                   "tier_name": "T"})]


def test_stop_event_aborts_transcription_before_the_first_request(tmp_path, monkeypatch):
    from analysis_helper.transcriber import transcribe_video_to_elan as elan
    from conftest import requires_ffmpeg  # noqa: F401
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    requests_sent = []
    monkeypatch.setattr(elan.requests, "post", lambda *a, **k: requests_sent.append(1))
    video = tmp_path / "clip.mp4"
    shutil.copy(EXAMPLE_VIDEOS[0], video)
    elan.transcribe_video(video, "h", 1, stop_event=types.SimpleNamespace(is_set=lambda: True))
    assert requests_sent == [] and not video.with_suffix(".eaf").exists()


def stoppable_transcribe(calls, started):
    """Waits in the first request until it is stopped, as a long video would."""
    def transcribe(video, url, port, **kwargs):
        calls.append(Path(video).name)
        started.set()
        kwargs["stop_event"].wait(10)
    return transcribe


def test_stopping_a_batch_ends_the_whole_batch(whisper, tmp_path, monkeypatch):
    # batch: running -> stopped (not: on to the next file)
    import threading

    calls, started, shown = [], threading.Event(), []
    monkeypatch.setattr(tgui, "transcribe_video", stoppable_transcribe(calls, started))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[1])))
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (tmp_path / name).write_bytes(b"x")
    whisper.folder_input.setText(str(tmp_path))
    whisper.check_folder()
    spin(300)
    whisper.start_batch()
    assert started.wait(5)
    whisper.stop_transcription()
    for _ in range(200):
        if whisper.start_batch_btn.isEnabled():
            break
        spin(50)
    spin(300)
    assert calls == ["a.mp4"]
    assert whisper.start_batch_btn.isEnabled() and whisper.start_btn.isEnabled() and not whisper.stop_btn.isEnabled()
    texts = [whisper.video_list.item(i).text() for i in range(3)]
    assert "Gestoppt" in texts[0] and not any("Fertig" in t or "Fehlgeschlagen" in t for t in texts)
    assert shown == []   # no "all files processed"


def test_stopping_a_single_transcription_is_no_error(whisper, tmp_path, monkeypatch):
    import threading

    calls, started, errors = [], threading.Event(), []
    monkeypatch.setattr(tgui, "transcribe_video", stoppable_transcribe(calls, started))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a[2])))
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    whisper.video_input.setText(str(video))
    whisper.start_transcription()
    assert started.wait(5)
    whisper.stop_transcription()
    for _ in range(200):
        if whisper.start_btn.isEnabled():
            break
        spin(50)
    assert errors == [] and whisper.start_btn.isEnabled()
    assert whisper.progress_label.text() == "Gestoppt"
