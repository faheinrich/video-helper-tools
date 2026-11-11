"""Carries the camera makers' own metadata boxes of an MP4/MOV over to the compressed result, byte for byte.

Nikon (NCDT), Olympus (OLYM, TAGS, scrn), Panasonic (PANA), Fujifilm (MVTG), Canon (uuid) and others keep lens,
exposure, serial number and picture profile in boxes of their own. exiftool reads them but cannot write them into
videos, and ffmpeg drops them. exiftool leaves boxes it does not write untouched, so copying them over before it runs
keeps them readable in the result exactly as in the original.

Only boxes that hold no offsets into the media data are copied (user data and uuid boxes): the video and sound of
the result are ffmpeg's, so the original's sample tables mean nothing there.
"""
import os
import shutil
import struct
from pathlib import Path

XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")   # exiftool writes XMP itself
# Written by ffmpeg (meta: encoder) or exiftool (XMP_) into the result; everything else in udta is the camera's.
OWN_UDTA = {b"meta", b"XMP_"}


def copied_by_exiftool(kind):
    """The standard QuickTime text boxes (©mak, ©mod, ©xyz, ©day ...): exiftool copies them as tags itself."""
    return kind[:1] == b"\xa9"


class BoxError(ValueError):
    pass


def iter_boxes(data, start=0, end=None):
    """(type, start, header size, total size) of the boxes in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break   # QuickTime ends some lists with four zero bytes; anything else malformed ends the walk too
        yield kind, pos, header, size
        pos += size


def top_level_boxes(path):
    """(type, offset, header size, size) of the file's top-level boxes, reading only their headers."""
    boxes = []
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = 0
        while pos + 8 <= file_size:
            f.seek(pos)
            head = f.read(16)
            size, kind = struct.unpack(">I4s", head[:8])
            header = 8
            if size == 1:
                size = struct.unpack(">Q", head[8:16])[0]
                header = 16
            elif size == 0:
                size = file_size - pos
            if size < header or pos + size > file_size:
                break
            boxes.append((kind, pos, header, size))
            pos += size
    return boxes


def read_box(path, box):
    with open(path, "rb") as f:
        f.seek(box[1])
        return f.read(box[3])


def make_box(kind, payload):
    if len(payload) + 8 > 0xFFFFFFFF:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def box_key(data, box):
    """What identifies a box for 'already there': its type, and for uuid boxes also the uuid."""
    kind, start, header, _ = box
    return kind + data[start + header:start + header + 16] if kind == b"uuid" else kind


def maker_boxes(path):
    """The boxes of path worth carrying over: (udta children, moov-level uuid boxes, top-level uuid boxes)."""
    top = top_level_boxes(path)
    moov = next((b for b in top if b[0] == b"moov"), None)
    udta_children, moov_uuids = [], []
    if moov is not None:
        data = read_box(path, moov)
        for box in iter_boxes(data, moov[2]):
            if box[0] == b"udta":
                for child in iter_boxes(data, box[1] + box[2], box[1] + box[3]):
                    if child[0] not in OWN_UDTA and not copied_by_exiftool(child[0]):
                        udta_children.append((box_key(data, child), data[child[1]:child[1] + child[3]]))
            elif box[0] == b"uuid" and data[box[1] + box[2]:box[1] + box[2] + 16] != XMP_UUID:
                moov_uuids.append((box_key(data, box), data[box[1]:box[1] + box[3]]))
    top_uuids = []   # uuid boxes, and meta boxes such as Sony's NonRealTimeMeta XML
    for box in top:
        if box[0] in (b"uuid", b"meta") and box[3] < 64 * 1024 * 1024:
            data = read_box(path, box)
            if box[0] == b"meta" or data[box[2]:box[2] + 16] != XMP_UUID:
                top_uuids.append((box_key(data, (box[0], 0, box[2], box[3])), data))
    return udta_children, moov_uuids, top_uuids


def copy_maker_boxes(src_path, dst_path):
    """Copies src's maker boxes into dst (an MP4 just written by ffmpeg) where dst has none of that kind yet.
    Returns the names of the boxes copied. Raises BoxError when dst cannot be changed safely."""
    udta_children, moov_uuids, top_uuids = maker_boxes(src_path)
    if not (udta_children or moov_uuids or top_uuids):
        return []
    dst_path = Path(dst_path)
    top = top_level_boxes(dst_path)
    moov = next((b for b in top if b[0] == b"moov"), None)
    if moov is None:
        raise BoxError("no moov box")
    data = read_box(dst_path, moov)
    children, udta_payload, have = [], b"", set()
    for box in iter_boxes(data, moov[2]):
        raw = data[box[1]:box[1] + box[3]]
        if box[0] == b"udta":
            for child in iter_boxes(data, box[1] + box[2], box[1] + box[3]):
                have.add(box_key(data, child))
                udta_payload += data[child[1]:child[1] + child[3]]
            continue
        if box[0] == b"uuid":
            have.add(box_key(data, box))
        children.append((raw, box[0] == b"trak"))
    copied = []
    for key, raw in udta_children:
        if key not in have:
            udta_payload += raw
            have.add(key)
            copied.append(key[:4].decode("latin-1"))
    for key, raw in moov_uuids:
        if key not in have:
            children.append((raw, False))
            have.add(key)
            copied.append("uuid " + key[4:].hex())
    top_have = {box_key(read_box(dst_path, b), (b[0], 0, b[2], b[3])) for b in top if b[0] in (b"uuid", b"meta")}
    tail = [raw for key, raw in top_uuids if key not in top_have]
    copied += [("uuid " + key[4:].hex()) if key[:4] == b"uuid" else key.decode("latin-1")
               for key, _ in top_uuids if key not in top_have]
    if not copied:
        return []

    write_moov(dst_path, top, moov, children + [(make_box(b"udta", udta_payload), False)], tail=tail)
    return copied


# ---------------------------------------------------------------------------------------------------------------------
# Tracks of their own: per-frame metadata (Apple mebx: faces, orientation, Live Photo data; GoPro gpmd, Sony rtmd:
# GPS and telemetry) and subtitles. ffmpeg cannot write most of them, so the whole track is carried over: its samples
# go into a media data box of their own in front of moov, and its tables are pointed there.

PICTURE_AND_SOUND = {b"vide", b"soun", b"tmcd", b"hint"}   # tmcd: ffmpeg writes the timecode track itself
TRAK_CONTAINERS = {b"trak", b"mdia", b"minf", b"stbl", b"edts"}


def payload(data, box):
    return data[box[1] + box[2]:box[1] + box[3]]


def find(data, box, *path):
    """The box at path below box, or None."""
    for kind in path:
        box = next((b for b in iter_boxes(data, box[1] + box[2], box[1] + box[3]) if b[0] == kind), None)
        if box is None:
            return None
    return box


def track_info(data, trak):
    """(track id, handler type, sample entry type) of a trak box."""
    tkhd, hdlr = find(data, trak, b"tkhd"), find(data, trak, b"mdia", b"hdlr")
    stsd = find(data, trak, b"mdia", b"minf", b"stbl", b"stsd")
    if tkhd is None or hdlr is None:
        return None, None, None
    head = payload(data, tkhd)
    track_id = struct.unpack(">I", head[20:24] if head[0] == 1 else head[12:16])[0]
    handler = payload(data, hdlr)[8:12]
    entry = payload(data, stsd)[12:16] if stsd is not None else b""
    return track_id, handler, entry


def movie_header(data, moov):
    """(timescale, next track id, offset of next track id in data) of the moov's mvhd."""
    mvhd = find(data, moov, b"mvhd")
    start = mvhd[1] + mvhd[2]
    version = data[start]
    timescale = struct.unpack(">I", data[start + (20 if version == 1 else 12):][:4])[0]
    next_at = start + (108 if version == 1 else 96)
    return timescale, struct.unpack(">I", data[next_at:next_at + 4])[0], next_at


def sample_chunks(data, stbl):
    """[(offset, size)] of the track's chunks in its file."""
    stsc, stsz = find(data, stbl, b"stsc"), find(data, stbl, b"stsz")
    stco, co64 = find(data, stbl, b"stco"), find(data, stbl, b"co64")
    if stsc is None or stsz is None or (stco is None and co64 is None):
        raise BoxError("track without plain sample tables")
    if co64 is not None:
        body = payload(data, co64)
        offsets = list(struct.unpack(f">{struct.unpack('>I', body[4:8])[0]}Q", body[8:]))
    else:
        body = payload(data, stco)
        offsets = list(struct.unpack(f">{struct.unpack('>I', body[4:8])[0]}I", body[8:]))
    body = payload(data, stsz)
    uniform, count = struct.unpack(">II", body[4:12])
    sizes = [uniform] * count if uniform else list(struct.unpack(f">{count}I", body[12:12 + 4 * count]))
    body = payload(data, stsc)
    entries = [struct.unpack(">III", body[8 + 12 * i:20 + 12 * i]) for i in range(struct.unpack(">I", body[4:8])[0])]
    chunks, sample = [], 0
    for index, offset in enumerate(offsets, 1):
        per_chunk = next((n for first, n, _ in reversed(entries) if first <= index), 0)
        chunks.append((offset, sum(sizes[sample:sample + per_chunk])))
        sample += per_chunk
    return chunks


def rebuild(data, box, change):
    """box rebuilt from its children; change(kind, body) returns new bytes for a leaf, the box as is for None,
    or b"" to drop it."""
    if box[0] in TRAK_CONTAINERS:
        return make_box(box[0], b"".join(rebuild(data, child, change)
                                         for child in iter_boxes(data, box[1] + box[2], box[1] + box[3])))
    new = change(box[0], payload(data, box))
    return data[box[1]:box[1] + box[3]] if new is None else new


def shift_offsets(trak_raw, delta, from_offset):
    """trak_raw with its chunk offsets at or after from_offset moved by delta."""
    box = next(iter_boxes(trak_raw))

    def change(kind, body):
        if kind not in (b"stco", b"co64"):
            return None
        count = struct.unpack(">I", body[4:8])[0]
        wide = kind == b"co64"
        offsets = struct.unpack(f">{count}{'Q' if wide else 'I'}", body[8:8 + count * (8 if wide else 4)])
        offsets = [o + delta if o >= from_offset else o for o in offsets]
        if not wide and any(o > 0xFFFFFFFF for o in offsets):
            raise BoxError("chunk offsets beyond 4 GB")
        return make_box(kind, body[:8] + struct.pack(f">{count}{'Q' if wide else 'I'}", *offsets))

    return rebuild(trak_raw, box, change)


def write_moov(dst_path, top, moov, children, prefix=b"", tail=()):
    """Replaces dst's moov by one made of children [(raw box, holds offsets into the file)]; prefix goes right
    in front of it (a media data box), tail at the end of the file. When the media data follows moov (as after
    exiftool or with faststart), the file is written anew and the chunk offsets moved by the change in size."""
    dst_path = Path(dst_path)
    mdat_after = any(b[0] == b"mdat" and b[1] > moov[1] for b in top)
    if mdat_after:
        size = len(make_box(b"moov", b"".join(raw for raw, _ in children)))
        delta = len(prefix) + size - moov[3]
        if delta:
            children = [(shift_offsets(raw, delta, moov[1] + moov[3]) if shifts else raw, shifts)
                        for raw, shifts in children]
    new_moov = make_box(b"moov", b"".join(raw for raw, _ in children))
    if not mdat_after:
        with open(dst_path, "rb") as f:
            f.seek(moov[1] + moov[3])
            after = f.read()   # only small boxes: no media data follows
        with open(dst_path, "r+b") as f:
            f.seek(moov[1])
            f.write(prefix)
            f.write(new_moov)
            f.write(after)
            for raw in tail:
                f.write(raw)
            f.truncate()
        return
    temp = dst_path.with_name(f".{dst_path.name}.rewriting")
    try:
        with open(dst_path, "rb") as source, open(temp, "wb") as out:
            out.write(source.read(moov[1]))
            out.write(prefix)
            out.write(new_moov)
            source.seek(moov[1] + moov[3])
            shutil.copyfileobj(source, out, 16 * 1024 * 1024)
            for raw in tail:
                out.write(raw)
        os.replace(temp, dst_path)
    finally:
        temp.unlink(missing_ok=True)


def transplant_track(src_data, trak, src_path, new_id, id_map, timescale_ratio, data_start, blob):
    """The trak rebuilt for the result: new id, references and durations, chunks appended to blob (a bytearray
    whose first byte will be at data_start in the result)."""
    stbl = find(src_data, trak, b"mdia", b"minf", b"stbl")
    chunks = sample_chunks(src_data, stbl)
    new_offsets = []
    with open(src_path, "rb") as f:
        for offset, size in chunks:
            f.seek(offset)
            new_offsets.append(data_start + len(blob))
            blob += f.read(size)
    wide = any(o > 0xFFFFFFFF for o in new_offsets)

    def change(kind, body):
        if kind == b"tkhd":
            body = bytearray(body)
            if body[0] == 1:
                body[20:24] = struct.pack(">I", new_id)
                body[28:36] = struct.pack(">Q", round(struct.unpack(">Q", body[28:36])[0] * timescale_ratio))
            else:
                body[12:16] = struct.pack(">I", new_id)
                body[20:24] = struct.pack(">I", min(0xFFFFFFFF, round(struct.unpack(">I", body[20:24])[0] * timescale_ratio)))
            return make_box(kind, bytes(body))
        if kind == b"elst":   # segment durations are in the movie's timescale, media times in the track's
            body = bytearray(body)
            wide_entries = body[0] == 1
            step = 20 if wide_entries else 12
            for i in range(struct.unpack(">I", body[4:8])[0]):
                at = 8 + i * step
                if wide_entries:
                    body[at:at + 8] = struct.pack(">Q", round(struct.unpack(">Q", body[at:at + 8])[0] * timescale_ratio))
                else:
                    body[at:at + 4] = struct.pack(">I", min(0xFFFFFFFF, round(struct.unpack(">I", body[at:at + 4])[0] * timescale_ratio)))
            return make_box(kind, bytes(body))
        if kind == b"tref":
            refs = b""
            for ref in iter_boxes(body):
                ids = struct.unpack(f">{(ref[3] - ref[2]) // 4}I", body[ref[1] + ref[2]:ref[1] + ref[3]])
                if not all(i in id_map for i in ids):
                    continue   # points at a track the result does not have
                refs += make_box(ref[0], struct.pack(f">{len(ids)}I", *(id_map[i] for i in ids)))
            return make_box(kind, refs) if refs else b""
        if kind in (b"stco", b"co64"):
            if wide:
                return make_box(b"co64", struct.pack(">II", 0, len(new_offsets)) + struct.pack(f">{len(new_offsets)}Q", *new_offsets))
            return make_box(b"stco", struct.pack(">II", 0, len(new_offsets)) + struct.pack(f">{len(new_offsets)}I", *new_offsets))
        return None

    return rebuild(src_data, trak, change)


def transplant_tracks(src_path, dst_path):
    """Carries src's tracks that are neither picture nor sound over to dst (an MP4 just written by ffmpeg) where dst
    has fewer of that kind. Returns the sample entry types carried over, e.g. ["mebx", "mebx"]."""
    src_top = top_level_boxes(src_path)
    src_moov = next((b for b in src_top if b[0] == b"moov"), None)
    if src_moov is None:
        return []
    src_data = read_box(src_path, src_moov)
    src_moov_box = (src_moov[0], 0, src_moov[2], src_moov[3])
    src_traks = [b for b in iter_boxes(src_data, src_moov[2]) if b[0] == b"trak"]
    side = [(t, track_info(src_data, t)) for t in src_traks if track_info(src_data, t)[1] not in PICTURE_AND_SOUND]
    side = [(t, info) for t, info in side if info[1] is not None]
    if not side:
        return []

    dst_path = Path(dst_path)
    top = top_level_boxes(dst_path)
    moov = next((b for b in top if b[0] == b"moov"), None)
    if moov is None:
        raise BoxError("no moov box")
    data = read_box(dst_path, moov)
    moov_box = (moov[0], 0, moov[2], moov[3])
    dst_traks = [b for b in iter_boxes(data, moov[2]) if b[0] == b"trak"]
    dst_infos = [track_info(data, t) for t in dst_traks]

    # the result's tracks that stand for the original's picture and sound, matched in order per kind
    id_map = {}
    for handler in (b"vide", b"soun"):
        src_ids = [info[0] for info in map(lambda t: track_info(src_data, t), src_traks) if info[1] == handler]
        dst_ids = [info[0] for info in dst_infos if info[1] == handler]
        id_map.update(zip(src_ids, dst_ids))
    have = [info[2] for info in dst_infos]
    chosen = []
    for trak, info in side:
        if have.count(info[2]) > 0:
            have.remove(info[2])   # the result has one of this kind already (written by ffmpeg)
            continue
        chosen.append((trak, info))
    if not chosen:
        return []

    timescale, next_id, next_at = movie_header(data, moov_box)
    src_timescale, _, _ = movie_header(src_data, src_moov_box)
    for trak, info in chosen:
        id_map[info[0]] = next_id
        next_id += 1

    # The new media data box goes where moov starts now; moov follows it. Its size is known before the tracks are
    # built (the chunk sizes), so the offsets can be set in one go.
    total = sum(size for trak, _ in chosen
                for _, size in sample_chunks(src_data, find(src_data, trak, b"mdia", b"minf", b"stbl")))
    mdat_header = 16 if total + 16 > 0xFFFFFFFF else 8
    blob = bytearray()
    new_traks = [transplant_track(src_data, trak, src_path, id_map[info[0]], id_map, timescale / src_timescale,
                                  moov[1] + mdat_header, blob)
                 for trak, info in chosen]
    mdat = (struct.pack(">I4sQ", 1, b"mdat", len(blob) + 16) if mdat_header == 16
            else struct.pack(">I4s", len(blob) + 8, b"mdat")) + bytes(blob)

    data = bytearray(data)
    data[next_at:next_at + 4] = struct.pack(">I", next_id)
    # the new tracks' offsets already point into the new media data box, which stays where it is put
    children = [(bytes(data[b[1]:b[1] + b[3]]), b[0] == b"trak") for b in iter_boxes(bytes(data), moov[2])]
    last_trak = max(i for i, b in enumerate(iter_boxes(bytes(data), moov[2])) if b[0] == b"trak")
    children[last_trak + 1:last_trak + 1] = [(raw, False) for raw in new_traks]
    write_moov(dst_path, top, moov, children, prefix=mdat)
    return [info[2].decode("latin-1") for _, info in chosen]


TRACK_METADATA = (b"meta", b"udta")   # a track's own keys (Apple: lens, focal length, aperture, maker notes) and user data


def copy_track_metadata(src_path, dst_path):
    """Copies the metadata boxes of src's picture and sound tracks into the matching tracks of dst (matched in order
    per kind), replacing a box of that kind dst has: that one can only be a partial copy exiftool made of the same
    original. They hold no offsets. Returns e.g. ["vide meta"]."""
    src_top = top_level_boxes(src_path)
    src_moov = next((b for b in src_top if b[0] == b"moov"), None)
    if src_moov is None:
        return []
    src_data = read_box(src_path, src_moov)
    src_traks = [t for t in iter_boxes(src_data, src_moov[2]) if t[0] == b"trak"]

    dst_path = Path(dst_path)
    top = top_level_boxes(dst_path)
    moov = next((b for b in top if b[0] == b"moov"), None)
    if moov is None:
        raise BoxError("no moov box")
    data = read_box(dst_path, moov)
    dst_traks = [t for t in iter_boxes(data, moov[2]) if t[0] == b"trak"]

    additions = {}   # dst trak start -> boxes to append
    copied = []
    for handler in (b"vide", b"soun"):
        pairs = zip([t for t in src_traks if track_info(src_data, t)[1] == handler],
                    [t for t in dst_traks if track_info(data, t)[1] == handler])
        for src_trak, dst_trak in pairs:
            for kind in TRACK_METADATA:
                box = find(src_data, src_trak, kind)
                if box is None:
                    continue
                raw = src_data[box[1]:box[1] + box[3]]
                present = find(data, dst_trak, kind)
                if present is not None and data[present[1]:present[1] + present[3]] == raw:
                    continue
                additions.setdefault(dst_trak[1], []).append((kind, raw))
                copied.append(f"{handler.decode()} {kind.decode()}")
    if not copied:
        return []

    children = []
    for box in iter_boxes(data, moov[2]):
        raw = data[box[1]:box[1] + box[3]]
        if box[1] in additions:
            replaced = {kind for kind, _ in additions[box[1]]}
            kept = b"".join(data[c[1]:c[1] + c[3]] for c in iter_boxes(data, box[1] + box[2], box[1] + box[3])
                            if c[0] not in replaced)
            raw = make_box(b"trak", kept + b"".join(new for _, new in additions[box[1]]))
        children.append((raw, box[0] == b"trak"))
    write_moov(dst_path, top, moov, children)
    return copied
