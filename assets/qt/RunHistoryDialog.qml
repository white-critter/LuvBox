import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "runHistoryDialog"
    property var app: null
    readonly property var records: app ? app.records : null
    title: "실행 기록"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(640, parent ? Math.max(240, parent.width - 32) : 640)
    height: Math.min(560, parent ? Math.max(240, parent.height - 32) : 560)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 20
    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }
    header: Column {
        width: dialog.width
        Label {
            width: parent.width
            text: dialog.title
            leftPadding: 20
            rightPadding: 20
            topPadding: 16
            bottomPadding: 2
            font.pixelSize: 18
        }
        Label {
            width: parent.width
            text: "수동·자동 저장 결과를 최근 1,000건까지 보관합니다."
            leftPadding: 20
            rightPadding: 20
            color: "#b9afb1"
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }
    onAboutToShow: if (records) records.setActive("runs", true)
    onClosed: if (records) records.setActive("runs", false)

    readonly property var tones: ({
        ok: { text: "#8fd3a0", fill: "#1c2a21" }, warn: { text: "#e6bd79", fill: "#2d2616" },
        error: { text: "#ff8791", fill: "#2f1a1c" }, muted: { text: "#b9afb1", fill: "#252525" }
    })

    contentItem: ColumnLayout {
        spacing: 8
        Label {
            objectName: "runHistoryError"
            Layout.fillWidth: true
            visible: text.length > 0
            text: dialog.records ? dialog.records.runError : ""
            color: "#ff8791"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        Label {
            Layout.fillWidth: true
            visible: text.length > 0
            text: dialog.records ? dialog.records.runNote : ""
            color: "#b9afb1"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        ListView {
            id: list
            objectName: "runHistoryList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 6
            model: dialog.records ? dialog.records.runRows : []
            ScrollBar.vertical: ScrollBar { policy: list.contentHeight > list.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
            delegate: Rectangle {
                id: card
                required property var modelData
                readonly property var tone: dialog.tones[modelData.tone] || dialog.tones.muted
                width: list.width - (list.contentHeight > list.height ? 14 : 0)
                implicitHeight: body.implicitHeight + 24
                radius: 10
                color: "#191919"
                ColumnLayout {
                    id: body
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 6
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Rectangle {
                            implicitWidth: chip.implicitWidth + 16
                            implicitHeight: 22
                            radius: 11
                            color: card.tone.fill
                            Label {
                                id: chip
                                anchors.centerIn: parent
                                text: card.modelData.status
                                color: card.tone.text
                                font.pixelSize: 12
                                font.bold: true
                            }
                        }
                        Label {
                            text: card.modelData.when + (card.modelData.origin ? " · " + card.modelData.origin : "")
                            color: "#b9afb1"
                            textFormat: Text.PlainText
                        }
                        Item { Layout.fillWidth: true }
                        Label {
                            Layout.maximumWidth: card.width * 0.3
                            text: card.modelData.account
                            color: "#b9afb1"
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                        }
                        FlatButton {
                            visible: card.modelData.hasLog
                            implicitHeight: 28
                            text: "로그"
                            Accessible.name: "실행 로그 열기"
                            onClicked: dialog.records.openLog(card.modelData.token)
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: card.modelData.summary
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: card.modelData.message
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: card.modelData.tone === "error" ? "#ff8791" : "#b9afb1"
                        font.pixelSize: 13
                    }
                }
            }
            Label {
                objectName: "runHistoryEmpty"
                anchors.centerIn: parent
                visible: list.count === 0 && dialog.records && !dialog.records.runBusy && !dialog.records.runError.length
                text: "아직 실행 기록이 없습니다."
                color: "#b9afb1"
            }
            BusyIndicator {
                anchors.centerIn: parent
                running: list.count === 0 && dialog.records && dialog.records.runBusy
                visible: running
            }
        }
        RowLayout {
            Layout.fillWidth: true
            visible: !!dialog.records && dialog.records.runPages > 1
            FlatButton {
                text: "이전"
                enabled: dialog.records && !dialog.records.runBusy && dialog.records.runPage > 0
                onClicked: dialog.records.loadRuns(dialog.records.runPage - 1)
            }
            Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                text: dialog.records ? (dialog.records.runPage + 1) + " / " + dialog.records.runPages : ""
                color: "#b9afb1"
            }
            FlatButton {
                text: "다음"
                enabled: dialog.records && !dialog.records.runBusy && dialog.records.runPage + 1 < dialog.records.runPages
                onClicked: dialog.records.loadRuns(dialog.records.runPage + 1)
            }
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
