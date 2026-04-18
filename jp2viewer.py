r"""
JP2 Viewer — Python / PyQt6 / glymur (v3)
==========================================

Adds on top of v2:
  - Toolbar with buttons for every action
  - Top-left overlay: filename, zoom %, preview level
  - Top-right minimap: thumbnail + yellow rectangle showing current view
  - Status bar: image dimensions, CRS, cursor pixel coords, world coords
  - GeoJP2 coordinate readout under the cursor (requires `tifffile`)

Extra install for geo readout:
    pip install tifffile

Shortcuts (also available as toolbar buttons):
    Ctrl+O              open file dialog
    Left / Right        prev / next in folder
    F / F11 / Esc       fullscreen
    R / Shift+R         rotate CW / CCW
    H / V               flip horizontal / vertical
    Ctrl++  /  Ctrl+-   zoom in / out
    Ctrl+0              fit to window
    Ctrl+1              100 % (reloads full resolution if on preview)
    Mouse drag          pan
    Ctrl + wheel        zoom at cursor
    Drag file onto      open
"""
import io
import math
import os
import sys
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QEvent, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QAction, QColor, QImage, QKeySequence, QPainter, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsView, QLabel, QMainWindow, QStatusBar, QStyle, QToolBar,
    QWidget,
)

JP2_EXTS = {".jp2", ".j2k", ".jpx", ".jpf", ".jpc"}

# UUID of the box in a GeoJP2 file that holds embedded GeoTIFF tags.
GEOJP2_UUID = "b14bf8bd-083d-4b43-a5ae-8cd7d5a6ce03"

# Preview decode target. Actual size is a power-of-two fraction of original,
# <= this value. 4000 fits a 4K monitor and decodes in ~100-500 ms typical.
TARGET_MAX_DIM = 4000


# ================== decode ==================

def pick_rlevel(w: int, h: int, target: int = TARGET_MAX_DIM) -> int:
    m, level = max(w, h), 0
    while m > target and level < 8:
        m //= 2
        level += 1
    return level


def decode_jp2(path: Path, full_res: bool = False) -> tuple[QImage, int, tuple[int, int]]:
    """Return (QImage, rlevel_used, (full_h, full_w))."""
    rlevel_used = 0
    try:
        import glymur
        glymur.set_option("lib.num_threads", max(1, (os.cpu_count() or 2) // 2))
        jp2 = glymur.Jp2k(str(path))
        full_h, full_w = jp2.shape[0], jp2.shape[1]
        rlevel_used = 0 if full_res else pick_rlevel(full_w, full_h)
        try:
            if rlevel_used > 0:
                step = 2 ** rlevel_used
                arr = jp2[::step, ::step]
            else:
                arr = jp2[:]
        except Exception:
            rlevel_used = 0
            arr = jp2[:]
    except ImportError:
        from PIL import Image
        arr = np.array(Image.open(path))
        full_h, full_w = arr.shape[:2]

    if arr.dtype != np.uint8:
        lo, hi = float(arr.min()), float(arr.max())
        arr = ((arr - lo) / max(hi - lo, 1.0) * 255).astype(np.uint8)

    arr = np.ascontiguousarray(arr)
    if arr.ndim == 2:
        h, w = arr.shape
        img = QImage(arr.data, w, h, w, QImage.Format.Format_Grayscale8)
    else:
        h, w, c = arr.shape
        if c == 3:
            img = QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
        elif c == 4:
            img = QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        else:
            raise ValueError(f"Unsupported channel count: {c}")
    return img.copy(), rlevel_used, (full_h, full_w)


# ================== geo ==================

def read_geo_info(path: Path):
    """Parse the embedded GeoTIFF in the JP2 UUID box.

    Returns (affine6, crs_str) or (None, None) if not a GeoJP2 or if
    required libs aren't installed. The affine is (a, b, c, d, e, f) s.t.
      world_x = a + col * b + row * c
      world_y = d + col * e + row * f
    """
    try:
        import glymur
        import tifffile
    except ImportError:
        return None, None

    try:
        jp2 = glymur.Jp2k(str(path))
    except Exception:
        return None, None

    tiff_blob = None
    for box in getattr(jp2, "box", []):
        uuid = getattr(box, "uuid", None)
        if uuid is not None and str(uuid).lower() == GEOJP2_UUID:
            tiff_blob = getattr(box, "raw_data", None)
            if tiff_blob:
                break
    if not tiff_blob:
        return None, None

    try:
        with tifffile.TiffFile(io.BytesIO(bytes(tiff_blob))) as tf:
            page = tf.pages[0]
            tags = page.tags
            tie = tags.get("ModelTiepointTag")
            scale = tags.get("ModelPixelScaleTag")
            gkd = tags.get("GeoKeyDirectoryTag")
            if not (tie and scale):
                return None, None
            i, j, _, x, y, _ = tie.value[:6]
            sx, sy, _ = scale.value
            a = x - i * sx
            b = sx
            c = 0.0
            d = y + j * sy
            e = 0.0
            f = -sy
            crs = None
            if gkd:
                keys = list(gkd.value)
                if len(keys) >= 4:
                    num = keys[3]
                    for k_i in range(num):
                        base = 4 + k_i * 4
                        if base + 3 >= len(keys):
                            break
                        kid = keys[base]
                        val = keys[base + 3]
                        if kid in (3072, 2048):  # Projected or Geographic CS
                            crs = f"EPSG:{val}"
                            break
            return (a, b, c, d, e, f), crs
    except Exception:
        return None, None


# ================== minimap ==================

class Minimap(QWidget):
    SIZE = QSize(200, 130)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._thumb: QPixmap | None = None
        self._scene_rect = QRectF()
        self._view_rect = QRectF()

    def set_image(self, pixmap: QPixmap, scene_rect: QRectF):
        margin = 6
        avail = self.size() - QSize(margin * 2, margin * 2)
        self._thumb = pixmap.scaled(
            avail,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scene_rect = scene_rect
        self.update()

    def set_view_rect(self, rect: QRectF):
        self._view_rect = rect
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 200))
        p.setPen(QPen(QColor(90, 90, 90), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

        if self._thumb is None or self._scene_rect.isEmpty():
            return

        tx = (self.width() - self._thumb.width()) // 2
        ty = (self.height() - self._thumb.height()) // 2
        p.drawPixmap(tx, ty, self._thumb)

        sx = self._thumb.width() / self._scene_rect.width()
        sy = self._thumb.height() / self._scene_rect.height()
        vr = QRectF(
            tx + (self._view_rect.x() - self._scene_rect.x()) * sx,
            ty + (self._view_rect.y() - self._scene_rect.y()) * sy,
            self._view_rect.width() * sx,
            self._view_rect.height() * sy,
        )
        vr = vr.intersected(QRectF(tx, ty, self._thumb.width(), self._thumb.height()))
        p.setPen(QPen(QColor(255, 220, 0), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(vr)


# ================== main window ==================

class Viewer(QMainWindow):
    def __init__(self, path: str | None = None):
        super().__init__()
        self.setWindowTitle("JP2 Viewer")
        self.resize(1280, 860)

        # --- graphics view ---
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setBackgroundBrush(Qt.GlobalColor.black)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.view.horizontalScrollBar().valueChanged.connect(self._update_overlays)
        self.view.verticalScrollBar().valueChanged.connect(self._update_overlays)
        self.setCentralWidget(self.view)

        # --- state ---
        self.item: QGraphicsPixmapItem | None = None
        self.files: list[Path] = []
        self.index = -1
        self.current_path: Path | None = None
        self.rlevel_loaded = 0
        self.full_shape = (0, 0)
        self.geo_transform = None
        self.crs: str | None = None
        self._upgrading = False

        # --- overlay (top-left) ---
        self.overlay = QLabel(self.view)
        self.overlay.setStyleSheet(
            "color: #fff; background: rgba(0,0,0,160); padding: 4px 8px;"
            "border-radius: 4px; font-family: Consolas, 'Courier New', monospace;"
            "font-size: 11px;"
        )
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.overlay.hide()

        # --- minimap (top-right) ---
        self.minimap = Minimap(self.view)
        self.minimap.hide()

        # --- toolbar + actions ---
        self._build_toolbar()

        # --- status bar ---
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_dims = QLabel("")
        self.status_cursor = QLabel("")
        self.status_cursor.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self.status.addWidget(self.status_dims)
        self.status.addPermanentWidget(self.status_cursor)

        self.setAcceptDrops(True)

        if path:
            self.load(Path(path))

    # --- toolbar / actions ---
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        self.addToolBar(tb)
        self.toolbar = tb
        style = self.style()

        def add(label, shortcut, handler, icon=None, tip=None):
            a = QAction(label, self)
            if icon is not None:
                a.setIcon(style.standardIcon(icon))
            if shortcut:
                a.setShortcut(shortcut)
            a.setToolTip(f"{tip or label}" + (f"  ({shortcut})" if shortcut else ""))
            a.triggered.connect(handler)
            self.addAction(a)
            tb.addAction(a)
            return a

        def add_hidden(shortcut, handler):
            """Shortcut without a toolbar button (for key aliases)."""
            a = QAction(self)
            a.setShortcut(shortcut)
            a.triggered.connect(handler)
            self.addAction(a)

        add("Open", "Ctrl+O", self.open_dialog,
            QStyle.StandardPixmap.SP_DirOpenIcon)
        tb.addSeparator()
        add("◀ Prev", "Left", self.prev_image,
            QStyle.StandardPixmap.SP_ArrowLeft, "Previous")
        add("Next ▶", "Right", self.next_image,
            QStyle.StandardPixmap.SP_ArrowRight, "Next")
        tb.addSeparator()
        add("Fit", "Ctrl+0", self.fit, None, "Fit to window")
        add("100%", "Ctrl+1", self.actual_size, None,
            "Full resolution (reloads if on preview)")
        add("Zoom +", "Ctrl++",
            lambda: self._after(self.view.scale, 1.25, 1.25), None, "Zoom in")
        add_hidden("Ctrl+=", lambda: self._after(self.view.scale, 1.25, 1.25))
        add("Zoom −", "Ctrl+-",
            lambda: self._after(self.view.scale, 0.8, 0.8), None, "Zoom out")
        tb.addSeparator()
        add("↻", "R", lambda: self._after(self.view.rotate, 90),
            None, "Rotate clockwise")
        add("↺", "Shift+R", lambda: self._after(self.view.rotate, -90),
            None, "Rotate counter-clockwise")
        add("Flip H", "H", lambda: self._after(self.view.scale, -1, 1),
            None, "Flip horizontal")
        add("Flip V", "V", lambda: self._after(self.view.scale, 1, -1),
            None, "Flip vertical")
        tb.addSeparator()
        add("⛶", "F", self.toggle_fs, None, "Fullscreen")
        add_hidden("F11", self.toggle_fs)
        add_hidden("Esc", self.exit_fs)

    def _after(self, fn, *args):
        fn(*args)
        self._update_overlays()

    # --- events ---
    def eventFilter(self, obj, ev):
        if obj is self.view.viewport():
            t = ev.type()
            if (t == QEvent.Type.Wheel
                    and ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
                factor = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
                self.view.scale(factor, factor)
                self._update_overlays()
                return True
            if t == QEvent.Type.MouseMove:
                self._update_cursor(ev.position().toPoint())
            if t == QEvent.Type.Resize:
                self._update_overlays()
        return super().eventFilter(obj, ev)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() in JP2_EXTS:
                self.load(p)
                break

    # --- core ---
    def open_dialog(self):
        start = str(self.current_path.parent) if self.current_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open JP2", start,
            "JPEG 2000 (*.jp2 *.j2k *.jpx *.jpf *.jpc);;All files (*.*)"
        )
        if path:
            self.load(Path(path))

    def load(self, path: Path, full_res: bool = False):
        if not path.exists():
            return
        self.current_path = path
        self.setWindowTitle(f"Loading {path.name} …")
        QApplication.processEvents()

        self.files = sorted(
            f for f in path.parent.iterdir() if f.suffix.lower() in JP2_EXTS
        )
        try:
            self.index = self.files.index(path)
        except ValueError:
            self.index = -1

        try:
            img, rlevel, full_shape = decode_jp2(path, full_res=full_res)
        except Exception as e:
            self.scene.clear()
            self.item = None
            self.setWindowTitle(f"Error — {path.name}")
            self.overlay.hide()
            self.minimap.hide()
            self.status_dims.setText(f"Error: {e}")
            self.status_cursor.setText("")
            return

        self.scene.clear()
        pm = QPixmap.fromImage(img)
        self.item = self.scene.addPixmap(pm)
        self.scene.setSceneRect(QRectF(img.rect()))
        self.rlevel_loaded = rlevel
        self.full_shape = full_shape
        self.geo_transform, self.crs = read_geo_info(path)

        self.view.resetTransform()
        self.view.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

        self.minimap.set_image(pm, self.scene.sceneRect())
        self.overlay.show()

        suffix = f"  (1:{2**rlevel})" if rlevel > 0 else ""
        self.setWindowTitle(f"{path.name}{suffix} — JP2 Viewer")

        h, w = full_shape
        geo = f"  {self.crs}" if self.crs else ""
        idx = f"  [{self.index + 1}/{len(self.files)}]" if self.files else ""
        self.status_dims.setText(f"{w} × {h} px{geo}{idx}")

        self._update_overlays()

    def fit(self):
        if self.item:
            self.view.resetTransform()
            self.view.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)
        self._update_overlays()

    def actual_size(self):
        if self.rlevel_loaded > 0 and self.current_path is not None:
            self.load(self.current_path, full_res=True)
        self.view.resetTransform()
        self._update_overlays()

    def toggle_fs(self):
        if self.isFullScreen():
            self.showNormal()
            self.toolbar.show()
            self.status.show()
        else:
            self.toolbar.hide()
            self.status.hide()
            self.showFullScreen()

    def exit_fs(self):
        if self.isFullScreen():
            self.showNormal()
            self.toolbar.show()
            self.status.show()

    def next_image(self):
        if self.files and self.index < len(self.files) - 1:
            self.index += 1
            self.load(self.files[self.index])

    def prev_image(self):
        if self.files and self.index > 0:
            self.index -= 1
            self.load(self.files[self.index])

    # --- overlays ---
    def _current_zoom(self) -> float:
        """Display scale relative to ORIGINAL image pixels (not preview)."""
        t = self.view.transform()
        det = abs(t.m11() * t.m22() - t.m12() * t.m21())
        scale_preview = math.sqrt(det)
        return scale_preview / (2 ** self.rlevel_loaded)

    def _update_overlays(self):
        self._reposition_overlays()
        if not self.item:
            return

        # If the user has zoomed in enough to make the preview look bad,
        # transparently reload the full-resolution data.
        self._maybe_upgrade_resolution()

        zoom_pct = self._current_zoom() * 100
        name = self.current_path.name if self.current_path else ""
        level = f"  ·  1:{2**self.rlevel_loaded} preview" if self.rlevel_loaded > 0 else ""
        self.overlay.setText(f"{name}   {zoom_pct:.1f}%{level}")
        self.overlay.adjustSize()

        # Minimap: update the view-rect indicator and keep it visible while
        # an image is loaded (the yellow rect will tighten as you zoom in).
        vp_rect = self.view.viewport().rect()
        scene_view_rect = self.view.mapToScene(vp_rect).boundingRect()
        self.minimap.set_view_rect(scene_view_rect)
        self.minimap.setVisible(True)

    def _maybe_upgrade_resolution(self):
        """Swap a downsampled preview for the full-resolution data in-place.

        Preserves the current zoom and the image point under the viewport
        centre, so the upgrade is visually seamless except for pixels getting
        sharper. Rotation/flip, if any, will be reset (acceptable trade-off
        for the simplicity).
        """
        if self._upgrading or self.rlevel_loaded == 0 or self.current_path is None:
            return
        if self._current_zoom() < 0.15:  # below 15% of original, preview is fine
            return

        self._upgrading = True
        try:
            factor = 2 ** self.rlevel_loaded
            vp_centre = self.view.viewport().rect().center()
            scene_pt = self.view.mapToScene(vp_centre)
            img_x = scene_pt.x() * factor
            img_y = scene_pt.y() * factor
            zoom = self._current_zoom()

            try:
                img, _rlevel, _shape = decode_jp2(self.current_path, full_res=True)
            except Exception:
                return

            self.scene.clear()
            pm = QPixmap.fromImage(img)
            self.item = self.scene.addPixmap(pm)
            self.scene.setSceneRect(QRectF(img.rect()))
            self.rlevel_loaded = 0
            self.minimap.set_image(pm, self.scene.sceneRect())

            # Restore the view: same zoom, same image point centred.
            self.view.resetTransform()
            self.view.scale(zoom, zoom)
            self.view.centerOn(img_x, img_y)

            self.setWindowTitle(f"{self.current_path.name} — JP2 Viewer")
        finally:
            self._upgrading = False

    def _reposition_overlays(self):
        margin = 10
        vp = self.view.viewport()
        self.overlay.move(margin, margin)
        self.minimap.move(vp.width() - self.minimap.width() - margin, margin)

    def _update_cursor(self, pos):
        if not self.item:
            self.status_cursor.setText("")
            return
        sp = self.view.mapToScene(pos)
        if not self.scene.sceneRect().contains(sp):
            self.status_cursor.setText("")
            return
        factor = 2 ** self.rlevel_loaded
        col = int(sp.x() * factor)
        row = int(sp.y() * factor)
        txt = f"px ({col}, {row})"
        if self.geo_transform:
            a, b, c, d, e, f = self.geo_transform
            wx = a + col * b + row * c
            wy = d + col * e + row * f
            prec = 6 if abs(b) < 0.01 else 2
            txt += f"   world ({wx:.{prec}f}, {wy:.{prec}f})"
        self.status_cursor.setText(txt)


def main():
    app = QApplication(sys.argv)
    w = Viewer(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
