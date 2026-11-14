import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Media that is not in the gallery, looked at the way the gallery is: what a save could not
// store, what the person excluded, and saved files that are gone. Tiles are picked by a
// click and the buttons above act on the picked ones, or on the whole list when none is.
Item {
    id: review
    property var app: null
    property int tileWidth: 168
    signal backRequested()

    readonly property var records: app ? app.records : null
    readonly property string view: records ? records.unsavedView : "unsaved"
    readonly property int chosen: records ? records.unsavedSelectedCount : 0
    readonly property var chosenTokens: records ? records.unsavedSelection : []
    // Acting on a whole list asks once more; it lapses after a few seconds.
    property bool confirmAll: false
    readonly property string retryWord: view === "unsaved" ? "다시 저장" : "다시 받기"
    readonly property string settleWord: view === "excluded" ? "제외 해제"
        : view === "missing" ? "갤러리에서 빼기" : "목록에서 지우기"

    function show(name) {
        confirmAll = false
        if (records)
            records.setUnsavedView(name)
    }

    onViewChanged: confirmAll = false
    onChosenChanged: confirmAll = false

    Timer {
        id: confirmTimer
        interval: 4000
        onTriggered: review.confirmAll = false
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.gap

        Card {
            objectName: "unsavedHeader"
            Layout.fillWidth: true
            implicitHeight: header.implicitHeight + 2 * Theme.gap
            ColumnLayout {
                id: header
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Theme.gapLarge
                anchors.rightMargin: Theme.gap
                spacing: Theme.gapSmall

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.gap
                    FlatButton {
                        objectName: "unsavedBack"
                        tone: "ghost"
                        compact: true
                        text: "← 갤러리"
                        tip: "갤러리로 돌아가기 (Esc)"
                        Accessible.name: "갤러리로 돌아가기"
                        onClicked: review.backRequested()
                    }
                    Label {
                        objectName: "unsavedTitle"
                        text: "저장 못 한 미디어"
                        font.bold: true
                        font.pixelSize: Theme.fontSection
                        color: Theme.text
                    }
                    Item { Layout.fillWidth: true }
                }
                // The three lists share the width, so they fit a narrow window too.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.gapSmall
                    FlatButton {
                        objectName: "unsavedViewFailed"
                        Layout.fillWidth: true
                        tone: "nav"
                        checkable: true
                        checked: review.view === "unsaved"
                        text: "저장하지 못함" + (review.records ? " (" + review.records.unsavedCount.toLocaleString() + ")" : "")
                        onClicked: {
                            review.show("unsaved")
                            checked = Qt.binding(function() { return review.view === "unsaved" })
                        }
                    }
                    FlatButton {
                        objectName: "unsavedViewMissing"
                        Layout.fillWidth: true
                        tone: "nav"
                        checkable: true
                        checked: review.view === "missing"
                        text: "없어진 파일" + (review.records ? " (" + review.records.missingCount.toLocaleString() + ")" : "")
                        onClicked: {
                            review.show("missing")
                            checked = Qt.binding(function() { return review.view === "missing" })
                        }
                    }
                    FlatButton {
                        objectName: "unsavedViewExcluded"
                        Layout.fillWidth: true
                        tone: "nav"
                        checkable: true
                        checked: review.view === "excluded"
                        text: "직접 제외" + (review.records ? " (" + review.records.excludedCount.toLocaleString() + ")" : "")
                        onClicked: {
                            review.show("excluded")
                            checked = Qt.binding(function() { return review.view === "excluded" })
                        }
                    }
                }
                Label {
                    objectName: "unsavedGuide"
                    Layout.fillWidth: true
                    text: review.view === "excluded"
                        ? "다시 받지 않도록 빼 둔 미디어입니다. ‘제외 해제’는 다음 저장에서 다시 받게 하고, ‘다시 받기’는 지금 받습니다. 저장된 파일은 바뀌지 않습니다."
                        : review.view === "missing"
                        ? "저장했지만 폴더에 파일이 없는 미디어입니다. 갤러리에는 ‘파일 없음’ 칸으로 보입니다. ‘다시 받기’는 지금 다시 받고, ‘갤러리에서 빼기’는 갤러리에서 감추고 다시 받지 않게 합니다. 뺀 것은 ‘직접 제외’에서 되돌릴 수 있습니다."
                        : "저장하지 못한 미디어입니다. ‘다시 저장’은 지금 다시 받습니다. 목록에서 지워도 저장된 파일은 그대로이고, 직접 제외한 항목은 빠지지 않습니다."
                    wrapMode: Text.WordWrap
                    textFormat: Text.PlainText
                    font.pixelSize: Theme.fontSmall
                    color: Theme.textMuted
                }
                Flow {
                    objectName: "unsavedActions"
                    Layout.fillWidth: true
                    spacing: Theme.gapSmall
                    visible: grid.count > 0
                    FlatButton {
                        objectName: "unsavedRetry"
                        tone: "primary"
                        text: review.chosen > 0 ? "선택한 " + review.chosen.toLocaleString() + "개 " + review.retryWord
                                                : "모두 " + review.retryWord
                        enabled: !!review.app && review.app.canStart && !!review.records
                            && (review.chosen > 0 || review.records.retryableCount > 0)
                        onClicked: review.chosen > 0 ? review.records.retrySelected() : review.records.retryAll()
                    }
                    FlatButton {
                        objectName: "unsavedSettle"
                        text: review.chosen > 0 ? "선택한 " + review.chosen.toLocaleString() + "개 " + review.settleWord
                            : review.confirmAll ? "한 번 더 누르면 모두 " + review.settleWord
                            : "모두 " + review.settleWord
                        tip: review.view === "missing"
                            ? "갤러리에서 감추고 다음 저장에서 다시 받지 않습니다. 기록은 지우지 않으며 ‘직접 제외’에서 되돌릴 수 있습니다."
                            : review.view === "excluded" ? "다음 저장에서 다시 받게 합니다"
                            : "이 목록에서만 지웁니다. 저장된 파일에는 영향이 없습니다."
                        onClicked: {
                            if (review.chosen > 0) {
                                review.records.settleSelected()
                            } else if (review.confirmAll) {
                                review.confirmAll = false
                                confirmTimer.stop()
                                if (review.view === "excluded")
                                    review.records.releaseAll()
                                else if (review.view === "missing")
                                    review.records.excludeAll()
                                else
                                    review.records.dismissAll()
                            } else {
                                review.confirmAll = true
                                confirmTimer.restart()
                            }
                        }
                    }
                    FlatButton {
                        objectName: "unsavedSelectPage"
                        tone: "ghost"
                        text: review.chosen > 0 ? "선택 해제" : "보이는 것 모두 선택"
                        onClicked: review.chosen > 0 ? review.records.clearUnsavedSelection()
                                                     : review.records.selectUnsavedPage()
                    }
                    FlatButton {
                        objectName: "unsavedOpenSource"
                        tone: "ghost"
                        text: "원문 보기"
                        visible: review.chosen === 1
                        onClicked: review.records.openSource(review.chosenTokens[0])
                    }
                }
                Label {
                    objectName: "unsavedRetryHint"
                    Layout.fillWidth: true
                    visible: grid.count > 0 && !!review.app && !review.app.canStart
                    text: review.retryWord + "하려면 X 계정이 연결되어 있고 진행 중인 작업이 끝나야 합니다."
                    color: Theme.notice
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSmall
                }
                Label {
                    objectName: "unsavedMessage"
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: review.records ? (review.records.unsavedError || review.records.unsavedStatus) : ""
                    color: review.records && review.records.unsavedError ? Theme.error : Theme.textMuted
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSmall
                }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            GridView {
                id: grid
                objectName: "unsavedGrid"
                anchors.fill: parent
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                readonly property int columns: Math.max(1, Math.floor(width / (review.tileWidth + 12)))
                cellWidth: Math.floor(width / columns)
                cellHeight: Math.round(review.tileWidth * 0.75) + 62
                model: review.records ? review.records.unsavedRows : []
                ScrollBar.vertical: ScrollBar {
                    policy: grid.contentHeight > grid.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                }
                SmoothWheel { flickable: grid; notchPixels: 160 }

                delegate: Item {
                    id: cell
                    required property var modelData
                    required property int index
                    width: grid.cellWidth
                    height: grid.cellHeight
                    Rectangle {
                        id: tile
                        objectName: "unsavedTile"
                        readonly property bool picked: review.chosenTokens.indexOf(cell.modelData.token) >= 0
                        anchors.fill: parent
                        anchors.margins: 4
                        radius: Theme.radiusCard
                        color: picked ? Theme.accentSoft : (tileMouse.containsMouse ? Theme.raised : Theme.well)
                        border.width: picked ? 1 : 0
                        border.color: Theme.accent
                        Accessible.role: Accessible.CheckBox
                        Accessible.name: cell.modelData.reasonLabel + " " + cell.modelData.author
                        Accessible.checked: picked

                        Item {
                            id: frame
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: 8
                            height: parent.height - 62
                            Image {
                                id: preview
                                objectName: "unsavedPreview"
                                anchors.fill: parent
                                source: cell.modelData.preview || ""
                                sourceSize: Qt.size(384, 384)
                                asynchronous: true
                                cache: false
                                fillMode: Image.PreserveAspectFit
                                visible: status === Image.Ready
                            }
                            // No picture kept on this PC: say what kind of media it is.
                            Canvas {
                                objectName: "unsavedPlaceholder"
                                visible: !preview.visible
                                anchors.centerIn: parent
                                width: 32
                                height: 26
                                property string kind: cell.modelData.mediaType
                                onKindChanged: requestPaint()
                                onPaint: {
                                    const ctx = getContext("2d")
                                    ctx.reset()
                                    ctx.strokeStyle = Theme.textFaint
                                    ctx.fillStyle = Theme.textFaint
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
                        Rectangle {
                            objectName: "unsavedReason"
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.margins: 12
                            width: reasonLabel.implicitWidth + 14
                            height: 22
                            radius: height / 2
                            color: cell.modelData.reason === "excluded" ? Theme.raised
                                : cell.modelData.reason === "failed" ? Theme.errorSoft : Theme.noticeSoft
                            Label {
                                id: reasonLabel
                                anchors.centerIn: parent
                                text: cell.modelData.reasonLabel
                                font.pixelSize: Theme.fontTiny
                                font.bold: true
                                color: cell.modelData.reason === "excluded" ? Theme.textMuted
                                    : cell.modelData.reason === "failed" ? Theme.error : Theme.notice
                            }
                        }
                        Rectangle {
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 12
                            width: 22
                            height: 22
                            radius: Theme.radiusSmall
                            visible: tile.picked
                            color: Theme.accent
                            Label {
                                anchors.centerIn: parent
                                text: "✓"
                                color: "white"
                                font.bold: true
                                font.pixelSize: Theme.fontSmall
                            }
                        }
                        Column {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            anchors.margins: 8
                            spacing: 1
                            Label {
                                width: parent.width
                                text: cell.modelData.author
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textMuted
                            }
                            Label {
                                width: parent.width
                                text: cell.modelData.info + (cell.modelData.posted ? " · 게시 " + cell.modelData.posted : "")
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontTiny
                                color: Theme.textFaint
                            }
                            Label {
                                width: parent.width
                                visible: cell.modelData.hint.length > 0
                                text: cell.modelData.hint
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontTiny
                                color: Theme.notice
                            }
                        }
                        MouseArea {
                            id: tileMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            ToolTip.visible: containsMouse
                            ToolTip.delay: 600
                            ToolTip.text: cell.modelData.detail + (cell.modelData.seen ? "\n마지막 확인 " + cell.modelData.seen : "")
                                + "\n누르면 선택 · 두 번 누르면 원문 보기"
                            onClicked: review.records.toggleUnsavedSelected(cell.modelData.token)
                            onDoubleClicked: review.records.openSource(cell.modelData.token)
                        }
                    }
                }
            }
            Label {
                objectName: "unsavedEmpty"
                anchors.centerIn: parent
                width: Math.max(0, parent.width - 32)
                visible: grid.count === 0 && !!review.records && !review.records.unsavedBusy
                    && !review.records.unsavedError.length
                text: review.view === "excluded" ? "직접 제외한 미디어가 없어요"
                    : review.view === "missing" ? "없어진 파일이 없어요 · 저장한 파일이 모두 제자리에 있어요"
                    : "저장하지 못한 미디어가 없어요 · 모두 잘 담겼어요"
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: Theme.textMuted
            }
            Column {
                anchors.centerIn: parent
                visible: grid.count === 0 && !!review.records && review.records.unsavedBusy
                    && !review.records.unsavedError.length
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
                    color: Theme.textMuted
                }
            }
        }

        RowLayout {
            objectName: "unsavedPager"
            Layout.fillWidth: true
            visible: !!review.records && review.records.unsavedPages > 1
            spacing: Theme.gap
            Label {
                Layout.fillWidth: true
                text: "미리보기는 이 PC에 남아 있는 것만 보여 줍니다. ‘직접 제외’의 빈 칸은 전체 목록 확인 저장을 하면 채워집니다."
                color: Theme.textFaint
                font.pixelSize: Theme.fontCaption
                elide: Text.ElideRight
            }
            FlatButton {
                objectName: "unsavedPrevious"
                text: "이전"
                enabled: !!review.records && !review.records.unsavedBusy && review.records.unsavedPage > 0
                onClicked: review.records.loadUnsaved(review.records.unsavedPage - 1)
            }
            Label {
                objectName: "unsavedPageLabel"
                text: review.records ? (review.records.unsavedPage + 1) + " / " + review.records.unsavedPages : ""
                color: Theme.textMuted
            }
            FlatButton {
                objectName: "unsavedNext"
                text: "다음"
                enabled: !!review.records && !review.records.unsavedBusy
                    && review.records.unsavedPage + 1 < review.records.unsavedPages
                onClicked: review.records.loadUnsaved(review.records.unsavedPage + 1)
            }
        }
    }
}
