"""Files the apps remove go to the system trash, so a mistake can be undone in Finder (or Explorer, or the desktop's
trash on Linux)."""
from pathlib import Path

from .i18n import tr


def _system_trash(path):
    """True when the file is in the trash now. Tests replace this so they never fill the real trash."""
    from PySide6.QtCore import QFile

    return bool(QFile.moveToTrash(str(path)))


def move_to_trash(path):
    """Moves a file to the trash. Raises OSError when it cannot; it never deletes the file instead."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(tr("{name} does not exist", name=path.name))
    if not _system_trash(path):
        raise OSError(tr("{name} could not be moved to the trash", name=path.name))
