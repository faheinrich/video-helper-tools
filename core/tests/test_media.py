import subprocess

from video_helper_core.testing import requires_ffmpeg, spin


def make_clip(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    return path


@requires_ffmpeg
def test_media_player_plays_pauses_seeks_and_stops(qapp, tmp_path):
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from video_helper_core.media import MediaPlayer

    player = MediaPlayer()
    audio = QAudioOutput()
    player.setAudioOutput(audio)
    player.setSource(QUrl.fromLocalFile(str(make_clip(tmp_path / "clip.mp4"))))
    spin(500)
    player.play()
    spin(300)
    assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    player.setPosition(4000.0)   # a float from a calculation is fine, like with QMediaPlayer
    spin(300)
    assert 3900 <= player.position() < 5500
    player.pause()
    assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    player.stop()
    assert player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    player.setSource(QUrl())


def test_media_player_calls_go_through_invoke_method(qapp, monkeypatch):
    # invokeMethod lets go of the GIL, the plain QMediaPlayer methods do not (see MediaPlayer)
    from video_helper_core import media

    calls = []
    monkeypatch.setattr(media.QMetaObject, "invokeMethod", staticmethod(lambda obj, name, *rest: calls.append(name)))
    player = media.MediaPlayer()
    player.play()
    player.pause()
    player.setPosition(1500)
    player.stop()
    assert calls == ["play", "pause", "setPosition", "stop"]
