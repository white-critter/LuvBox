import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: panel
    property var app: null
    readonly property var manager: app ? app.management : null
    readonly property int selectableCount: {
        if (!manager || manager.section !== "missing") return 0
        var count = 0
        var rows = manager.rows
        for (var i = 0; i < rows.length; ++i)
            if (rows[i].selectable) ++count
        return count
    }
    readonly property bool pageSelected: selectableCount > 0 && manager.selectedCount === selectableCount
    readonly property var sections: [
        {key: "schedule", label: "자동 저장"}, {key: "missing", label: "사라진 파일"},
        {key: "deletions", label: "삭제 이력"}, {key: "statistics", label: "통계"},
        {key: "runs", label: "실행 기록"}
    ]
    spacing: 12

    Label { text: "관리"; font.pixelSize: 24 }
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
    Label {
        Layout.fillWidth: true
        text: panel.app ? ((panel.app.account ? "@" + panel.app.account + " · " : "계정을 선택하세요 · ") + panel.app.outputFolder) : ""
        textFormat: Text.PlainText
        elide: Text.ElideMiddle
        color: "#b9afb1"
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
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Label {
                objectName: "managementCount"
                Layout.fillWidth: true
                text: panel.manager ? (panel.manager.total.toLocaleString() + "건" +
                     (panel.manager.section === "missing" && panel.manager.selectedCount > 0
                         ? " · 선택 " + panel.manager.selectedCount.toLocaleString() + "건" : "") +
                     (panel.manager.checkedAt ? " · 확인 " + panel.manager.checkedAt : "")) : ""
                color: "#b9afb1"
                elide: Text.ElideRight
            }
            BusyIndicator { running: panel.manager && panel.manager.busy; visible: running; implicitWidth: 26; implicitHeight: 26 }
            FlatButton { text: "다시 확인"; enabled: panel.manager && !panel.manager.busy && !panel.manager.mutating; onClicked: panel.manager.refresh() }
        }
        Flow {
            Layout.fillWidth: true
            spacing: 8
            visible: panel.manager && panel.manager.section === "missing"
            FlatCombo {
                objectName: "missingModeFilter"
                implicitWidth: 152
                model: ["전체", "다시 받을 파일", "다시 받지 않기", "확인 불가"]
                Accessible.name: "사라진 파일 상태"
                onActivated: panel.manager.setMissingMode(["all", "missing", "ignored", "unavailable"][currentIndex])
            }
            FlatButton {
                objectName: "missingSelectPage"
                text: panel.pageSelected ? "선택 해제" : "현재 목록 선택"
                enabled: panel.manager && !panel.manager.busy && !panel.manager.mutating && panel.selectableCount > 0
                onClicked: panel.pageSelected ? panel.manager.clearSelection() : panel.manager.selectPage()
            }
            FlatButton {
                text: "선택 다시 받기"
                enabled: panel.app && panel.app.canStart && panel.app.accounts &&
                    panel.app.accounts.authenticationState !== "reconnect_required" &&
                    panel.manager && !panel.manager.locked && !panel.manager.mutating && panel.manager.selectedCount > 0
                onClicked: panel.manager.retrySelected()
            }
            FlatButton {
                text: "다시 받지 않기"
                enabled: panel.manager && !panel.manager.locked && !panel.manager.mutating && panel.manager.selectedCount > 0
                onClicked: panel.manager.setIgnored(true)
            }
            FlatButton {
                text: "제외 해제"
                enabled: panel.manager && !panel.manager.locked && !panel.manager.mutating && panel.manager.selectedCount > 0
                onClicked: panel.manager.setIgnored(false)
            }
        }
        Label {
            id: retryHelp
            objectName: "retryConnectionHelp"
            Layout.fillWidth: true
            visible: !!(panel.manager && panel.manager.section === "missing" && panel.manager.selectedCount > 0 &&
                panel.app && !panel.app.busy && (!panel.app.canStart ||
                (panel.app.accounts && panel.app.accounts.authenticationState === "reconnect_required")))
            text: "다시 받으려면 X 계정 연결과 저장 위치를 확인해 주세요."
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: "#e6bd79"
        }
        FlatSwitch {
            visible: panel.manager && panel.manager.section === "runs"
            text: "새 실행 기록은 모든 계정 보기"
            onToggled: panel.manager.setAllAccounts(checked)
        }
        Label {
            Layout.fillWidth: true
            visible: panel.manager && panel.manager.section === "missing" && !retryHelp.visible
            text: "확인 불가는 드라이브·권한·클라우드 상태를 먼저 확인하세요. 선택 작업은 파일 없음이 확인된 항목에 적용합니다."
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: "#b9afb1"
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
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: panel.manager && panel.manager.section === "statistics"
            clip: true
            ColumnLayout {
                width: parent.width
                spacing: 10
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
                Label { text: "월별 기록"; font.bold: true; Layout.topMargin: 12 }
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
                required property var modelData
                width: records.width
                padding: 12
                background: Rectangle { color: "#191919"; radius: 10 }
                RowLayout {
                    width: parent.width
                    CheckBox {
                        visible: panel.manager && panel.manager.section === "missing"
                        enabled: modelData.selectable && !panel.manager.busy && !panel.manager.mutating
                        checked: panel.manager && panel.manager.selectedKeys.indexOf(modelData.token) >= 0
                        Accessible.name: modelData.title + " 선택"
                        onClicked: panel.manager.toggleRow(modelData.token)
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: modelData.title; textFormat: Text.PlainText; elide: Text.ElideMiddle }
                        Label { Layout.fillWidth: true; text: modelData.detail; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: "#b9afb1" }
                        Label { Layout.fillWidth: true; text: modelData.message; visible: text.length > 0; textFormat: Text.PlainText; wrapMode: Text.Wrap }
                    }
                    FlatButton { text: "로그"; visible: modelData.hasLog; onClicked: panel.manager.openLog(modelData.token) }
                }
            }
            Label { anchors.centerIn: parent; visible: records.count === 0 && panel.manager && !panel.manager.busy && !panel.manager.error.length; text: "표시할 기록이 없습니다."; color: "#b9afb1" }
        }
        RowLayout {
            Layout.fillWidth: true
            FlatButton { text: "이전"; enabled: panel.manager && !panel.manager.busy && panel.manager.pageIndex > 0; onClicked: panel.manager.goPage(panel.manager.pageIndex - 1) }
            Label { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: panel.manager ? ((panel.manager.pageCount ? panel.manager.pageIndex + 1 : 0) + " / " + panel.manager.pageCount) : "0 / 0" }
            FlatButton { text: "다음"; enabled: panel.manager && !panel.manager.busy && panel.manager.pageIndex + 1 < panel.manager.pageCount; onClicked: panel.manager.goPage(panel.manager.pageIndex + 1) }
        }
    }
}
