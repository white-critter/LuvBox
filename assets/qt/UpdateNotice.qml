import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A newer release, shown above the save and gallery screens. Nothing is
// fetched until "업데이트" is pressed; the running version is never replaced,
// so pressing it mid-work is safe and the new version starts next time.
Rectangle {
    id: notice
    objectName: "updateNotice"
    property var app: null
    readonly property var updates: app && app.updates ? app.updates : null
    readonly property string phase: updates ? updates.state : "idle"
    readonly property bool saving: !!app && !!app.download && app.download.running
    property bool notesOpen: false
    signal restartRequested()

    visible: !!updates && updates.noticeVisible
    implicitHeight: visible ? content.implicitHeight + 24 : 0
    radius: 12
    color: "#191919"
    border.width: 1
    border.color: phase === "failed" ? "#5a2a31" : "#3a2a2d"
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
                radius: 4
                color: notice.phase === "failed" ? "#ff8791" : "#ff4d4f"
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
                            return notice.updates.version + " 받는 중 · " + Math.round(notice.updates.progress * 100) + "%"
                        if (notice.phase === "ready")
                            return notice.updates.version + " 준비 완료 · 다시 시작하면 바뀌어요"
                        if (notice.phase === "failed")
                            return notice.updates.error
                        return "새 버전이 나왔어요 · " + notice.updates.version
                    }
                    color: notice.phase === "failed" ? "#ff8791" : "#f4f1f1"
                    font.pixelSize: 15
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
                            return notice.saving ? "저장이 끝나면 다시 시작할 수 있어요. 그냥 닫아도 다음에는 새 버전으로 열려요."
                                                 : "지금 닫기만 해도 다음에는 새 버전으로 열려요."
                        if (notice.phase === "downloading")
                            return "하던 일은 그대로 계속해도 돼요."
                        if (notice.phase === "available" && !notice.updates.canInstall)
                            return "이 버전은 릴리스 페이지에서 LuvBox.exe를 직접 받아 주세요."
                        if (notice.phase === "available")
                            return "지금 버전 " + notice.updates.currentVersion + " · 설정과 사진, 기록은 그대로예요."
                        return ""
                    }
                    color: "#b9afb1"
                    font.pixelSize: 13
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
            }
            FlatButton {
                objectName: "updateNotesToggle"
                visible: notice.phase === "available" && !!notice.updates && notice.updates.notes.length > 0
                text: notice.notesOpen ? "변경 사항 접기" : "변경 사항"
                Accessible.name: "변경 사항 보기"
                onClicked: notice.notesOpen = !notice.notesOpen
            }
            FlatButton {
                objectName: "updateCancelButton"
                visible: notice.phase === "downloading"
                text: "받기 취소"
                onClicked: notice.updates.cancel()
            }
            FlatButton {
                objectName: "updateReleasePageButton"
                visible: notice.phase === "failed"
                    || (notice.phase === "available" && !!notice.updates && !notice.updates.canInstall)
                text: "릴리스 페이지"
                Accessible.name: "릴리스 페이지 열기"
                onClicked: notice.updates.openReleasePage()
            }
            FlatButton {
                objectName: "updateLaterButton"
                visible: notice.phase !== "downloading"
                text: notice.phase === "failed" ? "닫기" : "나중에"
                onClicked: notice.updates.dismiss()
            }
            FlatButton {
                objectName: "updateInstallButton"
                visible: (notice.phase === "available" || notice.phase === "failed")
                    && !!notice.updates && notice.updates.canInstall
                tone: "primary"
                implicitHeight: 36
                font.pixelSize: 14
                text: notice.phase === "failed" ? "다시 시도" : "업데이트"
                Accessible.name: "새 버전 받기"
                onClicked: notice.updates.install()
            }
            FlatButton {
                objectName: "updateRestartButton"
                visible: notice.phase === "ready"
                enabled: !notice.saving
                tone: "primary"
                implicitHeight: 36
                font.pixelSize: 14
                text: "지금 다시 시작"
                onClicked: notice.restartRequested()
            }
        }

        Item {
            objectName: "updateProgress"
            Layout.fillWidth: true
            implicitHeight: 4
            visible: notice.phase === "downloading"
            Accessible.role: Accessible.ProgressBar
            Accessible.name: "업데이트 받는 중"
            Rectangle {
                anchors.fill: parent
                radius: height / 2
                color: "#2a2a2a"
            }
            Rectangle {
                height: parent.height
                radius: height / 2
                color: "#d93645"
                width: parent.width * (notice.updates ? Math.max(0, Math.min(1, notice.updates.progress)) : 0)
                Behavior on width { NumberAnimation { duration: 120 } }
            }
        }

        Flickable {
            id: notesView
            objectName: "updateNotes"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(notesText.implicitHeight, 132)
            visible: notice.notesOpen && notice.phase === "available"
            clip: true
            contentHeight: notesText.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {
                policy: notesView.contentHeight > notesView.height + 1 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }
            SmoothWheel { flickable: notesView }
            Label {
                id: notesText
                width: notesView.width - 14
                text: notice.updates ? notice.updates.notes : ""
                color: "#d9d2d3"
                font.pixelSize: 13
                lineHeight: 1.25
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
        }
    }
}
