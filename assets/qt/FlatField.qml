import QtQuick
import QtQuick.Controls

TextField {
    id: control
    implicitHeight: 40
    leftPadding: 14
    rightPadding: 14
    font.pixelSize: 14
    placeholderTextColor: "#8d8486"
    background: Rectangle {
        radius: 10
        color: control.enabled ? "#1c1c1c" : "#161616"
        border.width: control.activeFocus ? 1 : 0
        border.color: "#d93645"
    }
}
