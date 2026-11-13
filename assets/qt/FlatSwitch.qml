import QtQuick
import QtQuick.Controls

Switch {
    id: control
    // A settings row: the text on the left and the switch at the right edge.
    property bool trailing: false
    implicitHeight: Theme.controlHeight
    // In a row the text lines up with the labels above and below it.
    leftPadding: trailing ? 0 : 6
    rightPadding: trailing ? 0 : 6
    spacing: 12
    font.pixelSize: Theme.fontBody
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    indicator: Rectangle {
        implicitWidth: 40
        implicitHeight: 24
        x: control.trailing ? control.width - width - control.rightPadding : control.leftPadding
        y: (control.height - height) / 2
        radius: height / 2
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.focus
        color: {
            if (!control.enabled)
                return control.checked ? Theme.accentDisabled : Theme.hover
            return control.checked ? Theme.accent : Theme.lineStrong
        }
        Rectangle {
            width: 18
            height: 18
            radius: height / 2
            y: 3
            x: control.checked ? parent.width - width - 3 : 3
            color: control.enabled ? "white" : Theme.textFaint
            Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        }
    }

    contentItem: Text {
        text: control.text
        font: control.font
        color: control.enabled ? Theme.text : Theme.textDisabled
        leftPadding: control.trailing ? 0 : control.indicator.width + control.spacing
        rightPadding: control.trailing ? control.indicator.width + control.spacing : 0
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
