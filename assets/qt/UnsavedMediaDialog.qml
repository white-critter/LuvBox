import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "unsavedDialog"
    property var app: null
    readonly property var records: app ? app.records : null
    readonly property bool excludedView: !!records && records.unsavedView === "excluded"
    property bool confirmClear: false
    title: "저장 못 한 미디어"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(680, parent ? Math.max(240, parent.width - 32) : 680)
    height: Math.min(620, parent ? Math.max(260, parent.height - 32) : 620)
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
            text: dialog.excludedView
                ? "저장 확인에서 ‘저장 안 함’으로 직접 제외한 미디어입니다. 실패가 아니며 다시 묻지 않습니다."
                : "저장하지 못한 미디어입니다. 이미 저장했거나 중복이라 건너뛴 미디어는 표시하지 않습니다."
            leftPadding: 20
            rightPadding: 20
            color: "#b9afb1"
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }
    onAboutToShow: {
        confirmClear = false
        if (records)
            records.setActive("unsaved", true)
    }
    onClosed: if (records) records.setActive("unsaved", false)

    Timer {
        id: confirmTimer
        interval: 4000
        onTriggered: dialog.confirmClear = false
    }

    contentItem: ColumnLayout {
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            FlatButton {
                objectName: "unsavedViewFailed"
                Layout.fillWidth: true
                tone: "nav"
                checkable: true
                checked: !dialog.excludedView
                text: "저장하지 못함" + (dialog.records ? " (" + dialog.records.unsavedCount.toLocaleString() + ")" : "")
                onClicked: {
                    dialog.confirmClear = false
                    dialog.records.setUnsavedView("unsaved")
                }
            }
            FlatButton {
                objectName: "unsavedViewExcluded"
                Layout.fillWidth: true
                tone: "nav"
                checkable: true
                checked: dialog.excludedView
                text: "직접 제외" + (dialog.records ? " (" + dialog.records.excludedCount.toLocaleString() + ")" : "")
                onClicked: {
                    dialog.confirmClear = false
                    dialog.records.setUnsavedView("excluded")
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            visible: list.count > 0
            FlatButton {
                objectName: "unsavedRetryAll"
                tone: "primary"
                implicitHeight: 36
                text: dialog.excludedView ? "모두 다시 받기" : "모두 다시 저장"
                enabled: !!dialog.app && dialog.app.canStart && !!dialog.records && dialog.records.retryableCount > 0
                onClicked: dialog.records.retryAll()
            }
            FlatButton {
                objectName: "unsavedClearAll"
                text: dialog.confirmClear ? "한 번 더 누르면 비웁니다" : "목록 비우기"
                onClicked: {
                    if (dialog.confirmClear) {
                        dialog.confirmClear = false
                        confirmTimer.stop()
                        dialog.records.dismissAll()
                    } else {
                        dialog.confirmClear = true
                        confirmTimer.restart()
                    }
                }
            }
            Item { Layout.fillWidth: true }
        }
        Label {
            objectName: "unsavedRetryHint"
            Layout.fillWidth: true
            visible: list.count > 0 && !!dialog.app && !dialog.app.canStart
            text: "다시 저장하려면 X 계정이 연결되어 있고 진행 중인 작업이 끝나야 합니다."
            color: "#e6bd79"
            wrapMode: Text.Wrap
            font.pixelSize: 13
        }
        Label {
            objectName: "unsavedMessage"
            Layout.fillWidth: true
            visible: text.length > 0
            text: dialog.records ? (dialog.records.unsavedError || dialog.records.unsavedStatus) : ""
            color: dialog.records && dialog.records.unsavedError ? "#ff8791" : "#b9afb1"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            font.pixelSize: 13
        }
        ListView {
            id: list
            objectName: "unsavedList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 6
            model: dialog.records ? dialog.records.unsavedRows : []
            ScrollBar.vertical: ScrollBar { policy: list.contentHeight > list.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
            delegate: Rectangle {
                id: card
                required property var modelData
                width: list.width - (list.contentHeight > list.height ? 14 : 0)
                implicitHeight: row.implicitHeight + 24
                radius: 10
                color: "#191919"
                RowLayout {
                    id: row
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 12
                    // The file was never downloaded, so there is nothing to preview; the tile
                    // keeps the row recognisable and the original post stays one click away.
                    Rectangle {
                        objectName: "unsavedPlaceholder"
                        Layout.alignment: Qt.AlignTop
                        Layout.preferredWidth: 72
                        Layout.preferredHeight: 54
                        radius: 8
                        color: "#151515"
                        border.width: 1
                        border.color: "#252525"
                        Canvas {
                            anchors.centerIn: parent
                            width: 32
                            height: 26
                            property string kind: card.modelData.mediaType
                            onKindChanged: requestPaint()
                            onPaint: {
                                const ctx = getContext("2d")
                                ctx.reset()
                                ctx.strokeStyle = "#7a7274"
                                ctx.fillStyle = "#7a7274"
                                ctx.lineWidth = 1.6
                                ctx.lineJoin = "round"
                                ctx.strokeRect(2, 2, 28, 22)
                                if (kind === "photo") {
                                    ctx.beginPath()
                                    ctx.arc(10, 9.5, 2.2, 0, Math.PI * 2)
                                    ctx.stroke()
                                    ctx.beginPath()
                                    ctx.moveTo(5, 21)
                                    ctx.lineTo(13, 13)
                                    ctx.lineTo(18, 18)
                                    ctx.lineTo(23, 14)
                                    ctx.lineTo(27, 21)
                                    ctx.stroke()
                                } else if (kind === "video") {
                                    ctx.beginPath()
                                    ctx.moveTo(13, 8)
                                    ctx.lineTo(13, 18)
                                    ctx.lineTo(21, 13)
                                    ctx.closePath()
                                    ctx.fill()
                                } else {
                                    ctx.font = "bold 10px sans-serif"
                                    ctx.textAlign = "center"
                                    ctx.fillText("GIF", 16, 17)
                                }
                            }
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Label {
                                Layout.fillWidth: true
                                text: card.modelData.author
                                font.bold: true
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                            }
                            Rectangle {
                                implicitWidth: reason.implicitWidth + 16
                                implicitHeight: 22
                                radius: 11
                                color: card.modelData.reason === "excluded" ? "#252525"
                                    : card.modelData.reason === "size" ? "#2d2616" : "#2f1a1c"
                                Label {
                                    id: reason
                                    anchors.centerIn: parent
                                    text: card.modelData.reasonLabel
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: card.modelData.reason === "excluded" ? "#b9afb1"
                                        : card.modelData.reason === "size" ? "#e6bd79" : "#ff8791"
                                }
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: card.modelData.info + (card.modelData.posted ? " · 게시 " + card.modelData.posted : "")
                            textFormat: Text.PlainText
                            color: "#b9afb1"
                            font.pixelSize: 13
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            text: card.modelData.detail
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                            font.pixelSize: 13
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: card.modelData.hint.length > 0
                            text: card.modelData.hint
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                            color: "#e6bd79"
                            font.pixelSize: 12
                        }
                        Label {
                            text: "마지막 확인 " + card.modelData.seen
                            textFormat: Text.PlainText
                            color: "#8f8587"
                            font.pixelSize: 12
                        }
                        Flow {
                            Layout.fillWidth: true
                            Layout.topMargin: 2
                            spacing: 6
                            FlatButton {
                                objectName: "unsavedRetry"
                                implicitHeight: 30
                                text: dialog.excludedView ? "다시 받기" : "다시 저장"
                                enabled: !!dialog.app && dialog.app.canStart && card.modelData.canRetry
                                onClicked: dialog.records.retry(card.modelData.token)
                            }
                            FlatButton {
                                implicitHeight: 30
                                text: "원문 보기"
                                onClicked: dialog.records.openSource(card.modelData.token)
                            }
                            FlatButton {
                                objectName: "unsavedDismiss"
                                implicitHeight: 30
                                text: "목록에서 지우기"
                                onClicked: dialog.records.dismiss(card.modelData.token)
                            }
                        }
                    }
                }
            }
            Label {
                objectName: "unsavedEmpty"
                anchors.centerIn: parent
                visible: list.count === 0 && !!dialog.records && !dialog.records.unsavedBusy && !dialog.records.unsavedError.length
                text: dialog.excludedView ? "직접 제외한 미디어가 없습니다." : "저장하지 못한 미디어가 없습니다."
                color: "#b9afb1"
            }
            BusyIndicator {
                anchors.centerIn: parent
                running: list.count === 0 && !!dialog.records && dialog.records.unsavedBusy
                visible: running
            }
        }
        Label {
            Layout.fillWidth: true
            visible: list.count > 0
            text: "내려받지 못한 파일은 미리볼 수 없습니다. ‘원문 보기’로 게시물을 확인하세요."
            color: "#8f8587"
            font.pixelSize: 12
            wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.fillWidth: true
            visible: !!dialog.records && dialog.records.unsavedPages > 1
            FlatButton {
                text: "이전"
                enabled: dialog.records && !dialog.records.unsavedBusy && dialog.records.unsavedPage > 0
                onClicked: dialog.records.loadUnsaved(dialog.records.unsavedPage - 1)
            }
            Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                text: dialog.records ? (dialog.records.unsavedPage + 1) + " / " + dialog.records.unsavedPages : ""
                color: "#b9afb1"
            }
            FlatButton {
                text: "다음"
                enabled: dialog.records && !dialog.records.unsavedBusy
                    && dialog.records.unsavedPage + 1 < dialog.records.unsavedPages
                onClicked: dialog.records.loadUnsaved(dialog.records.unsavedPage + 1)
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
