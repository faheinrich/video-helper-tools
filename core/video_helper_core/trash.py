"""Files the apps remove go to the system trash, so a mistake can be undone (in the app, or in Finder, Explorer or
the desktop's trash on Linux)."""
from pathlib import Path

from .i18n import tr


def _system_trash(path):
    """Where the file is in the trash now, or None when it could not be moved there. Tests replace this so they never
    fill the real trash."""
    from PySide6.QtCore import QFile

    file = QFile(str(path))
    if not file.moveToTrash():
        return None
    return file.fileName()   # Qt sets it to the file's place in the trash


def move_to_trash(path):
    """Moves a file to the trash and returns where it is there (None if the system does not say). Raises OSError when
    it cannot; it never deletes the file instead."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(tr("{name} does not exist", name=path.name))
    location = _system_trash(path)
    if not location:
        raise OSError(tr("{name} could not be moved to the trash", name=path.name))
    return Path(location) if isinstance(location, (str, Path)) else None
