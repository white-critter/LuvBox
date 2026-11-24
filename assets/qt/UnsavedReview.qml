pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

// Media that is not in the gallery, looked at the way the gallery is: what a save could not
// store, what the person excluded, and saved files that are gone. Tiles are picked as in
// the gallery (click, Shift+click for a range, drag for a rectangle) and the buttons above
// act on the picked ones, or on the whole list when none is. More tiles arrive as the
// view scrolls.
Item {
    id: review
    property var app: null
    property int tileWidth: 168
    readonly property var hostWindow: Window.window
    signal backRequested()

    readonly property var records: app ? app.records : null
    readonly property string view: records ? records.unsavedView : "unsaved"
    readonly property int chosen: records ? records.unsavedSelectedCount : 0
    // Acting on a whole list asks once more; it lapses after a few seconds.
    property bool confirmAll: false
    // The tile a click just picked or let go of, and how it was before (see the tile's double click).
    property string clickedToken: ""
    property bool clickedWasPicked: false
    readonly property string retryWord: view === "unsaved" ? "다시 저장" : "다시 받기"
    readonly property string settleWord: view === "excluded" ? "제외 해제"
        : view === "missing" ? "갤러리에서 빼기" : "목록에서 지우기"

    function show(name) {
        confirmAll = false
        if (records)
            records.setUnsavedView(name)
    }

    // Esc cancels whole-list confirmation first, then clears picked tiles, then leaves the page.
    function stepBack() {
        if (confirmAll) {
            confirmAll = false
            confirmTimer.stop()
        } else if (chosen > 0) {
            records.clearUnsavedSelection()
        } else {
            backRequested()
        }
    }

    function selectAll() {
        if (records && grid.count > 0)
            records.selectUnsavedPage()
    }

    function requestMore() {
        if (records && records.unsavedMore && !records.unsavedBusy
                && grid.contentY - grid.originY + grid.height + grid.cellHeight * 2 >= grid.contentHeight)
            records.loadMoreUnsaved()
    }

    onViewChanged: confirmAll = false
    onChosenChanged: confirmAll = false
    onVisibleChanged: if (!visible) {
        confirmAll = false
        confirmTimer.stop()
    }

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
                    Label {
                        objectName: "unsavedLoadedCount"
                        visible: !!review.records && review.records.unsavedTotal > 0
                        text: review.records
                            ? (review.records.unsavedTotal > grid.count
                               ? review.records.unsavedTotal.toLocaleString() + "개 중 " + grid.count.toLocaleString() + "개 불러옴"
                               : review.records.unsavedTotal.toLocaleString() + "개")
                            : ""
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontSmall
                    }
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
                    // In a low window the guide left less than one row of tiles to see.
                    visible: review.height >= 560
                    text: review.view === "excluded"
                        ? "‘제외 해제’하면 다음 저장에 포함됩니다."
                        : review.view === "missing"
                        ? "갤러리에서 뺀 항목은 ‘직접 제외’에서 되돌릴 수 있습니다."
                        : "저장에 실패했거나 용량 제한을 넘은 미디어입니다."
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
                        enabled: !!review.app && review.app.canStart
                            && review.app.accounts.authenticationState !== "reconnect_required"
                            && !!review.records && !review.records.unsavedMutating
                            && (review.chosen > 0 || review.records.retryableCount > 0)
                        tip: !enabled && !!review.records && review.records.unsavedMutating
                            ? "선택한 항목을 정리하는 중…"
                            : (!enabled && !!review.app && (!review.app.canStart
                                    || review.app.accounts.authenticationState === "reconnect_required")
                                ? "X 계정을 연결하고 작업이 끝난 뒤 다시 시도할 수 있습니다"
                                : (!enabled && !!review.records && review.chosen === 0 && review.records.retryableCount === 0
                                    ? "크기 제한 초과 등으로 다시 받을 수 있는 항목이 없습니다" : ""))
                        onDoubleClicked: {}
                        onClicked: review.chosen > 0 ? review.records.retrySelected() : review.records.retryAll()
                    }
                    FlatButton {
                        objectName: "unsavedSettle"
                        enabled: !!review.records && !review.records.unsavedMutating
                        text: review.chosen > 0 ? "선택한 " + review.chosen.toLocaleString() + "개 " + review.settleWord
                            : review.confirmAll ? "한 번 더 누르면 모두 " + review.settleWord
                            : "모두 " + review.settleWord
                        tip: review.view === "missing"
                            ? "갤러리와 다음 저장에서 제외"
                            : review.view === "excluded" ? "다음 저장에 포함"
                            : "실패 기록만 삭제"
                        // A double click counts once. Without this its second click confirmed what
                        // its first had only asked about, and every item of the list was settled.
                        onDoubleClicked: {}
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
                        text: review.chosen > 0 ? "선택 해제" : "불러온 것 모두 선택"
                        tip: review.chosen > 0 ? "선택 해제 (Esc)" : "모두 선택 (Ctrl+A) · 범위 선택 (Shift+클릭) · 사각형 선택 (끌기)"
                        onClicked: review.chosen > 0 ? review.records.clearUnsavedSelection() : review.selectAll()
                    }
                    FlatButton {
                        objectName: "unsavedOpenSource"
                        tone: "ghost"
                        text: "원문 보기"
                        visible: review.chosen === 1
                        onClicked: review.records.openSource(review.records.unsavedSelection[0])
                    }
                }
                Label {
                    objectName: "unsavedRetryHint"
                    Layout.fillWidth: true
                    visible: grid.count > 0 && !!review.app && (!review.app.canStart || review.app.accounts.authenticationState === "reconnect_required")
                    text: "X 계정을 연결하고 작업이 끝난 뒤 다시 시도하세요."
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
                model: review.records ? review.records.unsavedModel : null
                ScrollBar.vertical: ScrollBar {
                    policy: grid.contentHeight > grid.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                    onPressedChanged: if (pressed) unsavedWheel.reset()
                }
                SmoothWheel {
                    id: unsavedWheel
                    flickable: grid
                    notchPixels: Math.max(120, Math.round(grid.cellHeight * 0.6))
                }
                onContentYChanged: review.requestMore()
                onCountChanged: Qt.callLater(review.requestMore)
                onHeightChanged: Qt.callLater(review.requestMore)

                // A list read again after a change (something excluded, released or forgotten)
                // comes back as a new list, which a grid shows from its top; stay where the
                // person was looking. Another list (or the page opened again) is emptied
                // first, so it does start at its top.
                property real keptY: -1
                property int resets: 0
                Connections {
                    target: grid.model
                    function onModelAboutToBeReset() {
                        unsavedWheel.reset()
                        grid.keptY = grid.contentY - grid.originY
                    }
                    function onModelReset() {
                        const wanted = grid.keptY
                        const serial = ++grid.resets
                        grid.keptY = -1
                        if (wanted > 0)
                            Qt.callLater(function() {
                                if (serial !== grid.resets)
                                    return              // a newer list arrived meanwhile
                                const limit = Math.max(0, grid.contentHeight - grid.height)
                                grid.contentY = grid.originY + Math.min(wanted, limit)
                            })
                    }
                }

                delegate: Item {
                    id: cell
                    required property int index
                    required property string token
                    required property string author
                    required property string reason
                    required property string reasonLabel
                    required property string mediaType
                    required property string info
                    required property string posted
                    required property string detail
                    required property string seen
                    required property string hint
                    required property string preview
                    required property bool picked
                    width: grid.cellWidth
                    height: grid.cellHeight
                    Rectangle {
                        id: tile
                        objectName: "unsavedTile"
                        readonly property bool picked: cell.picked
                        readonly property string token: cell.token
                        anchors.fill: parent
                        anchors.margins: 4
                        radius: Theme.radiusCard
                        activeFocusOnTab: true
                        color: cell.picked ? Theme.accentSoft : (tileMouse.containsMouse ? Theme.raised : Theme.well)
                        border.width: tile.activeFocus ? 2 : (cell.picked ? 1 : 0)
                        border.color: tile.activeFocus ? Theme.focus : Theme.accent
                        Accessible.role: Accessible.CheckBox
                        Accessible.name: cell.reasonLabel + " " + cell.author
                        Accessible.checkable: true
                        Accessible.checked: cell.picked
                        // Picked without a mouse too: a screen reader toggles or presses the tile.
                        Accessible.onToggleAction: review.records.toggleUnsavedSelected(cell.token)
                        Accessible.onPressAction: review.records.toggleUnsavedSelected(cell.token)
                        onActiveFocusChanged: if (activeFocus) grid.positionViewAtIndex(cell.index, GridView.Contain)
                        Keys.onSpacePressed: event => {
                            if (review.records)
                                review.records.toggleUnsavedSelected(cell.token)
                            event.accepted = true
                        }
                        Keys.onReturnPressed: event => {
                            if (review.records)
                                review.records.openSource(cell.token)
                            event.accepted = true
                        }
                        Keys.onEnterPressed: event => {
                            if (review.records)
                                review.records.openSource(cell.token)
                            event.accepted = true
                        }

                        Item {
                            id: frame
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: 8
                            height: parent.height - 62
                            Image {
                                id: picture
                                objectName: "unsavedPreview"
                                anchors.fill: parent
                                // Read from this PC's preview cache when the tile is shown.
                                source: cell.preview
                                sourceSize: Qt.size(150, 112)
                                asynchronous: true
                                cache: false
                                fillMode: Image.PreserveAspectFit
                                visible: status === Image.Ready
                            }
                            // No picture kept on this PC: say what kind of media it is.
                            Loader {
                                anchors.centerIn: parent
                                width: 32
                                height: 26
                                active: !picture.visible
                                sourceComponent: Canvas {
                                    objectName: "unsavedPlaceholder"
                                    property string kind: cell.mediaType
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
                        }
                        Rectangle {
                            objectName: "unsavedReason"
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.margins: 12
                            width: reasonText.implicitWidth + 14
                            height: 22
                            radius: height / 2
                            color: cell.reason === "excluded" ? Theme.raised
                                : cell.reason === "failed" ? Theme.errorSoft : Theme.noticeSoft
                            Label {
                                id: reasonText
                                anchors.centerIn: parent
                                text: cell.reasonLabel
                                font.pixelSize: Theme.fontTiny
                                font.bold: true
                                color: cell.reason === "excluded" ? Theme.textMuted
                                    : cell.reason === "failed" ? Theme.error : Theme.notice
                            }
                        }
                        Rectangle {
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 12
                            width: 22
                            height: 22
                            radius: Theme.radiusSmall
                            visible: cell.picked
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
                                text: cell.author
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textMuted
                            }
                            Label {
                                width: parent.width
                                text: cell.info + (cell.posted ? " · 게시 " + cell.posted : "")
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontTiny
                                color: Theme.textFaint
                            }
                            Label {
                                width: parent.width
                                visible: cell.hint.length > 0
                                text: cell.hint
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
                            ToolTip.visible: containsMouse && !bandDrag.active
                            ToolTip.delay: 600
                            ToolTip.text: [
                                cell.detail,
                                cell.seen ? (review.view === "excluded" ? "제외 일시 "
                                    : review.view === "missing" ? "확인 일시 " : "마지막 확인 ") + cell.seen : "",
                                "클릭 선택 · Shift+클릭 범위 선택 · 더블 클릭 원문 보기"
                            ].filter(Boolean).join("\n")
                            onClicked: mouse => {
                                if (mouse.modifiers & Qt.ShiftModifier) {
                                    review.clickedToken = ""
                                    review.records.selectUnsavedRangeTo(cell.token)
                                } else {
                                    review.clickedToken = cell.token
                                    review.clickedWasPicked = cell.picked
                                    review.records.toggleUnsavedSelected(cell.token)
                                }
                            }
                            onDoubleClicked: {
                                // The first click of the two picked or let go of the tile; opening
                                // the post leaves it as it was before.
                                if (review.clickedToken === cell.token && cell.picked !== review.clickedWasPicked)
                                    review.records.toggleUnsavedSelected(cell.token)
                                review.clickedToken = ""
                                review.records.openSource(cell.token)
                            }
                        }
                    }
                }

                // Drag a rectangle to pick the tiles it touches, as in the gallery.
                DragHandler {
                    id: bandDrag
                    objectName: "unsavedBandDrag"
                    target: null
                    acceptedDevices: PointerDevice.Mouse
                    acceptedButtons: Qt.LeftButton
                    enabled: !!review.records && grid.count > 0
                    dragThreshold: 10
                    property real startX: 0
                    property real startY: 0
                    property real currentX: 0
                    property real currentY: 0
                    // Ctrl or Shift held when the button went down adds to what is picked,
                    // even if the key is let go before the drag ends.
                    property int pressModifiers: Qt.NoModifier
                    onGrabChanged: (transition, point) => {
                        if (transition === PointerDevice.GrabPassive)
                            pressModifiers = centroid.modifiers
                    }
                    function update() {
                        // The rectangle lives in content coordinates, so it grows while the grid scrolls under it.
                        const top = Math.min(startY, currentY)
                        const bottom = Math.max(startY, currentY)
                        const left = Math.min(startX, currentX)
                        const right = Math.max(startX, currentX)
                        review.records.unsavedBandSelect(
                            Math.floor(Math.max(0, top) / grid.cellHeight),
                            Math.floor(Math.max(0, bottom) / grid.cellHeight),
                            Math.floor(Math.max(0, left) / grid.cellWidth),
                            Math.floor(Math.max(0, right) / grid.cellWidth), grid.columns)
                    }
                    // The handler reports positions in the scrolling content, which go stale while
                    // the grid moves under a resting pointer; the scene position is mapped instead.
                    function inView(scenePoint) {
                        return grid.mapFromItem(null, scenePoint.x, scenePoint.y)
                    }
                    function follow() {
                        const point = inView(centroid.scenePosition)
                        currentX = Math.max(0, Math.min(grid.width, point.x))
                        currentY = point.y + grid.contentY - grid.originY
                        update()
                    }
                    onActiveChanged: {
                        if (active) {
                            unsavedWheel.reset()
                            const pressed = inView(centroid.scenePressPosition)
                            startX = Math.max(0, Math.min(grid.width, pressed.x))
                            startY = pressed.y + grid.contentY - grid.originY
                            const held = pressModifiers | centroid.modifiers
                            review.records.beginUnsavedBand((held & (Qt.ControlModifier | Qt.ShiftModifier)) !== 0)
                            follow()
                        } else {
                            review.records.endUnsavedBand()
                        }
                    }
                    onCentroidChanged: if (active) follow()
                }
                Timer {
                    // Near the top or bottom edge the grid scrolls and the rectangle keeps growing.
                    interval: 30
                    repeat: true
                    running: bandDrag.active
                    onTriggered: {
                        const y = bandDrag.inView(bandDrag.centroid.scenePosition).y
                        let step = 0
                        if (y < 28)
                            step = -Math.min(48, 8 + (28 - y) * 0.6)
                        else if (y > grid.height - 28)
                            step = Math.min(48, 8 + (y - (grid.height - 28)) * 0.6)
                        if (!step)
                            return
                        const minimum = grid.originY
                        const maximum = minimum + Math.max(0, grid.contentHeight - grid.height)
                        const next = Math.max(minimum, Math.min(maximum, grid.contentY + step))
                        if (next !== grid.contentY) {
                            grid.contentY = next
                            bandDrag.follow()
                        }
                    }
                }
                Rectangle {
                    objectName: "unsavedBand"
                    // Drawn over the visible area, not inside the scrolling content.
                    parent: grid
                    visible: bandDrag.active
                    z: 5
                    x: Math.min(bandDrag.startX, bandDrag.currentX)
                    y: Math.min(bandDrag.startY, bandDrag.currentY) - (grid.contentY - grid.originY)
                    width: Math.abs(bandDrag.currentX - bandDrag.startX)
                    height: Math.abs(bandDrag.currentY - bandDrag.startY)
                    color: Qt.alpha(Theme.accent, 0.2)
                    border.width: 1
                    border.color: Theme.accent
                }
            }
            Label {
                objectName: "unsavedEmpty"
                anchors.centerIn: parent
                width: Math.max(0, parent.width - 32)
                visible: grid.count === 0 && !!review.records && !review.records.unsavedBusy
                    && !review.records.unsavedError.length
                text: review.view === "excluded" ? "직접 제외한 미디어가 없어요"
                    : review.view === "missing" ? "없어진 파일이 없어요"
                    : "저장하지 못한 미디어가 없어요"
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
                        && (!review.hostWindow || (review.hostWindow.visible && review.hostWindow.visibility !== Window.Minimized))
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
    }
}
