import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    property int minimumPopupWidth: 0
    implicitHeight: 40
    leftPadding: 14
    rightPadding: 28
    font.pixelSize: 14
    hoverEnabled: true
    // Measures the widest choice, so the list fits its labels and no more.
    implicitContentWidthPolicy: ComboBox.WidestText
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    background: Rectangle {
        radius: 10
        color: control.enabled ? "#1c1c1c" : "#161616"
        border.width: control.visualFocus ? 2 : (control.hovered && control.enabled ? 1 : 0)
        border.color: control.visualFocus ? "#ff8791" : "#333333"
    }

    contentItem: Text {
        text: control.displayText
        font: control.font
        color: control.enabled ? control.palette.text : control.palette.placeholderText
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: Canvas {
        id: canvasIndicator
        width: 10
        height: 6
        x: control.width - width - 14
        y: (control.height - height) / 2
        Connections {
            target: control
            function onEnabledChanged() { canvasIndicator.requestPaint() }
        }
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.moveTo(0, 1)
            ctx.lineTo(width, 1)
            ctx.lineTo(width / 2, height)
            ctx.closePath()
            ctx.fillStyle = control.enabled ? "#b9afb1" : "#555555"
            ctx.fill()
        }
    }

    delegate: ItemDelegate {
        id: row
        required property var model
        required property int index
        width: Math.max(control.popup.availableWidth, implicitWidth)
        height: 36
        text: {
            const value = model.modelData !== undefined ? model.modelData : model
            if (!control.textRole)
                return value
            return value && value[control.textRole] !== undefined ? value[control.textRole] : value
        }
        highlighted: control.highlightedIndex === index
        font.pixelSize: 14
        leftPadding: 12
        rightPadding: 12
        HoverHandler {
            cursorShape: Qt.PointingHandCursor
        }
        contentItem: Text {
            text: row.text
            font: row.font
            color: "#f4f1f1"
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 8
            color: row.highlighted || row.hovered ? "#2c2c2c" : "transparent"
        }
    }

    popup: Popup {
        y: control.height + 6
        width: Math.max(control.width, control.minimumPopupWidth, control.implicitContentWidth + 32)
        // Stay inside the window, e.g. for a combo at its right edge.
        margins: 8
        padding: 4
        implicitHeight: Math.min(280, contentItem.implicitHeight + 8)
        background: Rectangle {
            radius: 10
            color: "#1c1c1c"
            border.width: 1
            border.color: "#2e2e2e"
        }
        contentItem: ListView {
            width: control.popup.availableWidth
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
    }
}
