import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    property var app: null
    objectName: "deleteConfirmation"
    parent: Overlay.overlay
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    title: "휴지통으로 이동할까요?"
    width: Math.min(500, parent ? Math.max(240, parent.width - 32) : 500)
    height: Math.min(360, parent ? Math.max(200, parent.height - 32) : 360)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 24
    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }
    header: Label {
        text: dialog.title
        leftPadding: 24
        rightPadding: 24
        topPadding: 20
        bottomPadding: 4
        font.pixelSize: 18
        background: null
    }
    onOpened: { keepDeleted.checked = false; cancelButton.forceActiveFocus() }
    onRejected: if (app) app.management.cancelDelete()
    onAccepted: {
        let keep = keepDeleted.checked
        Qt.callLater(function() { if (dialog.app) dialog.app.management.confirmDelete(keep) })
    }
    contentItem: ColumnLayout {
        spacing: 16
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            Label {
                width: parent.width
                text: dialog.app ? dialog.app.management.deleteSummary : ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
        }
        FlatSwitch {
            id: keepDeleted
            Layout.fillWidth: true
            text: "다음 저장에서 다시 받지 않기"
            Accessible.name: text
        }
    }
    footer: DialogButtonBox {
        background: null
        FlatButton {
            objectName: "confirmRecycle"
            text: "휴지통으로 이동"
            tone: "primary"
            DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
        }
        FlatButton {
            id: cancelButton
            text: "취소"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onAccepted: dialog.accept()
        onRejected: dialog.reject()
    }
}
