import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
    id: dialog
    objectName: "scheduleDialog"
    property var app: null
    title: "자동 저장"
    preferredWidth: 540
    // As tall as the panel needs, inside the window.
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    height: Math.min(Math.max(240, panel.implicitHeight + chromeHeight + 6),
                     parent ? Math.max(200, parent.height - 32) : 560)
    contentItem: FocusScrollView {
        id: scroll
        clip: true
        contentWidth: availableWidth
        rightPadding: ScrollBar.vertical.policy === ScrollBar.AlwaysOn ? 14 : 0
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        // Created with the window: the save screen's summary needs the schedule status at start.
        SchedulePanel {
            id: panel
            width: scroll.availableWidth
            app: dialog.app
        }
    }
    footer: DialogButtons {
        FlatButton {
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }
}
