"""Look shared by all tools (cards, captions, the accent run button, progress bar).

Colours work on light and dark windows alike. Not palette(alternate-base) and #858b95: in macOS dark
mode Qt reports a bright grey and a nearly invisible white for them, so cards glared and captions disappeared."""
from PySide6.QtWidgets import QLabel

STYLE = """
QFrame#summary, QFrame#detailBar { background: rgba(128, 128, 128, 0.14); border: 1px solid rgba(128, 128, 128, 0.35); border-radius: 8px; }
QFrame#drawer { border-left: 1px solid palette(mid); }
QLineEdit { placeholder-text-color: #858b95; }
QLineEdit:disabled { color: #858b95; }
QLabel[role="caption"] { color: #858b95; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }
QLabel[role="value"] { font-size: 15px; font-weight: 600; }
QLabel[role="value"][tone="good"] { color: #2e9e6c; }
QLabel[role="value"][tone="warn"] { color: #c9821f; }
QLabel[role="hint"] { color: #858b95; font-size: 12px; }
QPushButton#runButton { background: palette(highlight); color: palette(highlighted-text); border: none; border-radius: 6px; padding: 6px 18px; font-weight: 600; }
QPushButton#runButton[mode="stop"] { background: #c63f35; color: white; }
QPushButton#runButton:disabled { background: rgba(128, 128, 128, 0.22); color: #858b95; }
QPushButton[role="chip"] { border: 1px solid palette(mid); border-radius: 12px; padding: 3px 12px; background: transparent; }
QPushButton[role="chip"]:checked { background: palette(text); color: palette(base); border-color: palette(text); }
QPushButton[role="segment"] { border: 1px solid palette(mid); padding: 5px 8px; background: palette(base); }
QPushButton[role="segment"]:checked { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); font-weight: 600; }
QProgressBar { border: none; background: palette(midlight); border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: palette(highlight); border-radius: 3px; }
"""


def caption(text):
    label = QLabel(text.upper())
    label.setProperty("role", "caption")
    return label
