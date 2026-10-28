import QtQuick
import QtQuick.Controls

TextField {
    id: control
    implicitHeight: 40
    leftPadding: 14
    rightPadding: 14
    font.pixelSize: 14
    color: control.enabled ? "#f4f1f1" : "#776b6e"
    selectionColor: "#d93645"
    selectedTextColor: "white"
    placeholderTextColor: "#8d8486"
    hoverEnabled: true
    background: Rectangle {
        radius: 10
        color: control.enabled ? "#1c1c1c" : "#161616"
        border.width: control.activeFocus || (control.hovered && control.enabled) ? 1 : 0
        border.color: control.activeFocus ? "#d93645" : "#333333"
    }
}
