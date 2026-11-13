import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    property int minimumPopupWidth: 0
    // A dim word in front of the choice ("정렬  최근 저장"), for lists whose
    // choices do not say what they are a choice of.
    property string label: ""
    // A filter chip: pill-shaped, and lit while `chosen`.
    property bool chip: false
    property bool chosen: false
    readonly property real labelWidth: label.length ? prefix.implicitWidth + 8 : 0
    implicitHeight: chip ? Theme.controlHeight : Theme.inputHeight
    leftPadding: 14
    rightPadding: 30
    font.pixelSize: Theme.fontBody
    hoverEnabled: true
    // A closed list must keep its choice when the pointer wheel passes over it.
    wheelEnabled: false
    // Measures the widest choice, so the list fits its labels and no more.
    implicitContentWidthPolicy: ComboBox.WidestText
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    background: Rectangle {
        radius: control.chip ? height / 2 : Theme.radiusControl
        color: {
            if (!control.enabled)
                return Theme.fieldDisabled
            if (control.chip && control.chosen)
                return control.hovered ? Theme.accentSoftHover : Theme.accentSoft
            return control.chip && (control.hovered || control.popup.visible) ? Theme.raised : Theme.field
        }
        border.width: control.visualFocus ? 2 : 1
        border.color: {
            if (control.visualFocus)
                return Theme.focus
            if (control.chip && control.chosen)
                return Theme.accentLine
            return control.hovered && control.enabled && !control.chip ? Theme.lineStrong : Theme.line
        }
    }

    contentItem: Item {
        implicitWidth: control.labelWidth + value.implicitWidth
        Text {
            id: prefix
            visible: control.label.length > 0
            anchors.verticalCenter: parent.verticalCenter
            text: control.label
            font: control.font
            color: control.enabled ? Theme.textFaint : Theme.textDisabled
        }
        Text {
            id: value
            anchors.left: parent.left
            anchors.leftMargin: control.labelWidth
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: control.displayText
            font: control.font
            color: {
                if (!control.enabled)
                    return Theme.textDisabled
                return control.chip && control.chosen ? Theme.accentText : Theme.text
            }
            elide: Text.ElideRight
        }
    }

    indicator: Canvas {
        id: canvasIndicator
        width: 10
        height: 6
        x: control.width - width - 14
        y: (control.height - height) / 2
        readonly property color stroke: !control.enabled ? Theme.textDisabled
            : (control.chip && control.chosen ? Theme.accentText : Theme.textMuted)
        onStrokeChanged: requestPaint()
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.moveTo(0, 1)
            ctx.lineTo(width, 1)
            ctx.lineTo(width / 2, height)
            ctx.closePath()
            ctx.fillStyle = stroke
            ctx.fill()
        }
    }

    delegate: ItemDelegate {
        id: row
        required property var model
        required property int index
        width: Math.max(control.popup.availableWidth, implicitWidth)
        height: Theme.controlHeight
        text: {
            const value = model.modelData !== undefined ? model.modelData : model
            if (!control.textRole)
                return value
            return value && value[control.textRole] !== undefined ? value[control.textRole] : value
        }
        highlighted: control.highlightedIndex === index
        font.pixelSize: Theme.fontBody
        leftPadding: 12
        rightPadding: 12
        HoverHandler {
            cursorShape: Qt.PointingHandCursor
        }
        contentItem: Text {
            text: row.text
            font: row.font
            color: control.currentIndex === row.index ? Theme.accentText : Theme.text
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: Theme.radiusSmall
            color: row.highlighted || row.hovered ? Theme.raised : "transparent"
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
            radius: Theme.radiusControl + 2
            color: Theme.field
            border.width: 1
            border.color: Theme.lineStrong
        }
        contentItem: ListView {
            id: popupList
            width: control.popup.availableWidth
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds
            // The bar can be dragged. The wheel is accepted here, including at
            // the first and last row, so it does not fall through to a list behind.
            ScrollBar.vertical: ScrollBar {
                policy: popupList.contentHeight > popupList.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }
            SmoothWheel {
                flickable: popupList
                notchPixels: 108
                passAtEnds: false
            }
            WheelHandler {
                acceptedDevices: PointerDevice.TouchPad
                blocking: true
                onWheel: function(event) {
                    const step = event.pixelDelta.y
                    if (!step)
                        return
                    const minimum = popupList.originY
                    const maximum = minimum + Math.max(0, popupList.contentHeight - popupList.height)
                    popupList.contentY = Math.max(minimum, Math.min(maximum, popupList.contentY + step))
                }
            }
        }
    }
}
