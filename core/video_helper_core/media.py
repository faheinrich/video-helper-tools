"""A QMediaPlayer that cannot freeze the app when it is stopped, paused, moved or started."""
from PySide6.QtCore import Q_ARG, QMetaObject, Qt
from PySide6.QtMultimedia import QMediaPlayer


class MediaPlayer(QMediaPlayer):
    """QMediaPlayer whose play(), pause(), stop() and setPosition() run without holding Python's GIL.

    PySide holds the GIL during these calls. Meanwhile Qt's audio thread may be tearing down its objects: it then holds
    a Qt lock and asks the QAudioOutput, a Python object, whether Python overrides one of its methods, which needs the
    GIL. When the call on this side waits for that same lock, both wait forever (seen in tests on Linux and macOS:
    stop() while a video was playing). invokeMethod lets go of the GIL first; setSource() does that already.
    """

    def play(self):
        QMetaObject.invokeMethod(self, "play", Qt.DirectConnection)

    def pause(self):
        QMetaObject.invokeMethod(self, "pause", Qt.DirectConnection)

    def stop(self):
        QMetaObject.invokeMethod(self, "stop", Qt.DirectConnection)

    def setPosition(self, position):
        QMetaObject.invokeMethod(self, "setPosition", Qt.DirectConnection, Q_ARG("qint64", int(position)))
