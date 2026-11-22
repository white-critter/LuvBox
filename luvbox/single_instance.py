"""One LuvBox per person: starting it again shows the window that is already running.

Since 1.11.0 closing the window leaves LuvBox in the tray. Starting LuvBox.exe again
(Start menu, desktop, the scheduled run's "앱 열기") must then bring that window back,
not open a second program on the same library.

The first program owns a named mutex in the session's namespace for its whole run
and listens on a local socket that only the same Windows user can reach. A later
start finds the mutex owned, asks the first program over the socket to show its
window, lets it take the foreground, and ends. A program that is shutting down
stops listening first; a start in that moment (the restart after an update, or a
quick reopen after "종료") waits for the mutex and then becomes the first program
itself. Both names come from the person's data folder and the Windows session, so
a test or startup check with a private LOCALAPPDATA never meets the real program.

Elsewhere than Windows nothing is guarded: every start is a first program.
"""

import hashlib
import os
import sys
import time

from PySide6.QtCore import QObject, QTimer, Slot

_WAIT_OBJECT_0 = 0x0
_WAIT_ABANDONED = 0x80
_REQUEST = b"show\n"
_ANSWER = b"ok\n"
# How long a second start keeps trying before it opens a window of its own: a
# first program that is closing ends its saves first, which can take a while.
DEADLINE = 30.0


def _identity() -> str:
    from .settings import _folders

    folder = os.path.normcase(os.path.abspath(_folders()[0]))
    session = 0
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32")
            kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
            kernel.ProcessIdToSessionId.restype = wintypes.BOOL
            value = wintypes.DWORD()
            if kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(value)):
                session = value.value
        except (OSError, AttributeError):
            pass
    return hashlib.sha256(f"{session}\0{folder}".encode("utf-8")).hexdigest()[:32]


class _Windows:
    """The few kernel32/user32 calls this needs, with their argument types."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetNamedPipeServerProcessId.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        user = ctypes.WinDLL("user32")
        user.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        user.AllowSetForegroundWindow.restype = wintypes.BOOL
        self.kernel = kernel
        self.user = user
        self.ulong = wintypes.ULONG

    def server_process(self, handle) -> int:
        value = self.ulong()
        if handle and self.kernel.GetNamedPipeServerProcessId(handle, self.ctypes.byref(value)):
            return int(value.value)
        return 0


class InstanceGuard(QObject):
    def __init__(self, parent=None, *, identity=None):
        super().__init__(parent)
        name = identity or _identity()
        self.server_name = "LuvBox-instance-" + name
        self._mutex_name = "Local\\LuvBox-instance-" + name
        self._windows = None
        self._mutex = None
        self.owner = False
        self._server = None
        self._handler = None
        self._buffers = {}
        self._adopt = None
        self._listen_retry = None
        self._stopped = False
        self.requests = 0

    def claim(self, deadline=DEADLINE) -> bool:
        """True when this start is the program to run; False when another one now shows its window."""
        if sys.platform != "win32":
            return True
        try:
            self._windows = _Windows()
        except (OSError, AttributeError):
            return True
        self._mutex = self._windows.kernel.CreateMutexW(None, False, self._mutex_name)
        if not self._mutex:
            return True                              # cannot tell: run as before
        if self._take(0):
            return True
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if self._ask_to_show():
                self._windows.kernel.CloseHandle(self._mutex)
                self._mutex = None
                return False
            if self._take(250):
                return True
        # The first program neither answers nor ends. Open anyway, as before
        # 1.11.2, and take over the name once that program has gone.
        self._adopt = QTimer(self)
        self._adopt.setInterval(1000)
        self._adopt.timeout.connect(self._try_adopt)
        self._adopt.start()
        return True

    def _take(self, milliseconds) -> bool:
        result = self._windows.kernel.WaitForSingleObject(self._mutex, milliseconds)
        if result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            return False
        self.owner = True
        self._listen()
        return True

    @Slot()
    def _try_adopt(self):
        if self._stopped:
            self._adopt.stop()                   # closing: the next start waits for this one to end
        elif self._mutex and not self.owner and self._take(0):
            self._adopt.stop()

    def _listen(self):
        from PySide6.QtNetwork import QLocalServer

        if self._server is not None or self._stopped or not self.owner:
            return
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._accept)
        if server.listen(self.server_name):
            self._server = server
            if self._listen_retry is not None:
                self._listen_retry.stop()
        else:
            server.deleteLater()
            if self._listen_retry is None:
                timer = QTimer(self)
                timer.setInterval(500)
                timer.timeout.connect(self._listen)
                self._listen_retry = timer
            if not self._listen_retry.isActive():
                self._listen_retry.start()

    def _ask_to_show(self) -> bool:
        from PySide6.QtNetwork import QLocalSocket

        socket = QLocalSocket()
        try:
            socket.connectToServer(self.server_name)
            if not socket.waitForConnected(1000):
                return False
            # Let the first program bring its window to the front: Windows gives
            # the right to this start (from the launcher), not to a program in the tray.
            pid = self._windows.server_process(int(socket.socketDescriptor()))
            if pid:
                self._windows.user.AllowSetForegroundWindow(pid)
            socket.write(_REQUEST)
            if not socket.waitForBytesWritten(1000):
                return False
            answer = b""
            give_up = time.monotonic() + 3
            while not answer.endswith(b"\n") and len(answer) < 16 and time.monotonic() < give_up:
                if socket.bytesAvailable() or socket.waitForReadyRead(200):
                    answer += bytes(socket.readAll())
                elif socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
                    break
            return answer == _ANSWER
        finally:
            socket.abort()
            socket.deleteLater()

    def attach(self, handler):
        """`handler()` shows the window and returns whether it could."""
        self._handler = handler

    @Slot()
    def _accept(self):
        server = self._server
        while server is not None and server.hasPendingConnections():
            socket = server.nextPendingConnection()
            self._buffers[socket] = b""
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._forget(s))
            self._read(socket)

    def _forget(self, socket):
        self._buffers.pop(socket, None)
        socket.deleteLater()

    def _read(self, socket):
        if socket not in self._buffers:
            return
        data = self._buffers[socket] + bytes(socket.readAll())
        self._buffers[socket] = data
        if len(data) < len(_REQUEST) and _REQUEST.startswith(data):
            return
        self._buffers.pop(socket, None)
        if data != _REQUEST or self._server is None:
            socket.abort()
            return
        self.requests += 1
        # No window yet (still starting): it appears by itself.
        shown = True if self._handler is None else bool(self._handler())
        if shown:
            socket.write(_ANSWER)
            socket.flush()
            socket.disconnectFromServer()
        else:
            socket.abort()

    @Slot()
    def stopListening(self):
        """While closing: a new start waits for this program to end instead."""
        self._stopped = True
        if self._listen_retry is not None:
            self._listen_retry.stop()
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        for socket in list(self._buffers):
            socket.abort()
        self._buffers.clear()

    def release(self):
        """At the very end, before a restart is started: the next start becomes the first program."""
        self.stopListening()
        if self._adopt is not None:
            self._adopt.stop()
        if self._mutex:
            if self.owner:
                self._windows.kernel.ReleaseMutex(self._mutex)
            self._windows.kernel.CloseHandle(self._mutex)
            self._mutex = None
        self.owner = False
