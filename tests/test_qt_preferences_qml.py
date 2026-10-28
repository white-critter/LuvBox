"""Exercise visible settings feedback and input recovery in the product QML."""
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def check_scene():
    from PySide6.QtCore import QObject, Property, QUrl, Qt, Signal, Slot, QtMsgType, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from PySide6.QtQuickControls2 import QQuickStyle

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda kind, _context, message: messages.append(message)
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg) else None)

    class Fixture(QObject):
        preferencesChanged = Signal()
        changed = Signal()
        def __init__(self):
            super().__init__()
            self._values = {"max_video_mb": 200}
            self._error = ""
            self.writes = []
        @Property("QVariantMap", notify=preferencesChanged)
        def preferences(self): return self._values
        @Property(bool, constant=True)
        def busy(self): return False
        @Property(str, notify=changed)
        def error(self): return self._error
        @Slot(str, "QVariant")
        def setPreference(self, key, value):
            self.writes.append((key, value))
            self._values[key] = value
            self.preferencesChanged.emit()
        @Slot()
        def openRecordsFolder(self): pass
        def set_error(self, value):
            self._error = value
            self.changed.emit()

    fixture = Fixture()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("fixtureApp", fixture)
    engine.loadData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    id: window
    width: 680; height: 480; visible: true
    PreferencesDialog { id: prefs; objectName: "prefs"; parent: Overlay.overlay; app: fixtureApp }
    function openPrefs() { prefs.open() }
}
''', QUrl.fromLocalFile(str(ROOT / 'assets/qt/preferences-test.qml')))
    assert engine.rootObjects(), messages
    window = engine.rootObjects()[0]
    window.openPrefs()
    QTest.qWait(100)
    dialog = window.findChild(QObject, 'prefs')
    advanced = dialog.findChild(QQuickItem, 'advancedSettingsToggle')
    field = dialog.findChild(QQuickItem, 'videoLimitField')
    error = dialog.findChild(QQuickItem, 'preferencesError')
    assert all(x is not None for x in (advanced, field, error))
    advanced.setProperty('checked', True)
    QTest.qWait(50)
    assert field.property('visible')
    field.forceActiveFocus()
    QTest.qWait(50)
    assert field.property('activeFocus')
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(window, Qt.Key.Key_Backspace)
    assert field.property('text') == ''
    advanced.forceActiveFocus()
    QTest.qWait(50)
    assert field.property('text') == '200', field.property('text')
    assert fixture.writes == [], fixture.writes

    field.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for key in (Qt.Key.Key_3, Qt.Key.Key_7, Qt.Key.Key_5):
        QTest.keyClick(window, key)
    QTest.keyClick(window, Qt.Key.Key_Return)
    QTest.qWait(50)
    assert fixture.writes == [('max_video_mb', 375)], fixture.writes
    fixture.set_error('설정을 저장하지 못했습니다. 설정 파일과 저장 권한을 확인해 주세요.')
    QTest.qWait(50)
    assert error.property('visible') and '저장하지 못했습니다' in error.property('text')
    assert error.height() >= error.property('implicitHeight') - 1
    assert dialog.property('height') <= 448
    fixture.set_error('')
    QTest.qWait(50)
    assert not error.property('visible')
    warnings = [m for m in messages if any(x in m for x in ('.qml', 'QQml', 'ReferenceError', 'TypeError', 'Binding loop'))]
    assert not warnings, warnings
    window.close()
    qInstallMessageHandler(previous)
    return {'blank-restored': True, 'valid-committed': True, 'visible-error': True, 'small-window': True}


@unittest.skipUnless(find_spec('PySide6') is not None, 'Qt dependencies are not installed')
class PreferencesQmlTests(unittest.TestCase):
    def test_settings_error_and_empty_number_focus_loss(self):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software', PYTHONIOENCODING='utf-8')
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--scene'], cwd=ROOT,
                             env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(all(json.loads(run.stdout).values()))


if __name__ == '__main__':
    if '--scene' in sys.argv:
        print(json.dumps(check_scene()))
    else:
        unittest.main()
