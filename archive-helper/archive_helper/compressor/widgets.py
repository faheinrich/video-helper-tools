from pathlib import Path

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QDialog, QSlider,
    QButtonGroup, QGraphicsItem, QGraphicsRectItem, QGraphicsView, QGraphicsScene, QMessageBox, QSizePolicy, QStyle, QStyleOptionFrame, QWidget
)
import json
from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QUrl, QSizeF, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

from video_helper_core.i18n import tr
from video_helper_core.media import MediaPlayer
from video_helper_core.paths import settings_file
from .utils import describe_streams, get_display_rotation, get_resolution_and_fps

# The compressed file decodes faster than e.g. a 4K original, so the players drift apart;
# the original is the master clock and the compressed player is pulled back beyond this.
SYNC_TOLERANCE_MS = 120
FLIP_SECONDS = 2   # flip mode: how long each of the two is shown
ORIGINAL_COLOR = QColor(47, 128, 237)   # blue: frame and label of the original in flip mode
RESULT_COLOR = QColor(229, 72, 77)      # red: the compressed version
READY_STATUSES = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                  QMediaPlayer.MediaStatus.BufferingMedia)


def format_clock(ms):
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class DropLineEdit(QLineEdit):
    """Path field that accepts a dropped folder. While not being edited, a path too long for the field is shown with
    the middle shortened, so both its start and its last folder stay visible; the tooltip has the whole path."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        self.textChanged.connect(self.setToolTip)

    def elided_text(self):
        """The text to show instead of the real one, or None when it fits or the field is being edited."""
        if self.hasFocus() or not self.text():
            return None
        rect = self.contentsRect().adjusted(4, 0, -4, 0)
        if self.fontMetrics().horizontalAdvance(self.text()) <= rect.width():
            return None
        return self.fontMetrics().elidedText(self.text(), Qt.ElideMiddle, rect.width())

    def paintEvent(self, event):
        shown = self.elided_text()
        if shown is None:
            return super().paintEvent(event)
        painter = QPainter(self)
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        self.style().drawPrimitive(QStyle.PE_PanelLineEdit, option, painter, self)
        painter.setPen(self.palette().color(QPalette.Text))
        painter.drawText(self.contentsRect().adjusted(4, 0, -4, 0), int(Qt.AlignVCenter | Qt.AlignLeft), shown)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            if url.isLocalFile():
                self.setText(str(Path(url.toLocalFile())))   # with the system's separators (backslashes on Windows), as if typed
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


def dropped_paths(mime):
    """Local file and folder paths of a drag, in the order they were dragged."""
    return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]


class DropOverlay(QWidget):
    """Covers the page while files are dragged over it, in two areas that never overlap: Source (the list and
    everything left of the settings) and Target (the settings column on the right). dropped(zone, paths) reports a drop."""

    dropped = Signal(str, list)

    def __init__(self, parent, target_widget):
        super().__init__(parent)
        self.target_widget = target_widget   # the settings drawer: its column is the Target area
        self.setAcceptDrops(True)
        self.hover = None
        self.dragged = []
        self.pointer = QPoint(-1, -1)
        self.hide()

    def zones(self, pos=None):
        """Name -> rectangle of the two areas. The Target area is the settings column; without it the right 30 %."""
        area = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        if self.target_widget.isVisible():
            split = self.target_widget.mapTo(self.parentWidget(), self.target_widget.rect().topLeft()).x()
        else:
            split = area.left() + area.width() * 0.7
        return {"source": QRectF(area.left(), area.top(), split - area.left() - 6, area.height()),
                "target": QRectF(split + 6, area.top(), area.right() - split - 6, area.height())}

    def describe(self, paths):
        """What is being dragged: remembered so the areas can say what a drop would do."""
        self.dragged = list(paths)

    def source_hint(self):
        paths = self.dragged
        if not paths:
            return tr("Drop a folder or video files")
        if len(paths) == 1 and paths[0].is_dir():
            return tr("Use this folder as the source")
        return tr("Pick {count} dropped items as the videos to compress", count=len(paths))

    def target_hint(self):
        paths = self.dragged
        if not paths:
            return tr("Drop the folder for the results")
        if len(paths) == 1 and paths[0].is_dir():
            return tr("Use this folder for the results")
        return tr("Use the folder of the first item for the results")

    def zone_at(self, pos):
        return next((name for name, rect in self.zones(pos).items() if rect.contains(QPointF(pos))), None)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        titles = {"source": (tr("Source"), self.source_hint()), "target": (tr("Target"), self.target_hint())}
        for name, rect in self.zones(self.pointer).items():
            hot = name == self.hover
            fill = QColor(58, 99, 216, 235 if hot else 170)
            painter.setPen(QPen(QColor(255, 255, 255, 230), 3, Qt.DashLine))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 18, 18)
            painter.setPen(QColor("white"))
            title, hint = titles[name]
            font = QFont(self.font())
            font.setPointSizeF(font.pointSizeF() * 2.2)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect.adjusted(0, -20, 0, 0), int(Qt.AlignCenter), title)
            painter.setFont(self.font())
            painter.drawText(rect.adjusted(0, 50, 0, 0), int(Qt.AlignHCenter | Qt.AlignVCenter), hint)

    def track(self, pos):
        self.pointer = pos
        self.hover = self.zone_at(pos)
        self.update()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.describe(dropped_paths(event.mimeData()))
            event.acceptProposedAction()
            self.track(event.position().toPoint())
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()
        self.track(event.position().toPoint())

    def dragLeaveEvent(self, event):
        self.hover = None
        self.hide()

    def dropEvent(self, event):
        zone = self.zone_at(event.position().toPoint())
        paths = dropped_paths(event.mimeData())
        self.hover = None
        self.hide()
        event.acceptProposedAction()
        if zone and paths:
            self.dropped.emit(zone, paths)


class ZoomableVideoView(QGraphicsView):
    zoom_changed = Signal(float)

    def __init__(self, player, rotation=0, parent=None):
        super().__init__(parent)
        self.rotation = rotation
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        
        self.video_item = QGraphicsVideoItem()
        # The item gets exactly the displayed aspect (see videoSizeChanged). Letting Qt fit the
        # frame itself uses the unrotated size for rotated clips: squashed picture and
        # never-repainted bands outside that area.
        self.video_item.setAspectRatioMode(Qt.IgnoreAspectRatio)
        self.scene.addItem(self.video_item)
        player.setVideoOutput(self.video_item)
        
        self.video_item.nativeSizeChanged.connect(self.videoSizeChanged)
        
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.zoom_factor = 1.0
        self.zoom_step = 1.15
        # slider view: a second video drawn over this one, cut off left of a dividing line (see show_overlay)
        self.overlay_item = self.clip_item = None
        self.split_x = 0
        self.border_color = None   # flip mode: a coloured frame around the picture says which video is shown

    def show_overlay(self, item):
        """Draws `item` (the other video) on top of this one, visible only right of split_x; None ends it.
        One view showing both keeps them exactly aligned when zooming, panning and rotating, and lets them repaint
        together (two stacked video widgets: macOS did not repaint the covered one, so it froze)."""
        if self.clip_item is not None:
            self.scene.removeItem(self.clip_item)
            self.clip_item = None
        self.overlay_item = item
        if item is not None:
            self.clip_item = QGraphicsRectItem()
            self.clip_item.setPen(Qt.NoPen)
            self.clip_item.setFlag(QGraphicsItem.ItemClipsChildrenToShape)
            self.scene.addItem(self.clip_item)
            item.setAspectRatioMode(Qt.IgnoreAspectRatio)
            item.setParentItem(self.clip_item)
            self.sync_overlay()

    def set_split_x(self, x):
        self.split_x = x
        self.update_clip()

    def sync_overlay(self):
        """The overlay takes this video's place and size: the result may have fewer pixels, the picture is the same."""
        if self.overlay_item is None:
            return
        item = self.overlay_item
        item.setSize(self.video_item.size())
        item.setTransformOriginPoint(self.video_item.transformOriginPoint())
        item.setRotation(self.video_item.rotation())
        item.setPos(self.video_item.pos())
        self.update_clip()

    def update_clip(self):
        if self.clip_item is None:
            return
        left = self.mapToScene(QPoint(int(self.split_x), 0)).x()
        area = self.frame_rect().adjusted(-100000, -100000, 100000, 100000)
        self.clip_item.setRect(QRectF(left, area.top(), max(0.0, area.right() - left), area.height()))

    def set_border(self, color):
        self.border_color = color
        self.viewport().update()

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self.border_color is None or self.video_item.size().isEmpty():
            return
        # around the picture, in screen pixels (not zoomed); at the edge of the view when zoomed in further
        picture = self.mapFromScene(self.frame_rect()).boundingRect()
        width = 6
        box = QRectF(picture).intersected(QRectF(self.viewport().rect()).adjusted(width / 2, width / 2, -width / 2, -width / 2))
        painter.save()
        painter.resetTransform()
        painter.setPen(QPen(self.border_color, width))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(box)
        painter.restore()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.update_clip()   # the line stays where it is on screen

    def videoSizeChanged(self, size):
        # nativeSize is the stored (unrotated) size, but Qt draws the frame rotated by the
        # file's display matrix; with a landscape box a portrait clip would be drawn tiny.
        if self.rotation % 180 == 90:
            size = QSizeF(size.height(), size.width())
        self.video_item.setSize(QSizeF(size))
        self.setSceneRect(self.frame_rect())
        self.fitInView(self.frame_rect(), Qt.KeepAspectRatio)
        self.zoom_factor = 1.0
        self.sync_overlay()

    def frame_rect(self):
        # Not boundingRect(): for rotated clips Qt computes it from the unrotated native size, while the frame is
        # drawn into the item's full size. mapRectToScene applies the item's own rotation (the Rotate button), so a
        # turned landscape clip is fitted as the portrait picture it now is.
        return self.video_item.mapRectToScene(QRectF(QPointF(0, 0), self.video_item.size()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if abs(self.zoom_factor - 1.0) < 0.001 and not self.video_item.size().isEmpty():
            self.fitInView(self.frame_rect(), Qt.KeepAspectRatio)
        self.update_clip()

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            self.scale(self.zoom_step, self.zoom_step)
            self.zoom_factor *= self.zoom_step
        else:
            self.scale(1.0 / self.zoom_step, 1.0 / self.zoom_step)
            self.zoom_factor /= self.zoom_step
        self.update_clip()
        self.zoom_changed.emit(self.zoom_factor)

    def set_zoom(self, factor):
        if abs(self.zoom_factor - factor) > 0.001:
            rel_scale = factor / self.zoom_factor
            self.scale(rel_scale, rel_scale)
            self.zoom_factor = factor
            self.update_clip()

    def rotate_by(self, degrees=90):
        """Turns the picture and fits the turned picture into the view again."""
        rect = QRectF(QPointF(0, 0), self.video_item.size())
        self.video_item.setTransformOriginPoint(rect.center())
        self.video_item.setRotation((self.video_item.rotation() + degrees) % 360)
        self.setSceneRect(self.frame_rect())
        self.sync_overlay()
        self.reset_view()

    def reset_view(self):
        self.setSceneRect(self.frame_rect())
        self.fitInView(self.frame_rect(), Qt.KeepAspectRatio)
        self.zoom_factor = 1.0
        self.update_clip()
        self.zoom_changed.emit(1.0)


class SplitHandle(QWidget):
    """The dividing line of the slider view; drag it left or right."""
    moved = Signal(int)   # x in the parent's coordinates
    WIDTH = 24

    def __init__(self, parent):
        super().__init__(parent)
        self.setCursor(Qt.SplitHCursor)
        self.setToolTip(tr("Drag to move the dividing line"))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        middle = self.width() / 2
        painter.setPen(QPen(QColor(0, 0, 0, 110), 4))
        painter.drawLine(QPointF(middle, 0), QPointF(middle, self.height()))
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawLine(QPointF(middle, 0), QPointF(middle, self.height()))
        grip = QRectF(middle - 10, self.height() / 2 - 10, 20, 20)
        painter.setPen(QPen(QColor(0, 0, 0, 110), 1))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(grip)
        painter.setPen(QPen(QColor(60, 60, 60), 1.5))
        y = grip.center().y()
        for direction in (-1, 1):   # ‹ ›
            tip = grip.center().x() + direction * 6
            painter.drawLine(QPointF(tip, y), QPointF(tip - direction * 3, y - 3))
            painter.drawLine(QPointF(tip, y), QPointF(tip - direction * 3, y + 3))

    def mousePressEvent(self, event):
        self.moved.emit(int(self.mapToParent(event.position().toPoint()).x()))

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self.moved.emit(int(self.mapToParent(event.position().toPoint()).x()))


class CompareStage(QWidget):
    """The two views side by side, or (slider view) on top of each other: left of the dividing line the original,
    right of it the compressed version, so the same spot of both can be compared without moving the eyes."""
    GAP = 8

    def __init__(self, view_orig, view_comp=None, parent=None):
        super().__init__(parent)
        self.view_orig, self.view_comp = view_orig, view_comp
        self.mode, self.split = "side", 0.5
        self.showing_comp = False   # "flip": which of the two is shown right now
        for view in (view_orig, view_comp):
            if view is not None:
                view.setParent(self)
        self.handle = self.tags = None
        if view_comp is not None:
            self.tags = [self.tag(tr("Original")), self.tag(tr("Compressed"))]
            self.handle = SplitHandle(self)
            self.handle.moved.connect(lambda x: self.set_split(x / max(1, self.width())))
        self.relayout()

    TAG_STYLE = "background: {background}; color: white; padding: 3px 8px; border-radius: 4px;"

    def tag(self, text):
        label = QLabel(text, self)
        label.setStyleSheet(self.TAG_STYLE.format(background="rgba(0, 0, 0, 0.55)"))
        label.setAttribute(Qt.WA_TransparentForMouseEvents)
        label.adjustSize()
        label.hide()
        return label

    MODES = ("side", "slider", "flip")

    @property
    def overlay(self):
        """Slider and flip both show the two videos in one view."""
        return self.mode != "side"

    def set_mode(self, mode):
        self.mode = mode if mode in self.MODES and self.view_comp is not None else "side"
        self.showing_comp = False
        self.relayout()

    def flip(self):
        """Flip mode: from the original to the compressed version and back."""
        self.showing_comp = not self.showing_comp
        self.relayout()

    def set_split(self, fraction):
        self.split = min(1.0, max(0.0, fraction))
        self.relayout()

    def split_x(self):
        return round(self.width() * self.split)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout()

    def relayout(self):
        rect = self.rect()
        if self.view_comp is None:
            self.view_orig.setGeometry(rect)
            return
        if self.overlay:
            # one view shows both videos (see ZoomableVideoView.show_overlay); the other one waits hidden
            self.view_comp.hide()
            self.view_orig.setGeometry(rect)
            left, right = self.tags
            if self.mode == "flip":
                # the dividing line far off one side: all original, or all compressed
                self.view_orig.set_split_x(-10 ** 6 if self.showing_comp else 10 ** 6)
                self.handle.hide()
                color = RESULT_COLOR if self.showing_comp else ORIGINAL_COLOR
                self.view_orig.set_border(color)
                shown, hidden = (right, left) if self.showing_comp else (left, right)
                shown.setStyleSheet(self.TAG_STYLE.format(background=color.name()))
                shown.move(10, 10)   # in the same corner, so the eye does not have to look for it
                shown.show()
                shown.raise_()
                hidden.hide()
                return
            self.view_orig.set_border(None)
            for tag in self.tags:
                tag.setStyleSheet(self.TAG_STYLE.format(background="rgba(0, 0, 0, 0.55)"))
            x = self.split_x()
            self.view_orig.set_split_x(x - self.view_orig.viewport().x())
            self.handle.setGeometry(x - SplitHandle.WIDTH // 2, 0, SplitHandle.WIDTH, rect.height())
            self.handle.show()
            self.handle.raise_()
            left.move(10, 10)
            right.move(rect.width() - right.width() - 10, 10)
            for tag in self.tags:
                tag.show()
                tag.raise_()
        else:
            half = max(0, (rect.width() - self.GAP) // 2)
            self.view_orig.setGeometry(0, 0, half, rect.height())
            self.view_comp.setGeometry(rect.width() - half, 0, half, rect.height())
            self.view_comp.show()
            self.view_orig.set_border(None)
            self.handle.hide()
            for tag in self.tags:
                tag.hide()


def saved_compare_mode():
    try:
        path = settings_file()
        return json.loads(path.read_text(encoding="utf-8")).get("compare_view", {}).get("mode") if path.exists() else None
    except (OSError, ValueError, AttributeError):
        return None


def save_compare_mode(mode):
    try:
        path = settings_file()
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        settings["compare_view"] = {"mode": mode}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=4), encoding="utf-8")
    except (OSError, ValueError) as e:
        print(f"Failed to save the compare view: {e}")


class CompareVideoDialog(QDialog):
    """Plays the original next to its compressed version; without comp_path it plays just the original."""

    def __init__(self, orig_path, comp_path=None, parent=None):
        super().__init__(parent)
        self.has_comp = comp_path is not None
        self.paths = {"orig": orig_path, "comp": comp_path}
        self.reported_errors = set()
        self.player_comp = self.view_comp = self.overlay_item = None
        self.setWindowTitle(tr("Compare: {name}", name=orig_path.name) if self.has_comp else tr("Original: {name}", name=orig_path.name))
        
        self.setWindowState(Qt.WindowMaximized)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        header_layout = QHBoxLayout()
        
        # Fetch Metadata
        w_o, h_o, fps_o = get_resolution_and_fps(orig_path)
        w_c, h_c, fps_c = get_resolution_and_fps(comp_path) if self.has_comp else (None, None, None)
        
        orig_meta = f"{w_o}x{h_o} @ {fps_o} FPS" if w_o and h_o else tr("Unknown")
        comp_meta = f"{w_c}x{h_c} @ {fps_c} FPS" if w_c and h_c else tr("Unknown")
        
        # Original Player
        self.player_orig = MediaPlayer(self)
        self.audio_orig = QAudioOutput(self)
        self.player_orig.setAudioOutput(self.audio_orig)
        self.view_orig = ZoomableVideoView(self.player_orig, get_display_rotation(orig_path))
        self.view_orig.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.player_orig.errorOccurred.connect(self.handle_player_error)

        orig_lbl = QLabel(tr("<b>Original (scroll to zoom, drag to pan)</b><br>{meta}", meta=orig_meta))
        orig_lbl.setAlignment(Qt.AlignCenter)
        self.btn_rotate_orig = self.rotate_button(self.view_orig)
        header_layout.addLayout(self.header_row(orig_lbl, self.btn_rotate_orig), stretch=1)
        
        if self.has_comp:
            # Compressed Player
            self.player_comp = MediaPlayer(self)
            self.audio_comp = QAudioOutput(self)
            self.player_comp.setAudioOutput(self.audio_comp)
            # One soundtrack: two slightly offset ones sound like an echo.
            self.audio_comp.setMuted(True)
            self.view_comp = ZoomableVideoView(self.player_comp, get_display_rotation(comp_path))
            self.view_comp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

            self.player_comp.errorOccurred.connect(self.handle_player_error)
            # No automatic rotation here: Qt 6 already applies the file's display matrix
            # (e.g. portrait iPhone clips); rotating again would turn them sideways.

            comp_lbl = QLabel(tr("<b>Compressed (scroll to zoom, drag to pan)</b><br>{meta}", meta=comp_meta))
            comp_lbl.setAlignment(Qt.AlignCenter)
            self.btn_rotate_comp = self.rotate_button(self.view_comp)
            header_layout.addLayout(self.header_row(comp_lbl, self.btn_rotate_comp), stretch=1)

        layout.addLayout(header_layout)
        self.stage = CompareStage(self.view_orig, self.view_comp)
        layout.addWidget(self.stage, stretch=1)
        
        # Timeline: scrubbing seeks both players to the same position.
        position_row = QHBoxLayout()
        self.slider_pos = QSlider(Qt.Horizontal)
        self.slider_pos.setRange(0, 0)
        self.slider_pos.setPageStep(5000)
        self.slider_pos.valueChanged.connect(self.seek)  # programmatic updates block signals
        self.slider_pos.sliderPressed.connect(self.scrub_start)
        self.slider_pos.sliderReleased.connect(self.scrub_end)
        self.lbl_time = QLabel("0:00 / 0:00")
        position_row.addWidget(self.slider_pos, stretch=1)
        position_row.addWidget(self.lbl_time)
        layout.addLayout(position_row)

        btn_layout = QHBoxLayout()
        self.btn_restart = QPushButton(tr("⏮ From the start"))
        self.btn_restart.clicked.connect(self.restart)
        self.btn_play = QPushButton(tr("▶️ Play / ⏸️ Pause"))
        self.btn_play.setToolTip(tr("Space bar"))
        self.btn_play.clicked.connect(self.toggle_play)
        
        # Zoom Controls
        self.btn_zoom_out = QPushButton("➖")
        self.btn_zoom_out.setFixedWidth(40)
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        
        self.slider_zoom = QSlider(Qt.Horizontal)
        self.slider_zoom.setRange(100, 500)
        self.slider_zoom.setValue(100)
        self.slider_zoom.setFixedWidth(200)
        self.slider_zoom.valueChanged.connect(self.slider_zoom_changed)
        
        self.btn_zoom_in = QPushButton("➕")
        self.btn_zoom_in.setFixedWidth(40)
        self.btn_zoom_in.clicked.connect(self.zoom_in)
        
        self.btn_reset = QPushButton(tr("🔄 Reset view"))
        self.btn_reset.clicked.connect(self.reset_views)
        
        self.btn_close = QPushButton(tr("Close"))
        self.btn_close.clicked.connect(self.close)

        # how the two are shown (kept for next time): side by side, one picture with a dividing line to drag, or one
        # picture that flips between them every FLIP_SECONDS
        self.mode_buttons = {}
        self.mode_group = QButtonGroup(self)
        for mode, text, tip in (
            ("side", tr("◫ Side by side"), tr("The two videos next to each other.")),
            ("slider", tr("◧ Slider"), tr("One picture with a dividing line you can drag: left of it the original, "
                                         "right of it the compressed version.")),
            ("flip", tr("⇄ Flip"), tr("One picture that changes every {seconds} seconds between the original and the "
                                     "compressed version.", seconds=FLIP_SECONDS)),
        ):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setVisible(self.has_comp)
            button.clicked.connect(lambda _=False, m=mode: self.choose_mode(m))
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
        self.flip_timer = QTimer(self, interval=FLIP_SECONDS * 1000)
        self.flip_timer.timeout.connect(self.on_flip)

        btn_layout.addWidget(self.btn_restart)
        btn_layout.addWidget(self.btn_play)
        btn_layout.addStretch()
        btn_layout.addWidget(QLabel("Zoom:"))
        btn_layout.addWidget(self.btn_zoom_out)
        btn_layout.addWidget(self.slider_zoom)
        btn_layout.addWidget(self.btn_zoom_in)
        btn_layout.addStretch()
        for button in self.mode_buttons.values():
            btn_layout.addWidget(button)
        btn_layout.addWidget(self.btn_reset)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)
        
        self.player_orig.setSource(QUrl.fromLocalFile(str(orig_path.resolve())))
        self.view_orig.zoom_changed.connect(self.on_view_zoom_changed)
        if self.has_comp:
            self.player_comp.setSource(QUrl.fromLocalFile(str(comp_path.resolve())))
            self.view_orig.horizontalScrollBar().valueChanged.connect(self.view_comp.horizontalScrollBar().setValue)
            self.view_comp.horizontalScrollBar().valueChanged.connect(self.view_orig.horizontalScrollBar().setValue)
            self.view_orig.verticalScrollBar().valueChanged.connect(self.view_comp.verticalScrollBar().setValue)
            self.view_comp.verticalScrollBar().valueChanged.connect(self.view_orig.verticalScrollBar().setValue)
            self.view_comp.zoom_changed.connect(self.on_view_zoom_changed)

        # Enter must not trigger whichever button happens to be the dialog default (it rotated the video).
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)
        self.set_view_mode(saved_compare_mode() or "side")

        self.scrubbing = False
        self.resume_after_scrub = False
        self.started = False
        self.sync_timer = QTimer(self, interval=200)
        self.sync_timer.timeout.connect(self.keep_in_sync)
        self.player_orig.durationChanged.connect(self.on_duration)
        # Start only once both files are loaded, otherwise the smaller one gets a head start.
        for player in self.players():
            player.mediaStatusChanged.connect(self.start_when_ready)
        self.start_when_ready()

        # Ensure keyboard shortcuts like ESC always lead to proper cleanup.
        # Otherwise QDialog might only hide/reject without closing immediately.
        self.setModal(True)
        
    def set_view_mode(self, mode):
        mode = mode if self.has_comp and mode in CompareStage.MODES else "side"
        overlay = mode != "side"
        if overlay and self.overlay_item is None:
            self.overlay_item = QGraphicsVideoItem()
            self.player_comp.setVideoOutput(self.overlay_item)
            self.view_orig.show_overlay(self.overlay_item)
        elif not overlay and self.overlay_item is not None:
            self.player_comp.setVideoOutput(self.view_comp.video_item)
            self.view_orig.show_overlay(None)
            self.overlay_item = None
        if self.has_comp and not self.is_playing():
            self.player_comp.setPosition(self.player_orig.position())   # paused: the new output gets the frame too
        self.stage.set_mode(mode)
        self.mode_buttons[mode].setChecked(True)
        # flipping goes on while paused: comparing one still frame is often the clearest
        if mode == "flip":
            self.flip_timer.start()
        else:
            self.flip_timer.stop()

    def choose_mode(self, mode):
        self.set_view_mode(mode)
        save_compare_mode(self.stage.mode)

    def on_flip(self):
        self.stage.flip()

    def players(self):
        return [self.player_orig, self.player_comp] if self.has_comp else [self.player_orig]

    def views(self):
        return [self.view_orig, self.view_comp] if self.has_comp else [self.view_orig]

    def rotate_button(self, view):
        button = QPushButton("⟳")
        button.setToolTip(tr("Rotate by 90°"))
        button.setFixedSize(34, 30)
        button.clicked.connect(lambda: self.rotate_video(view))
        return button

    @staticmethod
    def header_row(label, button):
        """The title in the middle, the small rotate button at the right edge."""
        row = QHBoxLayout()
        spacer = QWidget()
        spacer.setFixedWidth(button.width() if button.width() > 0 else 34)
        row.addWidget(spacer)
        row.addWidget(label, 1)
        row.addWidget(button, 0, Qt.AlignTop)
        return row

    def rotate_video(self, view):
        view.rotate_by(90)
        if view is self.view_orig or view is self.view_comp:
            self.slider_zoom.setValue(100)
    
    def slider_zoom_changed(self, value):
        factor = value / 100.0
        for view in self.views():
            view.set_zoom(factor)

    def on_view_zoom_changed(self, factor):
        # Every view follows, also below the slider's 100 % (zooming out with the wheel): in the slider view the two
        # pictures lie on top of each other and must stay exactly aligned.
        for view in self.views():
            view.set_zoom(factor)
        val = min(500, max(100, int(factor * 100)))
        if self.slider_zoom.value() != val:
            self.slider_zoom.blockSignals(True)
            self.slider_zoom.setValue(val)
            self.slider_zoom.blockSignals(False)

    def zoom_in(self):
        val = min(self.slider_zoom.value() + 20, 500)
        self.slider_zoom.setValue(val)
        
    def zoom_out(self):
        val = max(self.slider_zoom.value() - 20, 100)
        self.slider_zoom.setValue(val)
        
    def reset_views(self):
        self.slider_zoom.setValue(100)
        for view in self.views():
            view.reset_view()

    def is_playing(self):
        return self.player_orig.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def start_when_ready(self, *_):
        if not self.started and all(p.mediaStatus() in READY_STATUSES for p in self.players()):
            self.started = True
            self.play()

    def play(self):
        if self.has_comp:
            self.player_comp.setPosition(self.player_orig.position())
        for player in self.players():
            player.play()
        self.sync_timer.start()

    def pause(self):
        for player in self.players():
            player.pause()
        if self.has_comp:
            self.player_comp.setPosition(self.player_orig.position())  # show the same frame
        self.update_timeline()

    def toggle_play(self):
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def restart(self):
        self.seek(0)
        self.play()

    def seek(self, ms):
        for player in self.players():
            player.setPosition(ms)
        self.update_timeline(ms)

    def scrub_start(self):
        self.scrubbing = True
        self.resume_after_scrub = self.is_playing()
        if self.resume_after_scrub:
            for player in self.players():
                player.pause()

    def scrub_end(self):
        self.scrubbing = False
        self.seek(self.slider_pos.value())
        if self.resume_after_scrub:
            self.play()

    def keep_in_sync(self):
        master = self.player_orig.position()
        if self.has_comp and self.is_playing() and abs(self.player_comp.position() - master) > SYNC_TOLERANCE_MS:
            self.player_comp.setPosition(master)
        if not self.scrubbing:
            self.update_timeline(master)

    def update_timeline(self, position=None):
        position = self.player_orig.position() if position is None else position
        if not self.scrubbing:
            self.slider_pos.blockSignals(True)
            self.slider_pos.setValue(position)
            self.slider_pos.blockSignals(False)
        self.lbl_time.setText(f"{format_clock(position)} / {format_clock(self.player_orig.duration())}")

    def on_duration(self, duration):
        self.slider_pos.setRange(0, duration)
        self.update_timeline()

    def handle_player_error(self, error, error_string=""):
        if error == QMediaPlayer.Error.NoError:
            return
        side = "orig" if self.sender() is self.player_orig else "comp"
        if side in self.reported_errors:  # one message per side, not one per failed attempt
            return
        self.reported_errors.add(side)
        path = self.paths[side]
        QMessageBox.critical(self, tr("Video error"), tr(
            "The {side} could not be played: {error}\n\nFile: {name}\nFormat: {format}",
            side=tr("original") if side == "orig" else tr("compressed version"),
            error=error_string or tr("unknown error"), name=path.name,
            format=describe_streams(path) or tr("could not be read")))

    def cleanup_players(self):
        try:
            self.flip_timer.stop()
        except (AttributeError, RuntimeError):
            pass
        try:
            self.sync_timer.stop()
        except (AttributeError, RuntimeError):  # not created yet, or already freed (__del__)
            pass
        for player in (getattr(self, "player_orig", None), getattr(self, "player_comp", None)):
            if player is None:
                continue
            try:
                player.stop()
                player.setSource(QUrl())
                player.setVideoOutput(None)
            except (RuntimeError, AttributeError):
                pass

    def closeEvent(self, event):
        self.cleanup_players()
        super().closeEvent(event)

    def reject(self):
        self.cleanup_players()
        super().reject()

    def accept(self):
        self.cleanup_players()
        super().accept()

    def __del__(self):
        self.cleanup_players()
