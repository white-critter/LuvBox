import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
    id: dialog
    property var app: null
    objectName: "deleteConfirmation"
    parent: Overlay.overlay
    title: "휴지통으로 이동할까요?"
    preferredWidth: 500
    // As tall as what it says, up to a size where a long list of files scrolls.
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    height: Math.min(Math.max(200, summary.implicitHeight + keepDeleted.implicitHeight + Theme.gap + chromeHeight + 6),
                     420, parent ? Math.max(200, parent.height - 32) : 420)
    onOpened: { keepDeleted.checked = false; cancelButton.forceActiveFocus() }
    onRejected: if (app) app.management.cancelDelete()
    onAccepted: {
        let keep = keepDeleted.checked
        Qt.callLater(function() { if (dialog.app) dialog.app.management.confirmDelete(keep) })
    }
    contentItem: ColumnLayout {
        spacing: Theme.gap
        ScrollView {
            id: summaryScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            SmoothWheel { flickable: summaryScroll.contentItem }
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            Label {
                id: summary
                width: parent.width
                text: dialog.app ? dialog.app.management.deleteSummary : ""
                color: Theme.textMuted
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
        }
        FlatSwitch {
            id: keepDeleted
            Layout.fillWidth: true
            trailing: true
            text: "다음 저장에서 다시 받지 않기"
            Accessible.name: text
        }
    }
    footer: DialogButtons {
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
