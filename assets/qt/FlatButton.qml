import QtQuick
import QtQuick.Controls

Button {
    id: control
    property string tone: "quiet"
    flat: true
    implicitHeight: tone === "primary" ? 44 : 36
    leftPadding: 16
    rightPadding: 16
    font.bold: tone === "primary" || (tone === "nav" && checked)
    font.pixelSize: tone === "primary" ? 15 : 14
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
    background: Rectangle {
        radius: 8
        color: {
            if (control.tone === "primary") {
                if (!control.enabled)
                    return Qt.rgba(0.851, 0.212, 0.271, 0.35)
                if (control.down)
                    return "#b42332"
                if (control.hovered)
                    return "#e24b59"
                return control.palette.highlight
            }
            if (control.tone === "nav") {
                if (control.checked || control.down)
                    return "#2a2a2a"
                if (control.hovered && control.enabled)
                    return "#222222"
                return "transparent"
            }
            if (!control.enabled)
                return "#1a1a1a"
            if (control.down)
                return "#1f1f1f"
            if (control.hovered)
                return "#343434"
            return "#2a2a2a"
        }
    }
    contentItem: Text {
        text: control.text
        font: control.font
        color: {
            if (!control.enabled)
                return control.palette.placeholderText
            if (control.tone === "primary")
                return "white"
            if (control.tone === "nav" && !control.checked)
                return "#b9afb1"
            return control.palette.buttonText
        }
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
