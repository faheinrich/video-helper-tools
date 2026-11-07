# Single source of the version shown in the About dialog; a test keeps it equal to pyproject.toml.
__version__ = "0.0.4"

import os

# Decode video in software in Qt's players. With VideoToolbox, Qt reports "Unknown error
# occurred" at the end of some camera H.264 files (e.g. Panasonic, yuvj420p) that decode
# fine in software; 4K60 10-bit HEVC still plays at full frame rate this way. Must be set
# before Qt's multimedia backend starts, hence here. The compressor's ffmpeg runs are unaffected.
os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", ",")

from video_helper_core import paths

paths.configure("Archive Helper", "archive-helper")

from . import translations  # noqa: E402,F401  (registers the German texts)
