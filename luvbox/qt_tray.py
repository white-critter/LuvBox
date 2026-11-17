"""Keep the Qt application reachable while its window is hidden."""

from PySide6.QtCore import QMetaObject, QObject, Qt, Slot
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayController(QObject):
    def __init__(self, application, window, controller):
        super().__init__(controller)
        self._window = window
        self._controller = controller
        self._closed = False
        self._menu = QMenu()
        self._open = self._menu.addAction("LuvBox 열기", self.show)
        self._menu.setDefaultAction(self._open)
        self._settings = self._menu.addAction("설정", self.openSettings)
        self._menu.addSeparator()
        self._exit = self._menu.addAction("종료", controller.requestClose)
        self.icon = QSystemTrayIcon(application.windowIcon(), self)
        self.icon.setContextMenu(self._menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self.show)
        controller.changed.connect(self._changed)
        # A new save question must not wait unnoticed behind the tray.
        controller.reviewReady.connect(self.show)
        controller.storageConfirmationRequested.connect(self.show)
        controller.shutdownFinished.connect(self.close)
        controller.shutdownFinished.connect(application.quit)
        application.setQuitOnLastWindowClosed(False)
        self._changed()
        self.icon.show()

    @Slot()
    def show(self):
        if self._controller.closing:
            return
        if self._window.visibility() == QWindow.Visibility.Hidden:
            # Reset the native state while hidden: showing a hidden maximized
            # QWindow directly can leave Windows unable to restore its size.
            maximized = self._window.property("lastMaximized")
            place = self._window.property("normalPlace")
            self._window.setWindowState(Qt.WindowState.WindowNoState)
            if maximized and place.width() > 0 and place.height() > 0:
                self._window.setGeometry(place.toRect())
            self._window.showNormal()
            if maximized:
                self._window.showMaximized()
        elif self._window.visibility() == QWindow.Visibility.Minimized:
            if self._window.property("lastMaximized"):
                self._window.showMaximized()
            else:
                self._window.showNormal()
        else:
            self._window.show()
        self._window.raise_()
        self._window.requestActivate()
        # A scheduled save may have ended while the window was away: show its photos now.
        self._controller.checkScheduledRun()

    @Slot()
    def openSettings(self):
        if self._controller.closing:
            return
        self.show()
        # Keep a pending confirmation in front instead of stacking another dialog.
        if not self._window.property("modalOpen"):
            QMetaObject.invokeMethod(self._window, "openPreferences", Qt.ConnectionType.DirectConnection)

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show()

    @Slot()
    def _changed(self):
        enabled = not self._controller.closing
        for action in (self._open, self._settings, self._exit):
            action.setEnabled(enabled)
        self.icon.setToolTip("LuvBox · " + self._controller.status[:100])

    @Slot()
    def close(self):
        if self._closed:
            return
        self._closed = True
        self.icon.hide()
        self._menu.close()
        self._menu.deleteLater()
