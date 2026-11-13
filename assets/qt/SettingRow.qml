import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One line of a settings list: what it is on the left (with an optional
// explanation under it) and its control on the right.
RowLayout {
    id: row
    property string label: ""
    property string note: ""
    property color noteColor: Theme.textMuted
    property alias noteName: noteLabel.objectName
    default property alias control: slot.data
    spacing: Theme.gap
    Layout.fillWidth: true
    ColumnLayout {
        Layout.fillWidth: true
        spacing: 2
        Label {
            Layout.fillWidth: true
            visible: row.label.length > 0
            text: row.label
            color: row.enabled ? Theme.text : Theme.textDisabled
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        Label {
            id: noteLabel
            Layout.fillWidth: true
            visible: row.note.length > 0
            text: row.note
            font.pixelSize: Theme.fontSmall
            color: row.noteColor
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
    }
    RowLayout {
        id: slot
        spacing: Theme.gapSmall
        Layout.alignment: Qt.AlignVCenter
    }
}
