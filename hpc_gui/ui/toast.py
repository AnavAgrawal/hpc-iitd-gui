"""Non-modal toast notifications + a tiny busy indicator."""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect, QSize, Qt,
    QTimer, QPointF,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget, QPushButton,
    QVBoxLayout,
)

from . import style


LEVEL_COLORS = {
    "info": (style.INFO, "ℹ"),
    "success": (style.OK, "✓"),
    "warn": (style.WARN, "⚠"),
    "error": (style.ERR, "✕"),
}


class _Toast(QWidget):
    """A single toast banner. Auto-dismisses after ``duration`` ms."""

    def __init__(self, parent: QWidget, level: str, title: str, message: str,
                 duration: int = 4000) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("toast")
        color, icon = LEVEL_COLORS.get(level, LEVEL_COLORS["info"])
        self.setStyleSheet(f"""
            #toast {{
                background: {style.SURFACE_HI};
                border: 1px solid {color};
                border-left: 4px solid {color};
                border-radius: 8px;
            }}
            QLabel#icon {{ color: {color}; font-size: 15pt; font-weight: bold; }}
            QLabel#title {{ color: {style.TEXT}; font-weight: 600; }}
            QLabel#body  {{ color: {style.TEXT_DIM}; }}
            QPushButton#close {{
                background: transparent; border: none; color: {style.TEXT_DIM};
                font-weight: bold; padding: 2px 8px;
            }}
            QPushButton#close:hover {{ color: {style.TEXT}; }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 8, 10)
        layout.setSpacing(10)
        ilabel = QLabel(icon); ilabel.setObjectName("icon")
        ilabel.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        ilabel.setFixedWidth(20)
        layout.addWidget(ilabel)

        text_col = QVBoxLayout(); text_col.setSpacing(2); text_col.setContentsMargins(0, 0, 0, 0)
        if title:
            t = QLabel(title); t.setObjectName("title"); t.setWordWrap(True)
            text_col.addWidget(t)
        body = QLabel(message); body.setObjectName("body"); body.setWordWrap(True)
        text_col.addWidget(body)
        layout.addLayout(text_col, 1)

        btn = QPushButton("×"); btn.setObjectName("close"); btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.dismiss)
        layout.addWidget(btn, 0, Qt.AlignTop)

        self.setFixedWidth(360)
        self.adjustSize()

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        self._fade_in = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_in.setDuration(220); self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0); self._fade_in.setEasingCurve(QEasingCurve.OutCubic)

        self._fade_out = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_out.setDuration(220); self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0); self._fade_out.setEasingCurve(QEasingCurve.InCubic)
        self._fade_out.finished.connect(self._finalize)

        self._timer = QTimer(self); self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._timer.start(duration)

    def show_in(self, pos: QPoint) -> None:
        self.move(pos)
        self.show()
        self.raise_()
        self._fade_in.start()

    def dismiss(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
        self._fade_out.start()

    def _finalize(self) -> None:
        self.hide()
        mgr = self.parent()
        if isinstance(mgr, QObject):
            mgr = mgr.property("_toast_manager")
            if mgr:
                mgr._remove(self)
        self.deleteLater()


class ToastManager(QObject):
    """Owns the stack of visible toasts in the bottom-right of a QMainWindow."""

    MARGIN = 14
    GAP = 8

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._toasts: list[_Toast] = []
        window.installEventFilter(self)

    def show(self, level: str, message: str, title: str = "", duration: int = 4200) -> None:
        t = _Toast(self._window, level, title, message, duration)
        t.setProperty("_toast_manager", self)
        self._toasts.append(t)
        self._reflow()
        t.show_in(t.pos())

    # Convenience levels
    def info(self, msg: str, title: str = "") -> None: self.show("info", msg, title)
    def success(self, msg: str, title: str = "") -> None: self.show("success", msg, title)
    def warn(self, msg: str, title: str = "") -> None: self.show("warn", msg, title, 6000)
    def error(self, msg: str, title: str = "") -> None: self.show("error", msg, title, 7000)

    # Internals
    def _remove(self, t: _Toast) -> None:
        if t in self._toasts:
            self._toasts.remove(t)
        self._reflow()

    def _reflow(self) -> None:
        geom = self._window.geometry()
        # Anchor relative to the window widget (toast is a child of window).
        x_right = self._window.width() - self.MARGIN
        y = self._window.height() - self.MARGIN
        for t in reversed(self._toasts):
            t.adjustSize()
            t.move(x_right - t.width(), y - t.height())
            y -= t.height() + self.GAP

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._window and ev.type() in (ev.Type.Resize, ev.Type.Move):
            self._reflow()
        return False


class BusySpinner(QWidget):
    """A tiny rotating arc that can be flashed while async work is in flight."""

    def __init__(self, parent: QWidget | None = None, size: int = 16,
                 color: str = style.ACCENT) -> None:
        super().__init__(parent)
        self._size = size
        self._angle = 0
        self._color = QColor(color)
        self.setFixedSize(QSize(size, size))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def _tick(self) -> None:
        self._angle = (self._angle + 24) % 360
        self.update()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start(40)
        self.show()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._color)
        pen.setWidth(2); pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        # Draw a 270° arc rotated by _angle
        r = self.rect().adjusted(2, 2, -2, -2)
        p.drawArc(r, -self._angle * 16, 270 * 16)
