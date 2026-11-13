"""The file actions on an original and its result (replace, swap, move to the trash), and undoing them.

Every action records the steps it took, so it can be undone: a file moved to the trash comes back from there, a
moved file is moved back, a file the action made goes to the trash. Nothing here is ever deleted for good.
"""
import shutil
from dataclasses import dataclass
from pathlib import Path

from video_helper_core.i18n import tr
from video_helper_core.trash import move_to_trash

ACTIONS = ("overwrite", "swap", "del_orig", "del_comp")


@dataclass
class Step:
    """One change to the file system: `trashed` (path is now at `where` in the trash), `moved` (path to `where`)
    or `created` (a file the action made at path)."""
    kind: str
    path: Path
    where: Path | None = None


class UndoError(OSError):
    pass


def apply(action, original, result):
    """Does the action and returns its steps. Raises OSError; then the files are as they were (as far as the trash
    allows: a failure there leaves everything in place)."""
    original, result = Path(original), Path(result)
    if action == "overwrite":
        return replace_original(original, result)
    if action == "swap":
        return swap(original, result)
    if action == "del_orig":
        return [trash(original)]
    if action == "del_comp":
        return [trash(result)]
    raise ValueError(action)


def trash(path):
    return Step("trashed", path, move_to_trash(path))


def replace_original(original, result):
    """The result takes the original's place (with the result's name); the original goes to the trash."""
    new_original = original.with_name(result.name)
    if new_original.exists() and new_original.samefile(result):   # the result is already next to it
        return [trash(original)]
    # Copied under a temporary name first: the original (and any other file with the result's name) only goes to
    # the trash once the copy is complete, and is never overwritten in place, even when the result has the very
    # same name.
    temp = new_original.with_name(f".{new_original.name}.replacing")
    shutil.copy2(result, temp)
    steps = []
    try:
        steps.append(trash(original))
        if new_original.exists():
            steps.append(trash(new_original))
    except OSError:
        temp.unlink(missing_ok=True)
        undo(steps)
        raise
    temp.replace(new_original)
    steps.append(Step("created", new_original))
    return steps


def swap(original, result):
    temp = original.with_name(original.name + ".tmp")
    steps = []
    try:
        for source, target in ((original, temp), (result, original), (temp, result)):
            shutil.move(source, target)
            steps.append(Step("moved", source, target))
    except OSError:
        undo(steps)
        raise
    return steps


def undo_problem(steps):
    """Why the steps cannot be undone, or "" when they can: checked before anything is changed, by playing the undo
    through on a model of which paths exist (a swap frees and fills the same names in turn)."""
    added, removed = set(), set()

    def exists(path):
        return path in added or (path not in removed and path.exists())

    def take(source, target):
        removed.add(source)
        added.discard(source)
        added.add(target)
        removed.discard(target)

    for step in reversed(steps):
        if step.kind == "created":
            if not exists(step.path):
                return tr("{name} is gone.", name=step.path.name)
            removed.add(step.path)
            added.discard(step.path)
            continue
        if step.kind == "trashed" and (step.where is None or not step.where.exists()):
            return tr("{name} is no longer in the trash.", name=step.path.name)
        if step.kind == "moved" and not exists(step.where):
            return tr("{name} is gone.", name=step.where.name)
        if exists(step.path):
            return tr("There is a file named {name} again.", name=step.path.name)
        take(step.where, step.path)
    return ""


def undo(steps):
    """Takes the steps back, the last first. Raises UndoError, before changing anything, when that is not possible
    (the trash was emptied, a file of that name appeared)."""
    problem = undo_problem(steps)
    if problem:
        raise UndoError(problem)
    for step in reversed(steps):
        if step.kind == "trashed":
            shutil.move(step.where, step.path)
        elif step.kind == "moved":
            shutil.move(step.where, step.path)
        elif step.kind == "created":
            move_to_trash(step.path)   # a copy the action made: to the trash too, never deleted
