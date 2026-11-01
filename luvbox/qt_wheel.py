"""Scope click-wheel interception to one Qt view without redispatching input."""

from PySide6.QtCore import QEvent, QObject, Property, Signal, Slot, Qt
from PySide6.QtGui import QInputDevice
from PySide6.QtQuick import QQuickItem
from shiboken6 import isValid


class WheelInputFilter(QObject):
    wheelRequested = Signal(int)
    resetRequested = Signal()
    viewChanged = Signal()
    enabledChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = None
        self._window = None
        self._enabled = True
        self._closed = False

    @Property(QQuickItem, notify=viewChanged)
    def view(self):
        return self._view

    @view.setter
    def view(self, view):
        if self._closed or view is self._view:
            return
        previous = self._view
        if previous is not None and isValid(previous):
            previous.windowChanged.disconnect(self._window_changed)
            previous.destroyed.disconnect(self._view_destroyed)
        self._view = view
        if view is not None:
            view.windowChanged.connect(self._window_changed)
            view.destroyed.connect(self._view_destroyed)
        self._window_changed(view.window() if view is not None else None)
        self.viewChanged.emit()
        self.resetRequested.emit()

    @Property(bool, notify=enabledChanged)
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        if self._closed:
            return
        value = bool(value)
        if value == self._enabled:
            return
        self._enabled = value
        self.enabledChanged.emit()
        if not value:
            self.resetRequested.emit()

    def _window_changed(self, window):
        previous = self._window
        if previous is window:
            return
        if previous is not None and isValid(previous):
            previous.removeEventFilter(self)
            previous.destroyed.disconnect(self._window_destroyed)
        self._window = window
        if window is not None and not self._closed:
            window.installEventFilter(self)
            window.destroyed.connect(self._window_destroyed)
        self.resetRequested.emit()

    def _window_destroyed(self):
        self._window = None

    @staticmethod
    def _class_name(item) -> str:
        name = item.metaObject().className()
        if isinstance(name, bytes):
            name = name.split(b"\0", 1)[0].decode("ascii", "replace")
        return str(name)

    def _popup_contains(self, position) -> bool:
        window = self._window
        if window is None or not isValid(window):
            return False
        content = window.contentItem()
        if content is None or not isValid(content):
            return False
        overlay = next((child for child in content.childItems()
                        if isValid(child) and self._class_name(child) == "QQuickOverlay"), None)
        if overlay is None:
            return False
        stack = list(overlay.childItems())
        while stack:
            item = stack.pop()
            if not isValid(item) or not item.isVisible():
                continue
            if (self._class_name(item) == "QQuickPopupItem" and
                    item.contains(item.mapFromScene(position))):
                return True
            stack.extend(item.childItems())
        return False

    def _view_destroyed(self):
        self._view = None
        self._window_changed(None)
        self.viewChanged.emit()
        self.resetRequested.emit()

    def eventFilter(self, watched, event):
        view = self._view
        if (self._closed or not self._enabled or watched is not self._window or
                view is None or not isValid(view) or not view.isEnabled() or not view.isVisible()):
            return False
        kind = event.type()
        if kind in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.TouchBegin):
            self.resetRequested.emit()
            return False
        if kind != QEvent.Type.Wheel:
            return False
        position = event.position()
        if not view.contains(view.mapFromScene(position)):
            return False
        # A combo or calendar opens over the grid. The wheel belongs to that
        # popup while the pointer is inside it, including when the list is
        # already at its end. The gallery still scrolls beside the popup.
        if self._popup_contains(position):
            return False
        if (event.pointingDevice().type() != QInputDevice.DeviceType.Mouse or
                event.source() != Qt.MouseEventSource.MouseEventNotSynthesized or
                event.modifiers() != Qt.KeyboardModifier.NoModifier or
                not event.pixelDelta().isNull() or event.angleDelta().x() != 0 or
                event.angleDelta().y() == 0 or event.phase() != Qt.ScrollPhase.NoScrollPhase):
            self.resetRequested.emit()
            return False
        self.wheelRequested.emit(event.angleDelta().y())
        event.accept()
        return True

    @Slot()
    def close(self):
        if self._closed:
            return
        self.view = None
        self._closed = True
        self._enabled = False
        self.enabledChanged.emit()
