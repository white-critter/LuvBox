import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: panel
    property var app: null
    readonly property var manager: app ? app.management : null
    readonly property var sections: [
        {key: "schedule", label: "자동 저장"},
        {key: "deletions", label: "삭제 이력"}, {key: "statistics", label: "통계"},
        {key: "runs", label: "실행 기록"}
    ]
    spacing: 10

    RowLayout {
        Layout.fillWidth: true
        spacing: 12
        Label { text: "관리"; font.pixelSize: 22; font.bold: true }
        Label {
            Layout.fillWidth: true
            text: panel.app ? ((panel.app.account ? "@" + panel.app.account + " · " : "계정을 선택하세요 · ") + panel.app.outputFolder) : ""
            textFormat: Text.PlainText
            elide: Text.ElideMiddle
            color: "#b9afb1"
        }
    }
    RowLayout {
        Layout.fillWidth: true
        spacing: 6
        Repeater {
            model: panel.sections
            FlatButton {
                required property var modelData
                Layout.fillWidth: true
                text: modelData.label
                tone: "nav"
                checkable: true
                checked: panel.manager && panel.manager.section === modelData.key
                onClicked: if (panel.manager) panel.manager.setSection(modelData.key)
            }
        }
    }
    ScrollView {
        id: scheduleScroll
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: !panel.manager || panel.manager.section === "schedule"
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        SchedulePanel {
            width: scheduleScroll.availableWidth
            app: panel.app
        }
    }
    ColumnLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: panel.manager && panel.manager.section !== "schedule"
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Label {
                objectName: "managementCount"
                Layout.fillWidth: true
                text: panel.manager ? (panel.manager.total.toLocaleString() + "건" +
                     (panel.manager.checkedAt ? " · 확인 " + panel.manager.checkedAt : "")) : ""
                color: "#b9afb1"
                elide: Text.ElideRight
            }
            BusyIndicator { running: panel.manager && panel.manager.busy; visible: running; implicitWidth: 26; implicitHeight: 26 }
            FlatButton { text: "다시 확인"; enabled: panel.manager && !panel.manager.busy && !panel.manager.mutating; onClicked: panel.manager.refresh() }
        }
        FlatSwitch {
            visible: panel.manager && panel.manager.section === "runs"
            text: "새 실행 기록은 모든 계정 보기"
            onToggled: panel.manager.setAllAccounts(checked)
        }
        Label {
            Layout.fillWidth: true
            visible: panel.manager && panel.manager.section === "runs"
            text: "현재 계정의 기존 저장 기록과 최근 1,000회의 수동·자동 실행 결과를 표시합니다. 기록되지 않은 과거 예약 결과는 추정하지 않습니다."
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: "#b9afb1"
        }
        Label {
            Layout.fillWidth: true
            visible: panel.manager && (panel.manager.error.length > 0 || panel.manager.status.length > 0)
            text: panel.manager ? (panel.manager.error || panel.manager.status) : ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: panel.manager && panel.manager.error ? "#ff8791" : "#b9afb1"
        }
        FlatButton {
            visible: panel.manager && panel.manager.mutating && !panel.manager.deleteConfirmationPending
            text: "관리 작업 중단"
            onClicked: panel.manager.stop()
        }
        ScrollView {
            id: statisticsScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: panel.manager && panel.manager.section === "statistics"
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            ColumnLayout {
                width: statisticsScroll.availableWidth
                spacing: 8
                Label {
                    Layout.fillWidth: true
                    readonly property var summary: panel.manager ? panel.manager.statistics.summary || ({}) : ({})
                    text: "현재 파일 " + (summary.files || 0).toLocaleString() + "개 · 게시물 " + (summary.posts || 0).toLocaleString()
                        + "개 · " + ((summary.bytes || 0) / 1048576).toFixed(1) + " MB · 확인 불가 " + (summary.unavailable || 0).toLocaleString() + "개"
                    wrapMode: Text.Wrap
                }
                Label { text: "작성자별 보유 파일 · 게시물"; font.bold: true }
                Repeater {
                    model: panel.manager ? panel.manager.statistics.authors || [] : []
                    Label { required property var modelData; text: "@" + modelData.author + " · " + modelData.files.toLocaleString() + "개 · 게시물 " + modelData.posts.toLocaleString() + "개"; textFormat: Text.PlainText }
                }
                Label { text: "월별 기록"; font.bold: true; Layout.topMargin: 8 }
                Repeater {
                    model: panel.manager ? panel.manager.statistics.months || [] : []
                    Label { required property var modelData; text: modelData.kind + " · " + modelData.month + " · " + modelData.files.toLocaleString() + "개"; textFormat: Text.PlainText }
                }
                Label {
                    Layout.fillWidth: true
                    text: "같은 경로의 중복 기록은 한 번만 집계합니다. 확인된 좋아요는 관측 기록이며 정확한 좋아요 시각을 의미하지 않습니다."
                    wrapMode: Text.Wrap
                    color: "#b9afb1"
                }
            }
        }
        ListView {
            id: records
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: panel.manager && panel.manager.section !== "statistics"
            model: panel.manager ? panel.manager.rows : []
            clip: true
            spacing: 6
            ScrollBar.vertical: ScrollBar { policy: records.contentHeight > records.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
            delegate: Frame {
                id: rowFrame
                required property var modelData
                width: records.width
                padding: 10
                background: Rectangle {
                    color: "#191919"
                    radius: 10
                }
                RowLayout {
                    width: parent.width
                    spacing: 10
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Label { Layout.fillWidth: true; text: modelData.title; textFormat: Text.PlainText; elide: Text.ElideMiddle }
                        Label { Layout.fillWidth: true; text: modelData.detail; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: "#b9afb1" }
                        Label { Layout.fillWidth: true; text: modelData.message; visible: text.length > 0; textFormat: Text.PlainText; wrapMode: Text.Wrap }
                    }
                    FlatButton { text: "로그"; visible: modelData.hasLog; onClicked: panel.manager.openLog(modelData.token) }
                }
            }
            Label { anchors.centerIn: parent; visible: records.count === 0 && panel.manager && !panel.manager.busy && !panel.manager.error.length; text: "표시할 기록이 없습니다."; color: "#b9afb1" }
        }
        RowLayout {
            Layout.fillWidth: true
            visible: panel.manager && panel.manager.section !== "statistics"
            FlatButton { text: "이전"; enabled: panel.manager && !panel.manager.busy && panel.manager.pageIndex > 0; onClicked: panel.manager.goPage(panel.manager.pageIndex - 1) }
            Label { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: panel.manager ? ((panel.manager.pageCount ? panel.manager.pageIndex + 1 : 0) + " / " + panel.manager.pageCount) : "0 / 0" }
            FlatButton { text: "다음"; enabled: panel.manager && !panel.manager.busy && panel.manager.pageIndex + 1 < panel.manager.pageCount; onClicked: panel.manager.goPage(panel.manager.pageIndex + 1) }
        }
    }
}
