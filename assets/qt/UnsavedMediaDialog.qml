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
    readonly property int frameMargin: 24
    readonly property int preferredWidth: 1000
    readonly property int preferredHeight: 740
    property int chosenWidth: preferredWidth
    property int chosenHeight: preferredHeight
    property bool resizing: false
    property int placedX: 0
    property int placedY: 0
    readonly property int limitWidth: parent ? Math.max(0, parent.width - frameMargin * 2) : preferredWidth
    readonly property int limitHeight: parent ? Math.max(0, parent.height - frameMargin * 2) : preferredHeight
    width: Math.min(chosenWidth, limitWidth)
    height: Math.min(chosenHeight, limitHeight)
    x: placedX
    y: placedY
    padding: 20

    function place() {
        if (!parent || resizing)
            return
        placedX = Math.max(0, Math.round((parent.width - width) / 2))
        placedY = Math.max(0, Math.round((parent.height - height) / 2))
    }

    function syncContent() {
        if (!contentItem)
            return
        contentItem.width = availableWidth
        contentItem.height = Math.max(0, height - topPadding - bottomPadding
            - (header ? header.height + spacing : 0)
            - (footer ? footer.height + spacing : 0))
    }

    function applySize(nextWidth, nextHeight) {
        const fittedWidth = Math.max(Math.min(520, limitWidth), Math.min(nextWidth, limitWidth))
        const fittedHeight = Math.max(Math.min(400, limitHeight), Math.min(nextHeight, limitHeight))
        chosenWidth = fittedWidth
        chosenHeight = fittedHeight
        if (!parent)
            return
        placedX = Math.max(0, Math.min(placedX, parent.width - fittedWidth))
        placedY = Math.max(0, Math.min(placedY, parent.height - fittedHeight))
    }

    onWidthChanged: { place(); syncContent() }
    onHeightChanged: { place(); syncContent() }
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
                ? "다시 받지 않도록 빼 둔 미디어입니다. ‘제외 해제’는 다음 저장에서 다시 받게 하고, ‘다시 받기’는 지금 받습니다. 목록을 지우는 동작이 아니며 저장된 파일은 바뀌지 않습니다."
                : "저장하지 못한 미디어입니다. 이미 저장했거나 중복이라 건너뛴 미디어는 표시하지 않습니다. 목록에서 지워도 저장된 파일은 그대로이고, 직접 제외한 항목은 빠지지 않습니다."
            leftPadding: 20
            rightPadding: 20
            color: "#b9afb1"
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }
    onAboutToShow: {
        place()
        syncContent()
        Qt.callLater(syncContent)
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
                    checked = Qt.binding(function() { return !dialog.excludedView })
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
                    checked = Qt.binding(function() { return dialog.excludedView })
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
                objectName: dialog.excludedView ? "unsavedReleaseAll" : "unsavedClearAll"
                text: dialog.excludedView
                    ? (dialog.confirmClear ? "한 번 더 누르면 제외를 해제합니다" : "모두 제외 해제")
                    : (dialog.confirmClear ? "한 번 더 누르면 비웁니다" : "목록 비우기")
                onClicked: {
                    if (dialog.confirmClear) {
                        dialog.confirmClear = false
                        confirmTimer.stop()
                        if (dialog.excludedView)
                            dialog.records.releaseAll()
                        else
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
            spacing: 8
            model: dialog.records ? dialog.records.unsavedRows : []
            ScrollBar.vertical: ScrollBar { policy: list.contentHeight > list.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
            delegate: Rectangle {
                id: card
                required property var modelData
                required property int index
                width: list.width - (list.contentHeight > list.height ? 14 : 0)
                implicitHeight: row.implicitHeight + 24
                radius: 10
                color: "#191919"
                RowLayout {
                    id: row
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 12
                    // A preview is shown only when this PC already kept one (a reviewed
                    // exclusion). Failed and size-limited files are not downloaded again
                    // just to draw this list.
                    Rectangle {
                        objectName: "unsavedPlaceholder"
                        Layout.alignment: Qt.AlignTop
                        Layout.preferredWidth: 112
                        Layout.preferredHeight: 84
                        radius: 8
                        color: "#151515"
                        border.width: 1
                        border.color: "#252525"
                        clip: true
                        Image {
                            id: preview
                            objectName: "unsavedPreview"
                            anchors.fill: parent
                            anchors.margins: 1
                            source: card.modelData.preview || ""
                            sourceSize: Qt.size(224, 168)
                            asynchronous: true
                            cache: false
                            fillMode: Image.PreserveAspectFit
                            visible: status === Image.Ready
                            Accessible.name: "저장하지 못한 미디어 미리보기"
                        }
                        Canvas {
                            visible: !preview.visible
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
                                onActiveFocusChanged: if (activeFocus) list.positionViewAtIndex(card.index, ListView.Contain)
                            }
                            FlatButton {
                                implicitHeight: 30
                                text: "원문 보기"
                                onClicked: dialog.records.openSource(card.modelData.token)
                                onActiveFocusChanged: if (activeFocus) list.positionViewAtIndex(card.index, ListView.Contain)
                            }
                            FlatButton {
                                objectName: dialog.excludedView ? "unsavedRelease" : "unsavedDismiss"
                                implicitHeight: 30
                                text: dialog.excludedView ? "제외 해제" : "목록에서 지우기"
                                onClicked: dialog.excludedView
                                    ? dialog.records.release(card.modelData.token)
                                    : dialog.records.dismiss(card.modelData.token)
                                onActiveFocusChanged: if (activeFocus) list.positionViewAtIndex(card.index, ListView.Contain)
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
            Column {
                anchors.centerIn: parent
                visible: list.count === 0 && !!dialog.records && dialog.records.unsavedBusy && !dialog.records.unsavedError.length
                spacing: 8
                BusyIndicator {
                    anchors.horizontalCenter: parent.horizontalCenter
                    running: parent.visible
                    width: 32
                    height: 32
                }
                Label {
                    objectName: "unsavedLoading"
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "목록을 불러오는 중"
                    color: "#b9afb1"
                }
            }
        }
        Label {
            Layout.fillWidth: true
            visible: list.count > 0 && dialog.height >= 560
            text: "이 PC에 남겨 둔 미리보기가 있으면 보여 줍니다. 미리보기가 없는 항목은 종류, 이유, 원문으로 구분할 수 있습니다."
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
    footer: Item {
        implicitWidth: closeBox.implicitWidth
        implicitHeight: closeBox.implicitHeight
        DialogButtonBox {
            id: closeBox
            anchors.fill: parent
            background: null
            rightPadding: 28
            FlatButton {
                text: "닫기"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onRejected: dialog.close()
        }
        Canvas {
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.margins: 7
            width: 10
            height: 10
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = "#6e6668"
                ctx.lineWidth = 1.4
                ctx.beginPath()
                ctx.moveTo(2, 8)
                ctx.lineTo(8, 2)
                ctx.moveTo(5, 8)
                ctx.lineTo(8, 5)
                ctx.stroke()
            }
        }
        MouseArea {
            objectName: "unsavedResize"
            z: 2
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            width: 22
            height: 22
            cursorShape: Qt.SizeFDiagCursor
            preventStealing: true
            property real originX
            property real originY
            property int originWidth
            property int originHeight
            onPressed: function(mouse) {
                originX = mouse.x
                originY = mouse.y
                originWidth = dialog.width
                originHeight = dialog.height
                dialog.resizing = true
            }
            onPositionChanged: function(mouse) {
                if (!pressed)
                    return
                dialog.applySize(originWidth + mouse.x - originX, originHeight + mouse.y - originY)
            }
            onReleased: dialog.resizing = false
        }
    }
}
