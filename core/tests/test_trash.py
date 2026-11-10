import pytest


def test_a_file_goes_to_the_trash(tmp_path, trash):
    from video_helper_core.trash import move_to_trash

    video = tmp_path / "clip.mov"
    video.write_bytes(b"data")
    move_to_trash(video)
    assert not video.exists() and (trash / "clip.mov").read_bytes() == b"data"


def test_a_file_the_trash_refuses_is_kept_not_deleted(tmp_path, monkeypatch):
    from video_helper_core import trash as trash_module

    video = tmp_path / "clip.mov"
    video.write_bytes(b"data")
    monkeypatch.setattr(trash_module, "_system_trash", lambda path: False)
    with pytest.raises(OSError, match="clip.mov"):
        trash_module.move_to_trash(video)
    assert video.read_bytes() == b"data"


def test_a_missing_file_is_an_error(tmp_path):
    from video_helper_core.trash import move_to_trash

    with pytest.raises(FileNotFoundError):
        move_to_trash(tmp_path / "gone.mov")
