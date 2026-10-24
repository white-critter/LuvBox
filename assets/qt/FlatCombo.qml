import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    implicitHeight: 40
    leftPadding: 14
    rightPadding: 28
    font.pixelSize: 14
    hoverEnabled: true

    background: Rectangle {
        radius: 10
        color: control.enabled ? "#1c1c1c" : "#161616"
        border.width: control.visualFocus || control.hovered ? 1 : 0
        border.color: control.visualFocus ? "#d93645" : "#333333"
    }

    contentItem: Text {
        text: control.displayText
        font: control.font
        color: control.enabled ? control.palette.text : control.palette.placeholderText
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: Canvas {
        width: 10
        height: 6
        x: control.width - width - 14
        y: (control.height - height) / 2
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.moveTo(0, 1)
            ctx.lineTo(width, 1)
            ctx.lineTo(width / 2, height)
            ctx.closePath()
            ctx.fillStyle = "#b9afb1"
            ctx.fill()
        }
    }

    delegate: ItemDelegate {
        id: row
        width: ListView.view ? ListView.view.width : control.width
        height: 36
        text: !control.textRole ? modelData
              : (modelData && modelData[control.textRole] !== undefined ? modelData[control.textRole] : modelData)
        highlighted: control.highlightedIndex === index
        font.pixelSize: 14
        contentItem: Text {
            text: row.text
            font: row.font
            color: "#f4f1f1"
            verticalAlignment: Text.AlignVCenter
            leftPadding: 10
            rightPadding: 10
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 8
            color: row.highlighted || row.hovered ? "#2c2c2c" : "transparent"
        }
    }

    popup: Popup {
        y: control.height + 6
        width: control.width
        padding: 4
        implicitHeight: Math.min(280, contentItem.implicitHeight + 8)
        background: Rectangle {
            radius: 10
            color: "#1c1c1c"
            border.width: 1
            border.color: "#2e2e2e"
        }
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
    }
}
