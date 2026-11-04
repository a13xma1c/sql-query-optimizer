"""Zoomable / pannable SVG viewer used for plan graphs.

Beyond plain rendering, this view supports click-to-select: callers register a
mapping of SVG element id -> arbitrary payload with :meth:`set_hit_map`, and the
view resolves a click to the *smallest* matching element (so a node's label text
doesn't steal clicks over a tighter-fitting neighbour) and emits
:attr:`elementClicked` / :attr:`elementDoubleClicked`. Element geometry comes from
the SVG itself (:func:`sqlanalyzer.plans.graph.element_bounds`) rather than from
``QSvgRenderer.boundsOnElement``, which answers differently per platform because it
includes platform-measured text. Panning (``ScrollHandDrag``) is preserved - a
press/release is only treated as a click when the mouse barely moved between the
two events.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QWheelEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import QGraphicsScene, QGraphicsTextItem, QGraphicsView

from ..plans.graph import element_bounds

_CLICK_THRESHOLD = 4  # pixels; beyond this a press/release pair is a pan, not a click


class SvgView(QGraphicsView):
    elementClicked = Signal(object)  # payload registered for the clicked element, or None
    elementDoubleClicked = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._renderer: QSvgRenderer | None = None
        self._item: QGraphicsSvgItem | None = None
        self._message: QGraphicsTextItem | None = None
        self._fit = True
        self.svg_bytes: bytes = b""
        self._hit_map: dict[str, Any] = {}
        self._bounds: dict[str, QRectF] = {}
        self._press_pos: QPointF | None = None
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def show_message(self, text: str) -> None:
        self._scene.clear()
        self._item = None
        self._renderer = None
        self.svg_bytes = b""
        self._hit_map = {}
        self._bounds = {}
        message = self._scene.addText(text)
        message.setDefaultTextColor(Qt.GlobalColor.gray)
        self._message = message
        self.resetTransform()
        self._scene.setSceneRect(self._scene.itemsBoundingRect())

    def load_svg(self, data: bytes, hit_map: dict[str, Any] | None = None) -> None:
        renderer = QSvgRenderer(data, self)
        if not renderer.isValid():
            self.show_message("Could not render the plan graph.")
            return
        self._scene.clear()
        self._message = None
        self.svg_bytes = data
        self._renderer = renderer
        self._hit_map = dict(hit_map or {})
        self._bounds = {gid: QRectF(*box) for gid, box in element_bounds(data).items()}
        self._item = QGraphicsSvgItem()
        self._item.setSharedRenderer(renderer)
        self._scene.addItem(self._item)
        self._scene.setSceneRect(self._item.boundingRect())
        self.fit()

    def set_hit_map(self, hit_map: dict[str, Any]) -> None:
        """Update which element ids resolve to which payload without re-rendering."""
        self._hit_map = dict(hit_map)

    def element_rect(self, element_id: str) -> QRectF | None:
        """Bounding box of an SVG element in the item's (= scene's, at identity) coordinates."""
        return self._bounds.get(element_id)

    def fit(self) -> None:
        self._fit = True
        self.resetTransform()
        if self._item is not None:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
            if self.transform().m11() > 1.0:  # never enlarge small graphs
                self.resetTransform()

    def zoom(self, factor: float) -> None:
        self._fit = False
        self.scale(factor, factor)

    def reset_zoom(self) -> None:
        self._fit = False
        self.resetTransform()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier or event.angleDelta().y():
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit and self._item is not None:
            self.fit()

    # ------------------------------------------------------------------- hit-testing
    def _hit_test(self, scene_pos) -> Any:
        """Return the payload of the smallest element under ``scene_pos``, or ``None``."""
        if self._item is None or not self._hit_map:
            return None
        pos = self._item.mapFromScene(scene_pos)
        best: Any = None
        best_area = None
        for element_id, payload in self._hit_map.items():
            rect = self._bounds.get(element_id)
            if rect is None or not rect.contains(pos):
                continue
            area = rect.width() * rect.height()
            if best_area is None or area < best_area:
                best, best_area = payload, area
        return best

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._press_pos = event.position()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if self._press_pos is not None and (event.position() - self._press_pos).manhattanLength() <= _CLICK_THRESHOLD:
            payload = self._hit_test(self.mapToScene(event.position().toPoint()))
            if payload is not None:
                self.elementClicked.emit(payload)
        self._press_pos = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        super().mouseDoubleClickEvent(event)
        payload = self._hit_test(self.mapToScene(event.position().toPoint()))
        if payload is not None:
            self.elementDoubleClicked.emit(payload)
