import QtQuick
import QtQuick.Controls

MenuItem {
    id: item
    // The key that does the same thing, shown dimmed at the right.
    property string hint: ""
    implicitHeight: 34
    leftPadding: 10
    rightPadding: 12
    font.pixelSize: Theme.fontBody
    indicator: null
    HoverHandler {
        cursorShape: item.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
    contentItem: Item {
        implicitWidth: mark.width + label.implicitWidth + (item.hint.length ? keys.implicitWidth + 24 : 0)
        implicitHeight: label.implicitHeight
        Text {
            id: mark
            width: item.checkable ? 22 : 0
            visible: item.checkable
            anchors.verticalCenter: parent.verticalCenter
            text: item.checked ? "✓" : ""
            font.pixelSize: Theme.fontSmall
            color: Theme.accentText
        }
        Text {
            id: label
            anchors.left: mark.right
            anchors.right: keys.left
            anchors.rightMargin: item.hint.length ? 12 : 0
            anchors.verticalCenter: parent.verticalCenter
            text: item.text
            font: item.font
            color: !item.enabled ? Theme.textDisabled : (item.checked ? Theme.accentText : Theme.text)
            elide: Text.ElideRight
        }
        Text {
            id: keys
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: item.hint
            font.pixelSize: Theme.fontCaption
            color: item.enabled ? Theme.textFaint : Theme.textDisabled
        }
    }
    background: Rectangle {
        radius: Theme.radiusSmall
        color: item.highlighted && item.enabled ? Theme.raised : "transparent"
    }
}
