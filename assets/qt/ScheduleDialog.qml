import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "scheduleDialog"
    property var app: null
    title: "자동 저장"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(540, parent ? Math.max(240, parent.width - 32) : 540)
    height: Math.min(560, parent ? Math.max(200, parent.height - 32) : 560)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 20
    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }
    header: Label {
        text: dialog.title
        leftPadding: 20
        rightPadding: 20
        topPadding: 16
        bottomPadding: 2
        font.pixelSize: 18
    }
    contentItem: FocusScrollView {
        id: scroll
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        // Created with the window: the save screen's summary needs the schedule status at start.
        SchedulePanel {
            width: scroll.availableWidth
            app: dialog.app
        }
    }
    footer: DialogButtonBox {
        background: null
        FlatButton {
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }
}
