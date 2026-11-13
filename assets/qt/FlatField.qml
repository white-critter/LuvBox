import QtQuick
import QtQuick.Controls

TextField {
    id: control
    implicitHeight: Theme.inputHeight
    leftPadding: 14
    rightPadding: 14
    font.pixelSize: Theme.fontBody
    color: control.enabled ? Theme.text : Theme.textDisabled
    selectionColor: Theme.accent
    selectedTextColor: "white"
    placeholderTextColor: Theme.textFaint
    hoverEnabled: true
    background: Rectangle {
        radius: Theme.radiusControl
        color: control.enabled ? Theme.field : Theme.fieldDisabled
        border.width: 1
        border.color: control.activeFocus ? Theme.accent
            : (control.hovered && control.enabled ? Theme.lineStrong : Theme.line)
    }
}
