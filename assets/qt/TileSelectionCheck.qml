pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

CheckBox {
    id: control
    required property bool checkedState
    property string captionText: ""
    signal selectionToggled()

    padding: 6
    focusPolicy: Qt.NoFocus
    checked: control.checkedState
    Accessible.name: "사진 선택: " + control.captionText
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
    indicator: Rectangle {
        implicitWidth: 22
        implicitHeight: 22
        x: control.leftPadding
        y: control.topPadding
        radius: Theme.radiusSmall
        color: control.checked ? Theme.accent : Theme.scrim
        border.width: control.visualFocus ? 2 : (control.checked ? 0 : 1)
        border.color: control.visualFocus ? Theme.focus
            : (control.enabled ? Theme.textMuted : Theme.textDisabled)
        Text {
            anchors.centerIn: parent
            visible: control.checked
            text: "✓"
            font.pixelSize: Theme.fontBody
            font.bold: true
            color: "white"
        }
    }
    onClicked: {
        control.selectionToggled()
        checked = Qt.binding(function() { return control.checkedState })
    }
}
