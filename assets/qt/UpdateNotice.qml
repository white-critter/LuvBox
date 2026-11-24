import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A newer release, shown above the save and gallery screens. Nothing is
// fetched until "업데이트" is pressed; the running version is never replaced,
// so pressing it mid-work is safe and the new version starts next time.
Card {
    id: notice
    objectName: "updateNotice"
    property var app: null
    readonly property var updates: app && app.updates ? app.updates : null
    readonly property string phase: updates ? updates.state : "idle"
    readonly property bool saving: !!app && !!app.download && app.download.running
    readonly property bool busyWork: saving || (!!app && (app.operationBusy
        || (!!app.management && app.management.mutating)
        || (!!app.localManagement && app.localManagement.mutating)))
    property bool notesOpen: false
    signal restartRequested()

    visible: !!updates && updates.noticeVisible
    implicitHeight: visible ? content.implicitHeight + 24 : 0
    border.color: Theme.accentLine
    // News worth a look: this panel carries the red light.
    glow: 0.16
    Accessible.role: Accessible.Grouping
    Accessible.name: "업데이트 알림"
    onPhaseChanged: if (phase !== "available") notesOpen = false

    ColumnLayout {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 12
        anchors.leftMargin: 16
        anchors.rightMargin: 12
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Rectangle {
                Layout.alignment: Qt.AlignVCenter
                implicitWidth: 8
                implicitHeight: 8
                radius: height / 2
                color: notice.phase === "failed" ? Theme.error : Theme.accent
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Label {
                    objectName: "updateNoticeTitle"
                    Layout.fillWidth: true
                    text: {
                        if (!notice.updates)
                            return ""
                        if (notice.phase === "downloading")
                            return notice.updates.status
                        if (notice.phase === "ready")
                            return notice.updates.version + " 준비 완료"
                        if (notice.phase === "failed")
                            return notice.updates.error
                        return "새 버전이 나왔어요 · " + notice.updates.version
                    }
                    color: notice.phase === "failed" ? Theme.error : Theme.text
                    font.pixelSize: Theme.fontSection
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                Label {
                    objectName: "updateNoticeHint"
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: {
                        if (!notice.updates)
                            return ""
                        if (notice.phase === "ready")
                            return notice.saving ? "저장이 끝나면 다시 시작하세요."
                                : (notice.busyWork ? "진행 중인 작업이 끝나면 다시 시작하세요." : "다시 시작하면 적용돼요.")
                        if (notice.phase === "available" && !notice.updates.canInstall)
                            return "다운로드 페이지에서 LuvBox.exe를 받으세요."
                        if (notice.phase === "available")
                            return "지금 버전 " + notice.updates.currentVersion
                        return ""
                    }
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSmall
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
            }
            FlatButton {
                objectName: "updateNotesToggle"
                visible: notice.phase === "available" && !!notice.updates && notice.updates.notes.length > 0
                text: notice.notesOpen ? "변경 사항 접기" : "변경 사항"
                Accessible.name: notice.notesOpen ? "변경 사항 접기" : "변경 사항 보기"
                onClicked: notice.notesOpen = !notice.notesOpen
            }
            FlatButton {
                objectName: "updateCancelButton"
                visible: notice.phase === "downloading"
                text: notice.updates && notice.updates.version ? "받기 취소" : "확인 취소"
                onClicked: notice.updates.cancel()
            }
            FlatButton {
                objectName: "updateReleasePageButton"
                visible: notice.phase === "failed"
                    || (notice.phase === "available" && !!notice.updates && !notice.updates.canInstall)
                text: "다운로드 페이지"
                Accessible.name: "다운로드 페이지 열기"
                onClicked: notice.updates.openReleasePage()
            }
            FlatButton {
                objectName: "updateLaterButton"
                tone: "ghost"
                visible: notice.phase !== "downloading"
                text: notice.phase === "failed" ? "닫기" : "나중에"
                onClicked: notice.updates.dismiss()
            }
            FlatButton {
                objectName: "updateInstallButton"
                visible: (notice.phase === "available" || notice.phase === "failed")
                    && !!notice.updates && (notice.updates.canInstall || notice.updates.retryLatest)
                // Lit, but not the screen's main action: that stays "저장 시작".
                tone: "soft"
                font.bold: true
                text: notice.phase === "failed" ? "다시 시도" : "업데이트"
                Accessible.name: notice.updates && notice.updates.retryLatest ? "새 버전 다시 확인" : "새 버전 받기"
                onClicked: notice.updates.install()
            }
            FlatButton {
                objectName: "updateRestartButton"
                visible: notice.phase === "ready"
                enabled: !notice.busyWork
                tone: "soft"
                font.bold: true
                text: "지금 다시 시작"
                onClicked: notice.restartRequested()
            }
        }

        Item {
            objectName: "updateProgress"
            Layout.fillWidth: true
            implicitHeight: 4
            visible: notice.phase === "downloading" && !!notice.updates && notice.updates.version.length > 0
            Accessible.role: Accessible.ProgressBar
            Accessible.name: "업데이트 받는 중"
            Rectangle {
                anchors.fill: parent
                radius: height / 2
                color: Theme.line
            }
            Rectangle {
                height: parent.height
                radius: height / 2
                color: Theme.accent
                width: parent.width * (notice.updates ? Math.max(0, Math.min(1, notice.updates.progress)) : 0)
                Behavior on width { NumberAnimation { duration: 120 } }
            }
        }

        Flickable {
            id: notesView
            objectName: "updateNotes"
            readonly property bool scrollable: contentHeight > height + 1
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(notesText.implicitHeight, 132)
            visible: notice.notesOpen && notice.phase === "available"
            clip: true
            contentHeight: notesText.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            activeFocusOnTab: scrollable
            Accessible.role: Accessible.Pane
            Accessible.name: qsTr("변경 사항")
            Accessible.focusable: scrollable
            Keys.onPressed: event => {
                if (!notesView.scrollable)
                    return
                let offset = 0
                switch (event.key) {
                case Qt.Key_Up: offset = -32; break
                case Qt.Key_Down: offset = 32; break
                case Qt.Key_PageUp: offset = -notesView.height; break
                case Qt.Key_PageDown: offset = notesView.height; break
                case Qt.Key_Home: offset = -notesView.contentHeight; break
                case Qt.Key_End: offset = notesView.contentHeight; break
                default: return
                }
                notesWheel.reset()
                notesView.cancelFlick()
                notesView.contentY = Math.max(0, Math.min(notesView.contentHeight - notesView.height,
                                                          notesView.contentY + offset))
                event.accepted = true
            }
            ScrollBar.vertical: ScrollBar {
                policy: notesView.scrollable ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }
            SmoothWheel {
                id: notesWheel
                flickable: notesView
            }
            Rectangle {
                parent: notesView
                anchors.fill: parent
                visible: notesView.activeFocus
                z: 1
                color: "transparent"
                radius: Theme.radiusSmall
                border.color: Theme.focus
                border.width: 2
                Accessible.ignored: true
            }
            Label {
                id: notesText
                x: Theme.gapSmall
                width: notesView.width - x - 14
                text: notice.updates ? notice.updates.notes : ""
                color: Theme.textMuted
                font.pixelSize: Theme.fontSmall
                lineHeight: 1.25
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
        }
    }
}
