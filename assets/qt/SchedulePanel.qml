import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: panel
    objectName: "dailySchedulePanel"
    property var app: null
    readonly property var schedule: app ? app.schedule : null
    property string action: ""
    property string requestedTime: ""
    property bool timeEdited: false
    readonly property bool validTime: /^([01]?[0-9]|2[0-3]):[0-5][0-9]$/.test(timeField.text.trim())
    spacing: 14

    function stamp(value) {
        return value ? String(value).slice(0, 16).replace("T", " ") : "없음"
    }

    function syncTime() {
        if (schedule && !schedule.busy && !schedule.error.length && !timeField.activeFocus) {
            timeField.text = schedule.time || "09:00"
            timeEdited = false
        }
    }

    onVisibleChanged: if (visible && schedule && !schedule.busy) schedule.refresh()
    onAppChanged: if (schedule && !schedule.busy) schedule.refresh()
    Connections {
        target: panel.schedule
        function onChanged() {
            if (!panel.timeEdited)
                panel.syncTime()
        }
    }

    Label { text: "자동 저장"; font.pixelSize: 20; font.bold: true; textFormat: Text.PlainText }
    Label {
        Layout.fillWidth: true
        text: "매일 정한 시각에 현재 저장된 계정과 옵션으로 새 Like를 저장합니다."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "scheduleSummary"
        Layout.fillWidth: true
        text: panel.schedule ? panel.schedule.summary : "예약 정보를 확인하는 중입니다."
        textFormat: Text.PlainText
        font.bold: true
        wrapMode: Text.WordWrap
    }
    Label {
        Layout.fillWidth: true
        text: "대상 계정: " + (panel.app && panel.app.account ? "@" + panel.app.account : "연결 필요")
            + "\n저장 폴더: " + (panel.app && panel.app.outputFolder ? panel.app.outputFolder : "선택 필요")
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
    }
    Label {
        Layout.fillWidth: true
        text: "예약은 하나입니다. 저장할 계정이나 폴더를 바꾸면 다음 자동 저장의 대상도 바뀝니다."
        opacity: 0.72
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
    }
    Label {
        Layout.fillWidth: true
        visible: !!(panel.schedule && panel.schedule.registered)
        text: "다음 실행: " + panel.stamp(panel.schedule ? panel.schedule.nextRun : "")
            + "\n최근 시작: " + panel.stamp(panel.schedule ? panel.schedule.lastRun : "")
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
    }
    Label { text: "매일 실행 시각 (이 PC 시간)"; textFormat: Text.PlainText }
    FlatField {
        id: timeField
        objectName: "scheduleTimeField"
        Layout.preferredWidth: 150
        text: "09:00"
        placeholderText: "09:00"
        maximumLength: 5
        selectByMouse: true
        Accessible.name: "자동 저장 실행 시각"
        enabled: panel.schedule && !panel.schedule.busy && !panel.schedule.locked && !panel.schedule.running
        onTextEdited: panel.timeEdited = true
    }
    Label {
        Layout.fillWidth: true
        text: "24시간 형식으로 입력하세요. 오늘 시각이 지났다면 내일부터 실행합니다."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        opacity: 0.72
    }
    Label {
        objectName: "scheduleTimeHelp"
        Layout.fillWidth: true
        visible: panel.timeEdited && !panel.validTime
        text: "실행 시각은 09:00처럼 24시간 형식으로 입력해 주세요."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: "#ff8791"
    }
    Flow {
        Layout.fillWidth: true
        spacing: 8
        FlatButton {
            objectName: "scheduleSaveButton"
            text: panel.schedule && panel.schedule.registered ? "예약 저장" : "예약 만들기"
            tone: "primary"
            enabled: !!(panel.validTime && panel.app && panel.app.canStart && panel.schedule && panel.schedule.known
                && !panel.schedule.busy && !panel.schedule.locked && !panel.schedule.running)
            onClicked: {
                panel.action = panel.schedule.registered ? "update" : "create"
                panel.requestedTime = timeField.text
                confirmation.open()
            }
        }
        FlatButton {
            objectName: "scheduleMigrateButton"
            visible: !!(panel.schedule && panel.schedule.registered && !panel.schedule.current)
            text: "현재 버전으로 변경"
            enabled: panel.schedule && panel.schedule.enabled && !panel.schedule.busy
                && !panel.schedule.locked && !panel.schedule.running && !!panel.schedule.time
            onClicked: { panel.action = "migrate"; confirmation.open() }
        }
        FlatButton {
            objectName: "scheduleRemoveButton"
            visible: !!(panel.schedule && panel.schedule.registered)
            text: "예약 해제"
            enabled: panel.schedule && !panel.schedule.busy && !panel.schedule.locked
            onClicked: { panel.action = "remove"; confirmation.open() }
        }
        FlatButton {
            objectName: "scheduleRefreshButton"
            text: "다시 확인"
            enabled: panel.schedule && !panel.schedule.busy
            onClicked: panel.schedule.refresh()
        }
    }
    RowLayout {
        visible: !!(panel.schedule && panel.schedule.busy)
        BusyIndicator { running: parent.visible; implicitWidth: 24; implicitHeight: 24 }
        Label { text: panel.schedule ? panel.schedule.status : ""; textFormat: Text.PlainText }
    }
    Label {
        objectName: "scheduleMessage"
        Layout.fillWidth: true
        text: panel.schedule ? panel.schedule.error || (!panel.schedule.busy ? panel.schedule.status : "") : ""
        visible: text.length > 0
        color: panel.schedule && panel.schedule.error.length ? "#ff8791" : "#b9afb1"
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
    }
    Label {
        Layout.fillWidth: true
        visible: !!(panel.schedule && panel.schedule.locked)
        text: "진행 중인 작업이 끝난 뒤 예약을 바꿀 수 있습니다. 예약 조회와 실행 기록 확인은 계속할 수 있습니다."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        opacity: 0.72
    }
    Label {
        Layout.fillWidth: true
        visible: !!(panel.schedule && panel.schedule.registered)
        text: (panel.schedule && panel.schedule.catchUp ? "놓친 예약은 PC를 다시 사용할 때 실행합니다. " : "")
            + (panel.schedule && panel.schedule.onBattery ? "배터리 전원에서도 실행합니다. " : "")
            + "예약 저장은 확인창 없이 진행합니다. 결과는 관리의 실행 기록에서 확인하세요."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        opacity: 0.72
    }

    Dialog {
        id: confirmation
        objectName: "scheduleConfirmation"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(460, parent ? parent.width - 32 : 460)
        implicitHeight: 260
        height: Math.min(implicitHeight, parent ? Math.max(180, parent.height - 32) : implicitHeight)
        padding: 24
        background: Rectangle {
            color: "#141414"
            radius: 16
            border.width: 1
            border.color: "#2a2a2a"
        }
        header: Label {
            text: confirmation.title
            leftPadding: 24
            rightPadding: 24
            topPadding: 20
            bottomPadding: 4
            font.pixelSize: 18
            background: null
        }
        title: panel.action === "remove" ? "자동 저장 예약을 해제할까요?"
            : panel.action === "migrate" ? "현재 버전으로 변경할까요?"
            : panel.action === "create" ? "자동 저장을 예약할까요?" : "예약을 저장할까요?"
        closePolicy: Popup.CloseOnEscape
        contentItem: Label {
            text: panel.action === "remove"
                ? "다음 자동 저장부터 실행하지 않습니다. 이미 시작한 저장과 받은 파일은 유지합니다."
                : panel.action === "migrate"
                ? "기존 실행 시각을 유지하고 현재 버전으로 연결합니다."
                : "매일 " + panel.requestedTime + "에 현재 저장된 계정과 옵션으로 실행합니다. 예약 저장은 현재 버전에 연결하며 사용 중으로 설정합니다."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
        onAccepted: {
            if (!panel.schedule)
                return
            var accepted = panel.action === "remove" ? panel.schedule.remove()
                : panel.action === "migrate" ? panel.schedule.useCurrentVersion()
                : panel.action === "create" ? panel.schedule.create(panel.requestedTime)
                : panel.schedule.changeTime(panel.requestedTime)
            if (accepted)
                panel.timeEdited = false
        }
        footer: DialogButtonBox {
            background: null
            FlatButton {
                text: panel.action === "remove" ? "예약 해제" : "확인"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton { text: "취소"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            onAccepted: confirmation.accept()
            onRejected: confirmation.reject()
        }
    }
}
