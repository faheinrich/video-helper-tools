"""Camera makers' own metadata boxes (Nikon NCDT and the like) reach the compressed result byte for byte."""
import struct
import subprocess

import pytest

from conftest import requires_exiftool, requires_ffmpeg
from test_video_output import compress, make_clip

pytestmark = requires_ffmpeg


def nikon_box(lens="NIKKOR Z 24-70mm f/4 S"):
    """An NCDT box as Nikon cameras write it; exiftool decodes it as group "Nikon"."""
    from archive_helper.compressor.boxes import make_box

    def record(tag, fmt, payload):
        return struct.pack(">IHH", tag, fmt, len(payload) // {2: 1, 5: 8}[fmt]) + payload

    def text(value):
        return (value + "\0").encode()

    tags = (record(0x01, 2, text("NIKON CORPORATION")) + record(0x02, 2, text("NIKON Z 6_2"))
            + record(0x110a434, 2, text(lens)) + record(0x110920a, 5, struct.pack(">II", 35, 1))
            + record(0x110829d, 5, struct.pack(">II", 40, 10)) + record(0x110a431, 2, text("6012345")))
    return make_box(b"NCDT", make_box(b"NCHD", b"0100\0") + make_box(b"NCTG", tags))


def with_boxes(path, *udta_boxes, moov_boxes=(), top_boxes=()):
    """Puts boxes into an existing video, the way a camera would have written them."""
    from archive_helper.compressor.boxes import copy_maker_boxes, make_box

    donor = path.with_name(path.stem + "_donor.mp4")
    donor.write_bytes(make_box(b"moov", b"".join(moov_boxes) + make_box(b"udta", b"".join(udta_boxes)))
                      + b"".join(top_boxes))
    copy_maker_boxes(donor, path)
    donor.unlink()
    return path


def nikon_tags(path):
    out = subprocess.run(["exiftool", "-s", "-s", "-s", "-Nikon:LensModel", "-Nikon:SerialNumber", "-Nikon:FocalLength",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return out.split("\n")[:3]


@requires_exiftool
def test_nikon_lens_and_serial_number_reach_the_result(qapp, tmp_path):
    from archive_helper.compressor.utils import read_compression_settings

    src = tmp_path / "src"
    src.mkdir()
    with_boxes(make_clip(src / "DSC_0001.MOV", size="640x360"), nikon_box())
    assert nikon_tags(src / "DSC_0001.MOV") == ["NIKKOR Z 24-70mm f/4 S", "6012345", "35.0 mm"]

    out, message, data = compress(qapp, src, "DSC_0001.MOV")

    assert message.startswith("✅")
    assert nikon_tags(out) == ["NIKKOR Z 24-70mm f/4 S", "6012345", "35.0 mm"]
    assert "metadata_missing" not in read_compression_settings([out])[str(out)]
    # the result still plays to the end
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True, text=True)
    assert decode.returncode == 0 and decode.stderr == ""


@requires_exiftool
def test_metadata_that_does_not_reach_the_result_is_reported(qapp, tmp_path, monkeypatch):
    from archive_helper.compressor import workers
    from archive_helper.compressor.model import VideoRow, status_label
    from archive_helper.compressor.utils import read_compression_settings

    src = tmp_path / "src"
    src.mkdir()
    with_boxes(make_clip(src / "DSC_0002.MOV", size="640x360"), nikon_box())
    monkeypatch.setattr(workers, "copy_maker_boxes", lambda src, dst: [])   # as without the box copy

    out, message, data = compress(qapp, src, "DSC_0002.MOV")

    missing = read_compression_settings([out])[str(out)]["metadata_missing"]
    assert {"Nikon:LensModel", "Nikon:SerialNumber", "Nikon:FocalLength"} <= set(missing)
    assert data["settings"]["metadata_missing"] == missing
    row = VideoRow(src=src / "DSC_0002.MOV", dst=out, size=1, status="done", out_size=1, settings=data["settings"])
    assert "Metadaten?" in status_label(row)


@requires_exiftool
def test_a_plain_video_reports_nothing_missing(qapp, tmp_path):
    from archive_helper.compressor.utils import missing_metadata

    src = tmp_path / "src"
    src.mkdir()
    clip = make_clip(src / "plain.mov", size="640x360")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Keys:Make=Apple", "-Keys:Model=iPhone 15",
                    "-Keys:CreationDate=2025:06:15 19:20:57+09:00", "-XMP-dc:Title=Hafen", str(clip)], check=True)
    out, _, data = compress(qapp, src, "plain.mov")
    assert missing_metadata(clip, out) == []
    assert "metadata_missing" not in data["settings"]


def test_boxes_already_in_the_result_are_not_doubled_and_uuid_boxes_come_along(tmp_path):
    from archive_helper.compressor.boxes import copy_maker_boxes, make_box, maker_boxes

    canon = make_box(b"uuid", bytes.fromhex("85c0b687820f11e08111f4ce462b6a48") + b"CNCV-data")
    top = make_box(b"uuid", bytes.fromhex("50524f46" + "21d24fcebb88695cfac9c740") + b"PROF-data")
    src = with_boxes(make_clip(tmp_path / "src.mov", size="320x240", seconds=1), nikon_box(),
                     moov_boxes=[canon], top_boxes=[top])
    dst = make_clip(tmp_path / "dst.mp4", size="320x240", seconds=1)

    assert sorted(copy_maker_boxes(src, dst)) == sorted(["NCDT", "uuid 85c0b687820f11e08111f4ce462b6a48",
                                                         "uuid 50524f4621d24fcebb88695cfac9c740"])
    assert copy_maker_boxes(src, dst) == []   # a second run adds nothing
    udta, moov_uuids, top_uuids = maker_boxes(dst)
    assert [raw for _, raw in udta if raw[4:8] == b"NCDT"] == [nikon_box()]
    assert [raw for _, raw in moov_uuids] == [canon] and [raw for _, raw in top_uuids] == [top]


def test_a_result_with_media_data_after_the_moov_box_is_left_alone(tmp_path):
    from archive_helper.compressor.boxes import BoxError, copy_maker_boxes

    src = with_boxes(make_clip(tmp_path / "src.mov", size="320x240", seconds=1), nikon_box())
    dst = make_clip(tmp_path / "fast.mp4", size="320x240", seconds=1, extra=("-movflags", "+faststart"))
    before = dst.read_bytes()
    with pytest.raises(BoxError):
        copy_maker_boxes(src, dst)
    assert dst.read_bytes() == before


def test_a_video_without_maker_boxes_is_not_touched(tmp_path):
    from archive_helper.compressor.boxes import copy_maker_boxes

    src = make_clip(tmp_path / "src.mov", size="320x240", seconds=1)
    dst = make_clip(tmp_path / "dst.mp4", size="320x240", seconds=1)
    before = dst.read_bytes()
    assert copy_maker_boxes(src, dst) == []
    assert dst.read_bytes() == before


@requires_exiftool
def test_timecode_and_subtitle_tracks_are_kept_and_a_lost_one_is_reported(qapp, tmp_path):
    from archive_helper.compressor.utils import side_tracks

    src = tmp_path / "src"
    src.mkdir()
    make_clip(src / "tc.mov", size="320x240", seconds=1, extra=("-timecode", "01:00:00:00"))
    sub = tmp_path / "sub.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(make_clip(tmp_path / "plain.mov", size="320x240", seconds=1)),
                    "-i", str(sub), "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text", str(src / "sub.mov")],
                   check=True)
    assert side_tracks(src / "sub.mov") == ["text"]   # QuickTime text track

    _, _, timecode = compress(qapp, src, "tc.mov")
    _, _, subtitled = compress(qapp, src, "sub.mov")

    assert "metadata_missing" not in timecode["settings"]   # ffmpeg writes the timecode track anew
    assert "metadata_missing" not in subtitled["settings"]  # carried over as it is
    assert side_tracks(tmp_path / "dst" / "sub_archived.mp4") == ["text"]
    _, _, test_run = compress(qapp, src, "sub.mov", dry_run=True, overwrite=True)
    assert test_run["settings"]["metadata_missing"] == ["Track:text"]   # a one-second test run takes no tracks


@requires_exiftool
def test_maker_boxes_and_tracks_come_over_together(qapp, tmp_path):
    from archive_helper.compressor.utils import side_tracks

    src = tmp_path / "src"
    src.mkdir()
    sub = tmp_path / "sub.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(make_clip(tmp_path / "plain.mov", size="320x240", seconds=1)),
                    "-i", str(sub), "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text", str(src / "both.mov")],
                   check=True)
    with_boxes(src / "both.mov", nikon_box())

    out, _, data = compress(qapp, src, "both.mov")

    assert "metadata_missing" not in data["settings"]
    assert side_tracks(out) == ["text"]
    assert nikon_tags(out)[0] == "NIKKOR Z 24-70mm f/4 S"
    subtitles = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:s", "-f", "srt", "-"],
                               capture_output=True, text=True, check=True).stdout
    assert "Hallo" in subtitles


@requires_exiftool
def test_the_metadata_of_the_video_track_comes_over_byte_for_byte(qapp, tmp_path):
    # Newer iPhones keep lens, focal length, aperture and maker notes in the video track's own keys; exiftool can
    # write only some of them anew (the aperture was lost).
    from archive_helper.compressor.boxes import find, iter_boxes, read_box, top_level_boxes, track_info

    def video_meta(path):
        moov = next(b for b in top_level_boxes(path) if b[0] == b"moov")
        data = read_box(path, moov)
        trak = next(t for t in iter_boxes(data, moov[2]) if t[0] == b"trak" and track_info(data, t)[1] == b"vide")
        box = find(data, trak, b"meta")
        return None if box is None else data[box[1]:box[1] + box[3]]

    src = tmp_path / "src"
    src.mkdir()
    clip = make_clip(src / "IMG_6.MOV", size="320x240", seconds=1)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-VideoKeys:LensModel=iPhone 15 Pro back camera 6.765mm f/1.78",
                    "-VideoKeys:FocalLengthIn35mmFormat=24", str(clip)], check=True)
    assert video_meta(clip) is not None

    out, _, data = compress(qapp, src, "IMG_6.MOV")

    assert video_meta(out) == video_meta(clip)
    assert "metadata_missing" not in data["settings"]


@requires_exiftool
def test_the_metadata_of_an_old_format_is_kept_as_standard_fields_and_as_text(qapp, tmp_path):
    # AVI and AVCHD keep their camera data in structures MP4 has no room for (Fujifilm, older Olympus, Panasonic).
    from archive_helper.compressor.utils import is_quicktime, read_compression_settings

    src = tmp_path / "src"
    src.mkdir()
    clip = src / "DSCF0073.AVI"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(make_clip(tmp_path / "plain.mov", size="320x240", seconds=1)),
                    "-c:v", "mjpeg", "-c:a", "pcm_s16le", "-metadata", "title=Nellie im Garten",
                    "-metadata", "copyright=Familie", "-metadata", "comment=FinePix", str(clip)], check=True)
    assert not is_quicktime(clip) and is_quicktime(tmp_path / "plain.mov")

    out, _, data = compress(qapp, src, "DSCF0073.AVI")

    record = read_compression_settings([out])[str(out)]
    assert "metadata_missing" not in record
    assert record["source_metadata"]["RIFF:Title"] == "Nellie im Garten"
    assert not any(name.endswith(("FrameRate", "SampleRate", "VideoCodec")) for name in record["source_metadata"])
    rights = subprocess.run(["exiftool", "-s", "-s", "-s", "-XMP-dc:Rights", str(out)], capture_output=True, text=True).stdout
    assert rights.strip() == "Familie"


def test_what_compressing_changes_by_its_nature_is_not_reported():
    from archive_helper.compressor.utils import expected_to_change

    for group, tag in [("RIFF", "FrameRate"), ("M2TS", "Duration"), ("IFD1", "ThumbnailOffset"), ("Track3", "Anything"),
                       ("QuickTime", "Duration"), ("Keys", "ApplePhotosOriginatingSignature")]:
        assert expected_to_change(group, tag), (group, tag)
    for group, tag in [("RIFF", "DateTimeOriginal"), ("Nikon", "LensModel"), ("FujiFilm", "Quality"), ("Keys", "Make")]:
        assert not expected_to_change(group, tag), (group, tag)
