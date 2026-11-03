"""Zoomable / pannable SVG viewer used for plan graphs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QWheelEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView


class SvgView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._renderer: QSvgRenderer | None = None
        self._item: QGraphicsSvgItem | None = None
        self._message = None
        self._fit = True
        self.svg_bytes: bytes = b""
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def show_message(self, text: str) -> None:
        self._scene.clear()
        self._item = None
        self._renderer = None
        self.svg_bytes = b""
        self._message = self._scene.addText(text)
        self._message.setDefaultTextColor(Qt.GlobalColor.gray)
        self.resetTransform()
        self._scene.setSceneRect(self._scene.itemsBoundingRect())

    def load_svg(self, data: bytes) -> None:
        renderer = QSvgRenderer(data, self)
        if not renderer.isValid():
            self.show_message("Could not render the plan graph.")
            return
        self._scene.clear()
        self._message = None
        self.svg_bytes = data
        self._renderer = renderer
        self._item = QGraphicsSvgItem()
        self._item.setSharedRenderer(renderer)
        self._scene.addItem(self._item)
        self._scene.setSceneRect(self._item.boundingRect())
        self.fit()

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
