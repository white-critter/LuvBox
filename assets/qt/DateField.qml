import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A date shown as text that opens a calendar. It only displays the applied
// value; the owner stores a chosen or cleared date and passes it back in `text`.
Rectangle {
    id: field
    property string text: ""
    property string placeholder: "날짜"
    property string accessibleLabel: "날짜"
    property bool pickerOpen: false
    signal pickRequested()
    signal cleared()
    readonly property bool highlighted: activeFocus || pickerOpen
    activeFocusOnTab: true
    implicitHeight: 40
    radius: 10
    color: "#1c1c1c"
    border.width: highlighted ? 2 : (mouse.containsMouse ? 1 : 0)
    border.color: highlighted ? "#ff8791" : "#333333"
    Accessible.role: Accessible.Button
    Accessible.name: text.length > 0 ? accessibleLabel + " " + text : accessibleLabel + " 선택"
    Accessible.onPressAction: pickRequested()
    Keys.onReturnPressed: pickRequested()
    Keys.onEnterPressed: pickRequested()
    Keys.onSpacePressed: pickRequested()
    Keys.onDeletePressed: if (text.length > 0) cleared()
    Keys.onPressed: event => {
        if (event.key === Qt.Key_Backspace && field.text.length > 0) {
            field.cleared()
            event.accepted = true
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 8
        spacing: 6
        Canvas {
            width: 14
            height: 14
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = "#b9afb1"
                ctx.lineWidth = 1.4
                ctx.strokeRect(1, 2.5, 12, 10.5)
                ctx.beginPath()
                ctx.moveTo(1, 6)
                ctx.lineTo(13, 6)
                ctx.moveTo(4, 1)
                ctx.lineTo(4, 3.5)
                ctx.moveTo(10, 1)
                ctx.lineTo(10, 3.5)
                ctx.stroke()
            }
        }
        Label {
            Layout.fillWidth: true
            text: field.text.length > 0 ? field.text : field.placeholder
            color: field.text.length > 0 ? "#f4f1f1" : "#8f8587"
            font.pixelSize: 13
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
        }
        Label {
            visible: field.text.length > 0
            text: "×"
            color: "#b9afb1"
            font.pixelSize: 14
        }
    }
    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: event => {
            field.forceActiveFocus()
            if (field.text.length > 0 && event.x >= field.width - 26)
                field.cleared()
            else
                field.pickRequested()
        }
    }
}
