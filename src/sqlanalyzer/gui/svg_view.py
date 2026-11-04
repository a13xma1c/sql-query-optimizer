"""Zoomable / pannable SVG viewer used for plan graphs.

Beyond plain rendering, this view supports click-to-select: callers register a
mapping of SVG element id -> arbitrary payload with :meth:`set_hit_map`, and the
view resolves a click to the *smallest* matching element (so a node's label text
doesn't steal clicks over a tighter-fitting neighbour) and emits
:attr:`elementClicked` / :attr:`elementDoubleClicked`. Panning (``ScrollHandDrag``)
is preserved - a press/release is only treated as a click when the mouse barely
moved between the two events.

Hit rectangles come from the SVG *source* (:func:`element_rects`), not from
``QSvgRenderer.boundsOnElement``: the latter includes the ``<text>`` children of a
node group and therefore depends on the platform's fonts and DPI - on Windows it
reported node boxes about twice as wide as drawn, so clicks landed on the neighbour.
Graphviz writes each node's shape (``path``/``polygon``/``ellipse``) in plain graph
coordinates plus one ``translate``/``scale`` on the enclosing graph group, which is
all that is needed and is identical on every platform. ``boundsOnElement`` remains
the fallback for ids the parser does not know.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QWheelEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import QGraphicsScene, QGraphicsTextItem, QGraphicsView

_CLICK_THRESHOLD = 4  # pixels; beyond this a press/release pair is a pan, not a click
_SVG_NS = "{http://www.w3.org/2000/svg}"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_SHAPES = ("path", "polygon", "ellipse", "rect")

Rect = tuple[float, float, float, float]  # x, y, width, height


def _transform(spec: str) -> tuple[float, float, float, float]:
    """``(sx, sy, tx, ty)`` of a Graphviz group transform (``scale(..) rotate(0) translate(..)``)."""
    sx = sy = 1.0
    tx = ty = 0.0
    for name, args in re.findall(r"(scale|translate)\(([^)]*)\)", spec):
        nums = [float(n) for n in _NUMBER.findall(args)]
        if name == "scale" and nums:
            sx, sy = nums[0], (nums[1] if len(nums) > 1 else nums[0])
        elif name == "translate" and nums:
            tx, ty = nums[0], (nums[1] if len(nums) > 1 else 0.0)
    return sx, sy, tx, ty


def _shape_bbox(el: ET.Element) -> Rect | None:
    tag = el.tag.removeprefix(_SVG_NS)
    if tag == "ellipse":
        cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
        rx, ry = float(el.get("rx", 0)), float(el.get("ry", 0))
        return cx - rx, cy - ry, 2 * rx, 2 * ry
    if tag == "rect":
        return (float(el.get("x", 0)), float(el.get("y", 0)), float(el.get("width", 0)), float(el.get("height", 0)))
    data = el.get("points") if tag == "polygon" else el.get("d")
    if not data:
        return None
    nums = [float(n) for n in _NUMBER.findall(data)]
    if len(nums) < 4:
        return None
    xs, ys = nums[0::2], nums[1::2]  # Graphviz emits absolute coordinates only (M/C/L with x,y pairs)
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def element_rects(svg: bytes | str) -> dict[str, Rect]:
    """``element id -> (x, y, w, h)`` in viewBox coordinates for every ``<g id=...>`` that contains a shape.

    Only the first shape of a group counts (a Graphviz node group has exactly one; its text is
    ignored on purpose, see the module docstring). Edge, graph and cluster groups are skipped.
    """
    try:
        root = ET.fromstring(svg.encode("utf-8") if isinstance(svg, str) else svg)
    except ET.ParseError:
        return {}
    out: dict[str, Rect] = {}

    def walk(node: ET.Element, sx: float, sy: float, tx: float, ty: float) -> None:
        for child in node:
            if child.tag != f"{_SVG_NS}g":
                continue
            csx, csy, ctx, cty = sx, sy, tx, ty
            spec = child.get("transform")
            if spec:
                gsx, gsy, gtx, gty = _transform(spec)
                csx, csy, ctx, cty = sx * gsx, sy * gsy, tx + sx * gtx, ty + sy * gty
            element_id = child.get("id")
            if element_id and child.get("class") not in ("edge", "graph", "cluster"):
                shape = next((e for e in child.iter() if e.tag.removeprefix(_SVG_NS) in _SHAPES), None)
                bbox = _shape_bbox(shape) if shape is not None else None
                if bbox is not None:
                    x, y, w, h = bbox
                    out[element_id] = ((x + ctx) * csx, (y + cty) * csy, w * csx, h * csy)
            walk(child, csx, csy, ctx, cty)

    walk(root, 1.0, 1.0, 0.0, 0.0)
    return out


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
        self._rects: dict[str, Rect] = {}  # from the SVG source; see element_rects()
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
        self._rects = {}
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
        self._rects = element_rects(data) if self._hit_map else {}
        self._item = QGraphicsSvgItem()
        self._item.setSharedRenderer(renderer)
        self._scene.addItem(self._item)
        self._scene.setSceneRect(self._item.boundingRect())
        self.fit()

    def set_hit_map(self, hit_map: dict[str, Any]) -> None:
        """Update which element ids resolve to which payload without re-rendering."""
        self._hit_map = dict(hit_map)

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
    def element_rect(self, element_id: str) -> QRectF | None:
        """Hit rectangle of ``element_id`` in item coordinates (the SVG's viewBox space)."""
        if self._renderer is None:
            return None
        rect = self._rects.get(element_id)
        if rect is not None:
            x, y, w, h = rect
            view_box = self._renderer.viewBoxF()
            if not view_box.isEmpty():  # viewBox -> item coordinates (identity for Graphviz output)
                bounds = self._item.boundingRect() if self._item is not None else view_box
                fx, fy = bounds.width() / view_box.width(), bounds.height() / view_box.height()
                return QRectF((x - view_box.x()) * fx, (y - view_box.y()) * fy, w * fx, h * fy)
            return QRectF(x, y, w, h)
        if not self._renderer.elementExists(element_id):
            return None
        return self._renderer.transformForElement(element_id).mapRect(self._renderer.boundsOnElement(element_id))

    def _hit_test(self, scene_pos) -> Any:
        """Return the payload of the smallest element under ``scene_pos``, or ``None``."""
        if self._item is None or self._renderer is None or not self._hit_map:
            return None
        pos = self._item.mapFromScene(scene_pos)
        best: Any = None
        best_area = None
        for element_id, payload in self._hit_map.items():
            rect = self.element_rect(element_id)
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
