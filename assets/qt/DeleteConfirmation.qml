import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
    id: dialog
    property var app: null
    // The gallery's by default; the 폴더 정리 tab gives its own, and a folder has no saves to skip.
    property var library: app ? app.library : null
    property var manager: app ? app.management : null
    property bool showKeepDeleted: true
    objectName: "deleteConfirmation"
    parent: Overlay.overlay
    title: "휴지통으로 이동할까요?"
    preferredWidth: 500
    // As tall as what it says, up to a size where a long list of files scrolls.
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    height: Math.min(Math.max(200, summary.implicitHeight + (keepDeleted.visible ? keepDeleted.implicitHeight + Theme.gap : 0)
                              + chromeHeight + 6),
                     420, parent ? Math.max(200, parent.height - 32) : 420)
    // From the duplicate view the switch starts on: a copy fetched again at the next full save
    // would be the same duplicate again. From the gallery it starts off, as before.
    onOpened: {
        keepDeleted.checked = dialog.showKeepDeleted && !!library && library.filterDuplicateOnly
        cancelButton.forceActiveFocus()
    }
    onRejected: if (manager) manager.cancelDelete()
    onAccepted: {
        let keep = dialog.showKeepDeleted && keepDeleted.checked
        Qt.callLater(function() { if (dialog.manager) dialog.manager.confirmDelete(keep) })
    }
    contentItem: ColumnLayout {
        spacing: Theme.gap
        ScrollView {
            id: summaryScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            SmoothWheel { flickable: summaryScroll.contentItem as Flickable }
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            Label {
                id: summary
                objectName: "deleteSummaryText"
                // The width of the view, not of the content: a long line then wraps
                // instead of running out of the dialog.
                width: summaryScroll.availableWidth
                text: dialog.manager ? dialog.manager.deleteSummary : ""
                color: Theme.textMuted
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
        }
        FlatSwitch {
            id: keepDeleted
            objectName: "deleteKeepSwitch"
            visible: dialog.showKeepDeleted
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
    }
}
