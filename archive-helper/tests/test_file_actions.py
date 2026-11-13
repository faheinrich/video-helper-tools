"""The file actions on an original and its result, and undoing them (compressor/file_actions.py)."""
import pytest

from archive_helper.compressor import file_actions


def pair(tmp_path, same_folder=False):
    original = tmp_path / "src" / "clip.mov"
    result = (original.parent if same_folder else tmp_path / "dst") / "clip_archived.mp4"
    for path, data in ((original, b"original"), (result, b"result")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return original, result


def files(tmp_path):
    """Every file under tmp_path except the trash, with its content."""
    return {str(p.relative_to(tmp_path)): p.read_bytes() for p in sorted(tmp_path.rglob("*"))
            if p.is_file() and "Trash" not in p.parts}


@pytest.mark.parametrize("action", file_actions.ACTIONS)
@pytest.mark.parametrize("same_folder", [False, True])
def test_every_action_can_be_undone_to_the_byte(tmp_path, trash, action, same_folder):
    original, result = pair(tmp_path, same_folder)
    before = files(tmp_path)
    steps = file_actions.apply(action, original, result)
    assert files(tmp_path) != before
    file_actions.undo(steps)
    assert files(tmp_path) == before


def test_what_each_action_does(tmp_path, trash):
    original, result = pair(tmp_path)
    file_actions.apply("overwrite", original, result)
    assert not original.exists() and (original.parent / result.name).read_bytes() == b"result"
    assert (trash / "clip.mov").read_bytes() == b"original"

    original, result = pair(tmp_path / "2")
    file_actions.apply("swap", original, result)
    assert original.read_bytes() == b"result" and result.read_bytes() == b"original"


def test_replacing_keeps_another_file_of_the_results_name_and_brings_it_back(tmp_path, trash):
    original, result = pair(tmp_path)
    other = original.parent / result.name
    other.write_bytes(b"someone else's")
    steps = file_actions.apply("overwrite", original, result)
    assert other.read_bytes() == b"result"
    file_actions.undo(steps)
    assert other.read_bytes() == b"someone else's" and original.read_bytes() == b"original"


def test_undo_is_refused_without_a_change_when_the_trash_was_emptied(tmp_path, trash):
    original, result = pair(tmp_path)
    steps = file_actions.apply("overwrite", original, result)
    (trash / "clip.mov").unlink()   # emptied
    before = files(tmp_path)
    with pytest.raises(file_actions.UndoError, match="clip.mov"):
        file_actions.undo(steps)
    assert files(tmp_path) == before


def test_undo_is_refused_when_a_file_of_that_name_appeared(tmp_path, trash):
    original, result = pair(tmp_path)
    steps = file_actions.apply("del_orig", original, result)
    original.write_bytes(b"new")
    with pytest.raises(file_actions.UndoError):
        file_actions.undo(steps)
    assert original.read_bytes() == b"new"


def test_a_refusing_trash_changes_nothing(tmp_path, monkeypatch):
    from video_helper_core import trash as trash_module

    original, result = pair(tmp_path)
    before = files(tmp_path)
    monkeypatch.setattr(trash_module, "_system_trash", lambda path: None)
    for action in file_actions.ACTIONS:
        if action == "swap":
            continue   # moves only, no trash
        with pytest.raises(OSError):
            file_actions.apply(action, original, result)
        assert files(tmp_path) == before
