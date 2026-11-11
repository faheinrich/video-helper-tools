import os
import subprocess
import shutil
import time
import signal
import struct
import sys
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal
from PySide6.QtGui import QImage

from video_helper_core.i18n import tr
from video_helper_core.trash import move_to_trash
from .boxes import BoxError, copy_maker_boxes, copy_track_metadata, transplant_tracks
from .utils import (
    DEFAULT_POSTFIX, EXIFTOOL_CONFIG, block_sample_starts, blockiness_command, is_blocky, parse_blockiness, DurationCache, partial_path, path_key, same_file, copy_file_times, format_size, generate_thumbnail, get_thumbnail_path, get_video_info, is_photos_compatible,
    LEGACY_TAG_COPIES, capture_date_arguments, copy_sidecars, is_quicktime, metadata_as_text, missing_metadata, source_description, get_audio_streams, get_hdr_color, parse_ffmpeg_time, read_compression_settings, settings_tag_argument,
)

class UnifiedScanWorker(QThread):
    file_found = Signal(dict)
    scan_finished = Signal(list)
    
    def __init__(self, src_dir, dst_dir, flatten=False, sort_by="name_asc", postfix=DEFAULT_POSTFIX, excludes=(),
                 files=None, skip_hidden=True):
        """files: a hand-picked list of videos instead of everything below src_dir. Their results go to dst_dir, or
        next to each original when dst_dir is empty."""
        super().__init__()
        self.src_dir = Path(src_dir)
        self.dst_dir = Path(dst_dir) if dst_dir else None
        self.files = None if files is None else [Path(f) for f in files]
        self.flatten = flatten
        self.postfix = postfix
        # Results of earlier runs lying in the source folder are never compressed again.
        self.excludes = tuple(e.lower() for e in excludes) + ((postfix.lower(),) if postfix else ())
        self.skip_hidden = skip_hidden
        self.sort_by = sort_by
        self.video_types = [".mov", ".mp4", ".avi", ".mts", ".ogv", ".m4v", ".mkv"]

    def find_videos(self):
        """Videos below src_dir. Hidden folders (".hermes", ".Trash", …) are not even entered when skipped."""
        found = []
        for folder, subfolders, names in os.walk(self.src_dir):
            if self.skip_hidden:
                subfolders[:] = [d for d in subfolders if not d.startswith(".")]
            for name in names:
                # "._clip.mov" are macOS metadata files, never videos.
                if name.startswith("._") or (self.skip_hidden and name.startswith(".")):
                    continue
                path = Path(folder) / name
                if path.suffix.lower() in self.video_types and path.is_file():
                    found.append(path)
        return found

    def run(self):
        try:
            # 1. Gather all source files
            if self.files is not None:
                orig_files = [p for p in self.files if p.is_file()]
            else:
                orig_files = self.find_videos()

            candidates = []
            for src_path in orig_files:
                stem = src_path.stem
                if stem.lower().endswith(self.excludes):
                    continue
                candidates.append(src_path)

            def target_for(src_path, name):
                if self.files is not None:
                    return (self.dst_dir or src_path.parent) / name
                rel_path = src_path.relative_to(self.src_dir)
                return self.dst_dir / name if self.flatten else self.dst_dir / rel_path.with_name(name)

            # Two sources with the same target name (test.mov and test.mp4 both give test_archived.mp4) keep their
            # extension in the name: test.mov_archived.mp4 and test.mp4_archived.mp4.
            groups = {}
            for src_path in candidates:
                groups.setdefault(path_key(target_for(src_path, f"{src_path.stem}{self.postfix}.mp4")), []).append(src_path)
            colliding = {src for group in groups.values() if len(group) > 1 for src in group}

            source_keys = {path_key(p) for p in candidates}
            results = []
            for src_path in candidates:
                name = f"{src_path.name}{self.postfix}.mp4" if src_path in colliding else f"{src_path.stem}{self.postfix}.mp4"
                predicted_dst = target_for(src_path, name)
                # The target is one of the videos being processed (typically an empty postfix next to an .mp4):
                # it would replace the original. Shown in red and only done after an explicit confirmation.
                conflict = path_key(predicted_dst) in source_keys or (predicted_dst.exists() and same_file(predicted_dst, src_path))
                comp_path = predicted_dst if predicted_dst.exists() and not conflict else None

                item = {
                    'stem': src_path.stem,
                    'path': src_path,
                    'dst_path': predicted_dst,
                    'exists_compressed': comp_path is not None,
                    'conflict': conflict,
                    'size': src_path.stat().st_size,
                    'comp_size': comp_path.stat().st_size if comp_path else 0,
                    'duration': None
                }
                results.append(item)
            
            # 3. Sort
            if self.sort_by == "size_desc":
                results.sort(key=lambda x: x['size'], reverse=True)
            elif self.sort_by == "size_asc":
                results.sort(key=lambda x: x['size'])
            elif self.sort_by == "duration_desc":
                durations = {}
                for f in results:
                    dur, _, _ = get_video_info(f['path'])
                    durations[f['path']] = dur or 0.0
                results.sort(key=lambda x: durations[x['path']], reverse=True)
            elif self.sort_by == "duration_asc":
                durations = {}
                for f in results:
                    dur, _, _ = get_video_info(f['path'])
                    durations[f['path']] = dur or 0.0
                results.sort(key=lambda x: durations[x['path']])
            elif self.sort_by == "name_desc":
                results.sort(key=lambda x: x['stem'].lower(), reverse=True)
            else: # name_asc
                results.sort(key=lambda x: x['stem'].lower())
            
            for item in results:
                self.file_found.emit(item)
                
        except Exception as e:
            print(f"Scan error: {e}")
        
        self.scan_finished.emit(results)


def suspend_process(process):
    """Freezes a running ffmpeg (and its children) without losing its progress."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.ntdll.NtSuspendProcess(int(process._handle))
    else:
        os.killpg(os.getpgid(process.pid), signal.SIGSTOP)


def resume_process(process):
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.ntdll.NtResumeProcess(int(process._handle))
    else:
        os.killpg(os.getpgid(process.pid), signal.SIGCONT)


LOW_PRIORITY_NICE = 10
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # Windows


class ArchiveWorker(QThread):
    progress_step = Signal(int, str)

    # Pfad-basierte Signale für eindeutige Identifizierung bei doppelten Dateinamen
    status_update_path = Signal(str, str, str, dict)
    file_duration_discovered_path = Signal(str, float)
    file_progress_path = Signal(str, int)
    ffmpeg_log_line_path = Signal(str, str)
    
    finished_all = Signal()
    
    def __init__(self, src_dir, dst_dir, max_jobs, video_data_list, settings):
        super().__init__()
        self.src_dir = Path(src_dir)
        self.dst_dir = Path(dst_dir)
        self.max_jobs = max_jobs
        self.is_running = True
        self.settings = settings
        self.active_processes = []
        # The GUI may reorder the remaining queue while running (set_queue), hence the lock.
        self.queue_lock = threading.Lock()
        self.queue = list(video_data_list)
        self.started = set()
        self.hdr_sources = set()
        self.replacing = set()   # sources whose result replaces them (target = source, confirmed by the user)
        self.paused = False

    def settings_record(self, src_path=None, source=None, date_from=None):
        """What gets stored in the result file (see utils.SETTINGS_TAG). source: what the result was made from
        (see utils.source_description); date_from: where an added capture date came from."""
        import datetime as dt
        from .. import __version__
        s = self.settings
        record = {
            "version": 2,
            "app_version": __version__,
            "compressed_at": dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "encoder": s.get('renderer'),
            "max_res": s.get('max_res') if s.get('limit_res') else None,
            "max_fps": s.get('max_fps') if s.get('limit_fps') else None,
            "copy_aac": bool(s.get('copy_aac')),
            "dry_run": bool(s.get('dry_run')),
        }
        if s.get('renderer') == "libx265":
            record.update(crf=s.get('crf'), preset=s.get('preset'))
        else:
            record.update(vt_quality=s.get('vt_quality'))
        if src_path is not None and str(src_path) in self.hdr_sources:
            record["hdr"] = True
        if source:
            record["source"] = source
        if date_from:
            record["capture_date_from"] = date_from
        return record

    def pause(self):
        """Freezes the running encodes and starts no new ones until resume()."""
        self.paused = True
        for process, *_ in list(self.active_processes):
            try:
                suspend_process(process)
            except OSError:
                pass

    def resume(self):
        for process, *_ in list(self.active_processes):
            try:
                resume_process(process)
            except OSError:
                pass
        self.paused = False

    def set_queue(self, items):
        """Replace the videos still waiting; ones already started are never queued again."""
        with self.queue_lock:
            self.queue = [item for item in items if str(item['path']) not in self.started]

    def next_item(self):
        with self.queue_lock:
            if not self.queue:
                return None
            item = self.queue.pop(0)
            self.started.add(str(item['path']))
            return item

    def has_queue(self):
        with self.queue_lock:
            return bool(self.queue)

    def run(self):
        count = 0

        while (self.has_queue() or self.active_processes) and self.is_running:
            while len(self.active_processes) < self.max_jobs and self.is_running and not self.paused:
                file_info = self.next_item()
                if file_info is None:
                    break
                src_path = file_info['path']
                dst_path = file_info['dst_path']
                
                src_dur, src_fps, src_audio = get_video_info(src_path)
                if src_dur:
                    self.file_duration_discovered_path.emit(str(src_path), src_dur)

                p_ffmpeg, skip_reason, log_msg, skip_data = self.start_video_process(
                    src_path, dst_path, src_dur, src_fps, src_audio, replace_source=bool(file_info.get('conflict')))

                if skip_reason:
                    count += 1
                    failed = bool(skip_data.get('failed'))
                    self.ffmpeg_log_line_path.emit(str(src_path), log_msg)
                    self.status_update_path.emit(str(src_path), "error" if failed else "skipped", skip_reason, skip_data)
                    self.progress_step.emit(count, log_msg)
                elif p_ffmpeg:
                    if file_info.get('conflict'):
                        self.replacing.add(str(src_path))
                    self.status_update_path.emit(str(src_path), "running", "", {})
                    self.active_processes.append((p_ffmpeg, src_path, dst_path, src_dur, b""))
            
            still_active = []
            for p_ffmpeg, src_path, dst_path, total_dur, unread_buffer in self.active_processes:
                if not self.is_running:
                    break
                
                try:
                    raw_data = p_ffmpeg.stderr.read(1024)
                    if raw_data:
                        unread_buffer += raw_data
                        lines = re.split(b'[\r\n]', unread_buffer)
                        unread_buffer = lines.pop()
                        
                        for line in lines:
                            decoded_line = line.decode('utf-8', errors='replace').strip()
                            if decoded_line:
                                self.ffmpeg_log_line_path.emit(str(src_path), decoded_line)

                                # Extract speed
                                speed_match = re.search(r'speed=\s*([\d\.]+)x', decoded_line)
                                speed_str = speed_match.group(1) + "x" if speed_match else ""

                                if speed_str:
                                    # Update status with speed info
                                    self.status_update_path.emit(str(src_path), "running", speed_str, {})

                                if total_dur and total_dur > 0:
                                    current_time = parse_ffmpeg_time(decoded_line)
                                    if current_time is not None:
                                        pct = min(int((current_time / total_dur) * 100), 99)
                                        self.file_progress_path.emit(str(src_path), pct)
                except Exception:
                    pass
                
                poll = p_ffmpeg.poll()
                if poll is not None:
                    count += 1
                    self.ffmpeg_log_line_path.emit(str(src_path), tr("\n[INFO] Copying metadata & EXIF tags..."))
                    success_msg, data_dict = self.finalize_video_process(
                        p_ffmpeg, src_path, dst_path, replace_source=str(src_path) in self.replacing)
                    status_str = "finished" if "✅" in success_msg else "error"
                    reason_str = "" if status_str == "finished" else tr("FFmpeg error")

                    self.file_progress_path.emit(str(src_path), 100)
                    self.status_update_path.emit(str(src_path), status_str, reason_str, data_dict)
                    self.ffmpeg_log_line_path.emit(str(src_path), success_msg)
                    self.progress_step.emit(count, success_msg)
                else:
                    still_active.append((p_ffmpeg, src_path, dst_path, total_dur, unread_buffer))
            
            self.active_processes = still_active
            time.sleep(0.1)
        
        if not self.is_running:
            self.kill_all_processes()
        self.finished_all.emit()
    
    def build_ffmpeg_command(self, src_path, out_path, src_fps, segment=None):
        """The ffmpeg call for one video with this worker's settings. segment=(start, length) in seconds encodes just
        that part (used to try settings out). Also remembers HDR sources for the settings record."""
        filters = []
        if self.settings['limit_res']:
            max_res = self.settings['max_res']
            filters.append(
                f"scale='if(gt(iw,ih),min({max_res},iw),-2)':'if(gt(iw,ih),-2,min({max_res},ih))':force_original_aspect_ratio=decrease,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
            )
        if self.settings['limit_fps'] and src_fps and src_fps > self.settings['max_fps']:
            filters.append(f"fps=fps={self.settings['max_fps']}")

        # HDR stays HDR: 10-bit HEVC (Main 10) with the source's colour tags. SDR stays 8-bit.
        hdr = get_hdr_color(src_path)
        libx265 = self.settings['renderer'] == "libx265"
        if hdr:
            self.hdr_sources.add(str(src_path))
            filters.append("format=yuv420p10le" if libx265 else "format=p010le")
        else:
            filters.append("format=yuv420p")
        vf_chain = ",".join(filters)

        # Keep EVERY audio stream: without -map ffmpeg takes only one, which loses the other tracks of recordings with
        # separate tracks (e.g. game sound and voice). "V" skips cover art, "a?" does not fail without audio.
        audio_streams = get_audio_streams(src_path)
        ffmpeg_cmd = ["ffmpeg", "-y"]
        if segment:
            ffmpeg_cmd += ["-ss", str(segment[0])]   # before -i: jumps to the start instead of decoding up to it
        ffmpeg_cmd += ["-i", str(src_path), "-vf", vf_chain, "-map", "0:V:0", "-map", "0:a?"]
        # re-muxing drops the stream names of the recorder; write them back as handler name and title
        for idx, (_codec, stream_name) in enumerate(audio_streams):
            if stream_name:
                ffmpeg_cmd += [f"-metadata:s:a:{idx}", f"handler_name={stream_name}",
                               f"-metadata:s:a:{idx}", f"title={stream_name}"]
        if hdr:
            ffmpeg_cmd += ["-color_primaries", hdr["color_primaries"], "-color_trc", hdr["color_trc"],
                           "-colorspace", hdr["colorspace"], "-profile:v", "main10"]

        if segment:
            ffmpeg_cmd += ["-t", str(segment[1])]
        elif self.settings.get('dry_run', False):
            ffmpeg_cmd += ["-t", "1"]

        if self.settings['copy_aac'] and audio_streams and all(c == "aac" for c, _ in audio_streams):
            ffmpeg_cmd += ["-c:a", "copy"]
        else:
            ffmpeg_cmd += ["-c:a", "aac", "-b:a", "128k"]

        if libx265:
            ffmpeg_cmd += [
                "-c:v", "libx265",
                "-crf", str(self.settings['crf']),
                "-preset", self.settings['preset'],
                "-tag:v", "hvc1"
            ]
        else:
            ffmpeg_cmd += [
                "-c:v", "hevc_videotoolbox",
                "-q:v", str(self.settings['vt_quality']),
                "-tag:v", "hvc1"
            ]
        ffmpeg_cmd.append(str(out_path))
        return ffmpeg_cmd

    def start_video_process(self, src_path, dst_path, src_dur, src_fps, src_audio, replace_source=False):
        try:
            # replace_source: the target is the source itself (confirmed by the user), so there is no earlier result to keep.
            if dst_path.exists() and not self.settings['overwrite'] and not replace_source:
                dst_dur, _, _ = get_video_info(dst_path)
                if src_dur and dst_dur:
                    src_size = src_path.stat().st_size
                    dst_size = dst_path.stat().st_size
                    
                    skip_data = {
                        'src_size': src_size, 'dst_size': dst_size,
                        'diff_size': src_size - dst_size,
                        'ratio': (dst_size / src_size) * 100 if src_size > 0 else 100.0
                    }
                    if abs(src_dur - dst_dur) < 0.8:
                        return None, tr("Already exists"), f"⏭️ SKIP: {src_path.name}", skip_data
                    else:
                        return None, tr("Duration differs"), f"⚠️ SKIP: {src_path.name}", {}
            
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            partial = partial_path(dst_path)
            partial.unlink(missing_ok=True)   # left over from a crashed run
            
            # The result is written next to its final name and only renamed when complete: an aborted or failed run
            # never destroys an existing file (and a replaced original stays intact until the new file is ready).
            ffmpeg_cmd = self.build_ffmpeg_command(src_path, partial, src_fps)
            
            env = os.environ.copy()
            env["FFREPORT"] = f"file={os.devnull}:level=32"
            
            low_priority = self.settings.get('low_priority', True)

            def child_setup():
                os.setsid()
                if low_priority:
                    os.nice(LOW_PRIORITY_NICE)  # keeps the computer usable while it encodes

            p_ffmpeg = subprocess.Popen(
                ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env,
                preexec_fn=child_setup if sys.platform != "win32" else None,
                creationflags=BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" and low_priority else 0,
            )
            
            if sys.platform != "win32":
                import fcntl
                flags = fcntl.fcntl(p_ffmpeg.stderr, fcntl.F_GETFL)
                fcntl.fcntl(p_ffmpeg.stderr, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            return p_ffmpeg, None, None, {}
        
        except Exception as e:
            return (None, tr("Start error: {error}", error=e),
                    tr("❌ Could not start {name}: {error}", name=src_path.name, error=e), {'failed': True})
    
    def finalize_video_process(self, p_ffmpeg, src_path, dst_path, replace_source=False):
        data_dict = {}
        partial = partial_path(dst_path)
        if p_ffmpeg.returncode != 0:
            partial.unlink(missing_ok=True)
            return tr("❌ Error in {name}: FFmpeg exit code {code}", name=src_path.name, code=p_ffmpeg.returncode), data_dict

        # Metadata copy is best-effort: a failure here (e.g. exotic tags, missing
        # exiftool) must not throw away an otherwise successfully compressed video.
        record = None
        try:
            date_arguments, date_from = capture_date_arguments(src_path)
            record = self.settings_record(src_path, source_description(src_path), date_from)
        except Exception as e:
            print(f"Settings record of {src_path.name} not made: {e}")
            date_arguments = []
        # A test run is a second long: tracks of the whole video would not fit it.
        metadata_warning = carry_metadata(src_path, partial, record, date_arguments,
                                          tracks=not self.settings.get('dry_run'))
        if metadata_warning:
            record = None
        try:
            copy_file_times(src_path, partial)
        except OSError as e:
            metadata_warning += tr(" ⚠️ (metadata could not be copied: {error})", error=e)

        try:
            src_size = src_path.stat().st_size
            dst_size = partial.stat().st_size
        except OSError as e:
            partial.unlink(missing_ok=True)
            return tr("❌ Post-processing failed for {name}: {error}", name=src_path.name, error=e), data_dict

        if replace_source and dst_size >= src_size:
            # Replacing the original by something bigger would only do harm.
            partial.unlink(missing_ok=True)
            data_dict = {'src_size': src_size, 'dst_size': src_size, 'diff_size': 0, 'ratio': 100.0, 'settings': None}
            return tr("✅ KEPT ORIGINAL: {name} | the compressed file was not smaller", name=src_path.name), data_dict
        try:
            if dst_path.exists():
                # the original (when the result replaces it) or an earlier result: to the trash, not gone for good
                move_to_trash(dst_path)
            os.replace(partial, dst_path)   # atomic: the final name never holds a half-written file
        except OSError as e:
            partial.unlink(missing_ok=True)
            return tr("❌ Post-processing failed for {name}: {error}", name=src_path.name, error=e), data_dict

        try:
            if copy_sidecars(src_path, dst_path) is not None:
                copy_file_times(src_path, dst_path)   # writing an XMP sidecar into the result touched it
        except OSError as e:
            print(f"Sidecars of {src_path.name} not copied: {e}")
        diff_size = src_size - dst_size
        ratio = (dst_size / src_size) * 100 if src_size > 0 else 100.0

        data_dict = {'src_size': src_size, 'dst_size': dst_size, 'diff_size': diff_size, 'ratio': ratio,
                     'settings': record}
        result_msg = f"✅ FINISH: {src_path.name} | {format_size(src_size)} -> {format_size(dst_size)} ({ratio:.1f}%){metadata_warning}"

        if dst_size >= src_size:
            # Only keep the smaller original if it imports into Apple Photos; otherwise
            # the (always hvc1/AAC) compressed file stays the archive copy.
            if dst_path.parent.resolve() == src_path.parent.resolve():
                result_msg += tr(" ⚠️ (the original next to it is smaller)")   # a copy would only take up space
            elif is_photos_compatible(src_path):
                backup_path = dst_path.with_name(f"{src_path.stem}_source{src_path.suffix}")
                shutil.copy2(src_path, backup_path)
                result_msg += tr(" ⚠️ (original copied because it is smaller)")
            else:
                result_msg += tr(" ⚠️ (original not Apple Photos compatible, kept the compressed version)")

        return result_msg, data_dict
    
    def stop(self):
        self.is_running = False
        if self.paused:
            self.resume()

    def kill_all_processes(self):
        for p_ffmpeg, src_path, dst_path, _, _ in self.active_processes:
            if p_ffmpeg.poll() is None:
                try:
                    if sys.platform != "win32":
                        os.killpg(os.getpgid(p_ffmpeg.pid), signal.SIGKILL)
                    else:
                        p_ffmpeg.kill()
                except:
                    pass
            partial_path(dst_path).unlink(missing_ok=True)   # only the unfinished file; an existing result stays



def carry_metadata(src_path, target, record, date_arguments=(), tracks=True, extra_arguments=()):
    """Gives target (an MP4 made from src_path) all metadata of src_path and stores record (the settings tag) in it.
    Returns a warning text when exiftool failed, else "". Afterwards record["metadata_missing"] lists what did not
    come over (also stored in the file)."""
    # First: exiftool keeps boxes and tracks it cannot write itself, so they must be in place before it runs.
    # The comparison at the end reports whatever does not come over.
    if tracks:
        try:
            transplant_tracks(src_path, target)
        except (OSError, BoxError, struct.error, ValueError) as e:
            print(f"Tracks of {Path(src_path).name} not carried over: {e}")
    try:
        copy_track_metadata(src_path, target)
    except (OSError, BoxError, struct.error) as e:
        print(f"Track metadata of {Path(src_path).name} not copied: {e}")
    try:
        copy_maker_boxes(src_path, target)
    except (OSError, BoxError, struct.error) as e:
        print(f"Maker boxes of {Path(src_path).name} not copied: {e}")
    if record is None:
        record = {}
    record.pop("metadata_missing", None)
    try:
        legacy = not is_quicktime(src_path)
        if legacy:
            record["source_metadata"] = metadata_as_text(src_path)
        # -config must come first; the settings tag and an added date go after the copy so it does not
        # overwrite them. The file type fields of ffmpeg's container are not metadata of the video: left out.
        subprocess.run(
            ["exiftool", "-config", str(EXIFTOOL_CONFIG),
             # -XMP: the packet as a whole first, so structures exiftool cannot write tag by tag (Adobe's
             # editing history) come along too
             "-tagsFromFile", str(src_path), "-XMP", "-all:all", "-gps*", "-Keys:all", "-UserData:all",
             "--MajorBrand", "--MinorVersion", "--CompatibleBrands",
             *(LEGACY_TAG_COPIES if legacy else []), *date_arguments, *extra_arguments, settings_tag_argument(record),
             str(target), "-overwrite_original", "-q"], check=True)
    except Exception as e:
        return tr(" ⚠️ (metadata could not be copied: {error})", error=e)
    try:
        missing = missing_metadata(src_path, target, record.get("source_metadata", {}))
        if missing:
            # kept in the result, so the warning is still there after a restart
            record["metadata_missing"] = missing
            subprocess.run(["exiftool", "-config", str(EXIFTOOL_CONFIG), settings_tag_argument(record),
                            str(target), "-overwrite_original", "-q"], check=True)
    except Exception as e:
        print(f"Metadata of {Path(src_path).name} not compared: {e}")
    return ""


def add_missing_metadata(src_path, result_path):
    """Gives a result made by an earlier version the metadata that version did not carry over (camera and lens
    boxes, tracks, the video track's keys), without compressing again. Works on a hidden copy next to the result
    that replaces it in one step at the end; the result's file times stay. Returns what is still missing."""
    import datetime as dt
    src_path, result_path = Path(src_path), Path(result_path)
    work = result_path.with_name(f".{result_path.stem}.adding-metadata{result_path.suffix}")
    shutil.copy2(result_path, work)
    try:
        record = dict(read_compression_settings([result_path]).get(str(result_path)) or {})
        record.setdefault("source", source_description(src_path))
        record["metadata_added_at"] = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
        date_arguments, date_from = capture_date_arguments(src_path)
        if date_from:
            record["capture_date_from"] = date_from
        src_duration, result_duration = get_video_info(src_path)[0], get_video_info(result_path)[0]
        whole = bool(src_duration and result_duration and abs(src_duration - result_duration) < 1.5)
        warning = carry_metadata(src_path, work, record, date_arguments, tracks=whole,
                                 # left there by earlier versions: ffmpeg's container type, not the camera's
                                 extra_arguments=["-Keys:MajorBrand=", "-Keys:MinorVersion=", "-Keys:CompatibleBrands="])
        if warning:
            raise OSError(warning.strip(" ⚠️()"))
        copy_file_times(result_path, work)
        os.replace(work, result_path)
        stat = result_path.stat()
        copy_sidecars(src_path, result_path)
        os.utime(result_path, (stat.st_atime, stat.st_mtime))
        return record.get("metadata_missing", [])
    finally:
        work.unlink(missing_ok=True)


class CompareScanWorker(QThread):
    pair_found = Signal(dict)
    scan_finished = Signal(list)
    
    def __init__(self, orig_dir, comp_dir, sort_by="name_asc"):
        super().__init__()
        self.orig_dir = Path(orig_dir)
        self.comp_dir = Path(comp_dir)
        self.sort_by = sort_by
        self.video_types = [".mov", ".mp4", ".avi", ".mts", ".ogv", ".m4v", ".mkv"]
        
    def run(self):
        pairs = []
        try:
            # Gather all original files
            orig_files = []
            if self.orig_dir.exists() and self.orig_dir.is_dir():
                orig_files = [p for p in self.orig_dir.rglob("*") if
                             p.is_file() and not p.name.startswith(".") and p.suffix.lower() in self.video_types]
            
            # Gather all compressed files
            comp_files = []
            if self.comp_dir.exists() and self.comp_dir.is_dir():
                comp_files = [p for p in self.comp_dir.rglob("*") if
                             p.is_file() and not p.name.startswith(".") and p.suffix.lower() in self.video_types]
                
            results = []
            # We iterate over all original files and look for their counterparts
            for src_path in orig_files:
                rel_path = src_path.relative_to(self.orig_dir)
                stem = src_path.stem
                
                stem_for_comp = stem[:-9] if stem.lower().endswith("_archived") else stem
                
                # Check for compressed version in the same relative subfolder
                predicted_comp = self.comp_dir / rel_path.with_name(f"{stem_for_comp}_archived.mp4")
                
                comp_path = None
                if predicted_comp.exists():
                    comp_path = predicted_comp
                else:
                    # Fallback: check if it exists with original name in comp_dir (if it was just copied)
                    if (self.comp_dir / rel_path).exists():
                        comp_path = self.comp_dir / rel_path
                
                pair = {
                    'stem': stem,
                    'orig_path': src_path,
                    'comp_path': comp_path,
                    'orig_size': src_path.stat().st_size,
                    'comp_size': comp_path.stat().st_size if comp_path else 0,
                    'orig_dur': 0.0
                }
                
                if self.sort_by in ["duration_asc", "duration_desc"]:
                    dur, _, _ = get_video_info(src_path)
                    pair['orig_dur'] = dur or 0.0
                
                results.append(pair)
            
            # Also find compressed files that don't have an original (orphans)
            # This was implicitly handled by the old set(all_stems) logic
            orig_rel_paths = {p.relative_to(self.orig_dir) for p in orig_files}
            for c_path in comp_files:
                c_rel = c_path.relative_to(self.comp_dir)
                # Try to map back to original rel path
                c_stem = c_path.stem
                if c_stem.endswith("_archived"):
                    c_stem = c_stem[:-9]
                
                # Possible original relative paths
                possible_rels = [
                    c_rel.with_name(f"{c_stem}{ext}") for ext in self.video_types
                ]
                
                found_orig = False
                for pr in possible_rels:
                    if pr in orig_rel_paths:
                        found_orig = True
                        break
                
                if not found_orig:
                    # Orphaned compressed file
                    results.append({
                        'stem': c_path.stem,
                        'orig_path': None,
                        'comp_path': c_path,
                        'orig_size': 0,
                        'comp_size': c_path.stat().st_size,
                        'orig_dur': 0.0
                    })

            pairs = results
            
            if self.sort_by == "size_desc":
                pairs.sort(key=lambda x: x['orig_size'], reverse=True)
            elif self.sort_by == "size_asc":
                pairs.sort(key=lambda x: x['orig_size'])
            elif self.sort_by == "duration_desc":
                pairs.sort(key=lambda x: x['orig_dur'], reverse=True)
            elif self.sort_by == "duration_asc":
                pairs.sort(key=lambda x: x['orig_dur'])
            elif self.sort_by == "name_desc":
                pairs.sort(key=lambda x: x['stem'].lower(), reverse=True)
            else: # name_asc
                pairs.sort(key=lambda x: x['stem'].lower())

            for pair in pairs:
                self.pair_found.emit(pair)
                
        except Exception as e:
            print(f"Compare scan error: {e}")
            
        self.scan_finished.emit(pairs)

class ThumbnailSignals(QObject):
    # QImage, not QPixmap: pixmaps may only be created on the GUI thread.
    finished = Signal(str, object)


class ThumbnailRunnable(QRunnable):
    """Connect `signals.finished` to a method of a long-lived QObject, never to a lambda:
    a lambda is invoked via this short-lived signals object, which the pool may already
    have freed when the queued call is delivered (crash in Shiboken::cppPointer)."""

    def __init__(self, video_path, cancelled=None, allowed=None):
        super().__init__()
        self.video_path = video_path
        self.cancelled = cancelled or threading.Event()
        self.allowed = allowed  # cleared while new thumbnails must wait (a video plays in the viewer)
        self.signals = ThumbnailSignals()

    def wait_until_allowed(self):
        while self.allowed is not None and not self.allowed.wait(0.1):
            if self.cancelled.is_set():
                return False
        return not self.cancelled.is_set()

    def run(self):
        if self.cancelled.is_set():
            return
        image = None
        try:
            thumb_path = get_thumbnail_path(self.video_path)
            if not thumb_path.exists():
                if not self.wait_until_allowed():
                    return
                generate_thumbnail(self.video_path, thumb_path, self.cancelled)
            else:
                os.utime(thumb_path)  # used recently: keeps it out of prune_thumbnails
            if self.cancelled.is_set():
                return
            if thumb_path.exists():
                image = QImage(str(thumb_path))
        except Exception as e:
            print(f"Error generating thumbnail for {self.video_path}: {e}")
        try:
            self.signals.finished.emit(str(self.video_path), image)
        except RuntimeError:  # the window was closed while this job was still running
            pass


class ThumbnailLoader(QObject):
    """Makes the list's thumbnails in a pool of its own, a few at a time. A new folder or closing
    the window drops the waiting ones and stops running ffmpeg calls at once (the global pool
    made the app wait for every thumbnail of the folder before it quit), and while a video plays
    in the viewer no new ones start, so playback gets the disk and the CPU."""
    PARALLEL = 3
    ready = Signal(str, object)  # video path, QImage or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(self.PARALLEL)
        self.cancelled = threading.Event()
        self.allowed = threading.Event()
        self.allowed.set()

    def load(self, paths):
        self.cancel()
        self.cancelled = threading.Event()
        for path in paths:
            runnable = ThumbnailRunnable(path, self.cancelled, self.allowed)
            runnable.signals.finished.connect(self.on_finished)
            self.pool.start(runnable)

    def on_finished(self, path, image):
        if not self.cancelled.is_set():
            self.ready.emit(path, image)

    def cancel(self):
        self.cancelled.set()
        self.pool.clear()

    def pause(self):
        self.allowed.clear()

    def resume(self):
        self.allowed.set()

    def is_busy(self):
        return self.pool.activeThreadCount() > 0

    def shutdown(self, timeout_ms=5000):
        self.cancel()
        return self.pool.waitForDone(timeout_ms)


class DurationProbeWorker(QThread):
    """Fills in video durations after a scan so the table and ETA have them before archiving."""
    duration_found = Signal(str, float)

    def __init__(self, paths):
        super().__init__()
        self.paths = list(paths)
        self._stopped = False

    def stop(self):
        self._stopped = True

    PARALLEL_PROBES = 6  # ffprobe mostly waits for the disk; a few at once hide that latency

    def run(self):
        cache = DurationCache()
        missing = []
        for path in self.paths:
            if self._stopped:
                return
            duration = cache.get(path)
            if duration:
                self.duration_found.emit(str(path), duration)
            else:
                missing.append(path)

        def probe(path):
            return path, (None if self._stopped else get_video_info(path)[0])

        with ThreadPoolExecutor(self.PARALLEL_PROBES) as pool:
            futures = [pool.submit(probe, path) for path in missing]
            try:
                for count, future in enumerate(as_completed(futures), 1):
                    if self._stopped:
                        break
                    path, duration = future.result()
                    if duration:
                        cache.put(path, duration)
                        self.duration_found.emit(str(path), duration)
                    if count % 25 == 0:
                        cache.save()
            finally:
                for future in futures:
                    future.cancel()
        cache.save()


class BlockCheckWorker(QThread):
    """Measures finished results one after the other, at low priority, while the run goes on."""
    checked = Signal(str, object)  # source path, (result score, original score)

    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.waiting = []
        self.process = None
        self.stopping = False
        self.finished.connect(self.start_if_waiting)  # added just as the thread was ending

    def start_if_waiting(self):
        if self.waiting and not self.stopping:
            self.start()

    def add(self, src, dst, same_file=False):
        """same_file: the result replaced the original, so there is nothing to compare it with."""
        with self.condition:
            self.waiting.append((src, dst, same_file))
        if not self.isRunning():
            self.start()

    def stop(self):
        with self.condition:
            self.stopping = True
            if self.process is not None:
                self.process.kill()

    def measure(self, path, start):
        low_priority = {"creationflags": BELOW_NORMAL_PRIORITY_CLASS} if sys.platform == "win32" else \
            {"preexec_fn": lambda: os.nice(LOW_PRIORITY_NICE)}
        with self.condition:
            if self.stopping:
                return None
            self.process = subprocess.Popen(blockiness_command(path, start), stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, text=True, errors="replace", **low_priority)
        _, output = self.process.communicate()
        return parse_blockiness(output)

    def check(self, src, dst, same_file):
        """The part where the result looks worst compared with the original."""
        worst = None
        for start in block_sample_starts(get_video_info(dst)[0]):
            result = self.measure(dst, start)
            if result is None:
                continue
            original = None if same_file else self.measure(src, start)
            if worst is None or is_blocky(result, original) and not is_blocky(*worst) or result > worst[0]:
                worst = (result, original)
        return worst

    def run(self):
        while True:
            with self.condition:
                # Ends when there is nothing to do: an idle thread would abort the app if its owner goes away.
                if self.stopping or not self.waiting:
                    return
                src, dst, same_file = self.waiting.pop(0)
            try:
                result = self.check(src, dst, same_file)
            except OSError:  # the check is a bonus; it never gets in the way of the run
                result = None
            if result is not None and not self.stopping:
                self.checked.emit(str(src), result)


class ResultSettingsWorker(QThread):
    """Reads the stored compression settings of existing results after a scan."""
    settings_found = Signal(str, object)  # source path, settings dict

    def __init__(self, pairs):
        super().__init__()
        self.pairs = list(pairs)  # (source path, result path)

    def run(self):
        found = read_compression_settings([dst for _, dst in self.pairs])
        for src, dst in self.pairs:
            if str(dst) in found:
                self.settings_found.emit(str(src), found[str(dst)])
