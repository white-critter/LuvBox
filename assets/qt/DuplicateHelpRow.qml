import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One line of the duplicate view's help card: the badge as it looks on a photo, and what it means.
RowLayout {
    id: row
    property string badge: ""
    property color badgeColor: Theme.lineStrong
    property string text: ""
    Layout.fillWidth: true
    spacing: Theme.gap

    Item {
        // The badges line up in one column, whatever their length.
        Layout.preferredWidth: 64
        Layout.alignment: Qt.AlignTop
        implicitHeight: 22
        Rectangle {
            width: badgeLabel.implicitWidth + 14
            height: 22
            radius: height / 2
            color: row.badgeColor
            border.width: row.badgeColor === Theme.raised ? 1 : 0
            border.color: Theme.lineStrong
            Label {
                id: badgeLabel
                anchors.centerIn: parent
                text: row.badge
                font.pixelSize: Theme.fontTiny
                font.bold: true
                color: Theme.text
            }
        }
    }
    Label {
        Layout.fillWidth: true
        Layout.topMargin: 2
        text: row.text
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        lineHeight: 1.15
        font.pixelSize: Theme.fontSmall
        color: Theme.textMuted
    }
}
