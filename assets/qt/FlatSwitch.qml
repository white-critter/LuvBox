import QtQuick
import QtQuick.Controls

Switch {
    id: control
    implicitHeight: 36
    spacing: 12
    font.pixelSize: 14
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    indicator: Rectangle {
        implicitWidth: 44
        implicitHeight: 26
        x: control.leftPadding
        y: (control.height - height) / 2
        radius: 13
        color: {
            if (!control.enabled)
                return control.checked ? Qt.rgba(0.851, 0.212, 0.271, 0.35) : "#222222"
            return control.checked ? "#d93645" : "#2a2a2a"
        }
        Rectangle {
            width: 20
            height: 20
            radius: 10
            y: 3
            x: control.checked ? parent.width - width - 3 : 3
            color: control.enabled ? "#f7f4f4" : "#8a8385"
            Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        }
    }

    contentItem: Text {
        text: control.text
        font: control.font
        color: control.enabled ? control.palette.text : control.palette.placeholderText
        leftPadding: control.indicator.width + control.spacing
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
