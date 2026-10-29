pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: window
    required property var galleryModel
    required property var wheelInputFilter
    property var appController: null
    width: 1080
    height: 760
    minimumWidth: 680
    minimumHeight: 480
    visible: true
    readonly property var app: appController
    readonly property color cardColor: "#191919"
    readonly property color textColor: "#f4f1f1"
    readonly property color mutedColor: "#b9afb1"
    readonly property color lineColor: "#353535"
    readonly property color accentColor: "#d93645"
    readonly property color errorColor: "#ff8791"
    readonly property color noticeColor: "#e6bd79"
    readonly property bool modalOpen: previewDialog.visible || connectionDialog.visible ||
        preferencesDialog.visible || saveReviewDialog.visible || storageDialog.visible || repairDialog.visible ||
        folderDialog.visible || deleteConfirmation.visible
    title: app ? "LuvBox · " + app.version : "LuvBox · 갤러리"
    color: "#101010"
    palette.window: color
    palette.windowText: textColor
    palette.base: "#121212"
    palette.alternateBase: "#202020"
    palette.text: textColor
    palette.button: "#252525"
    palette.buttonText: textColor
    palette.highlight: accentColor
    palette.highlightedText: "white"
    palette.placeholderText: mutedColor
    palette.light: "#4b4143"
    palette.midlight: "#40383a"
    palette.mid: lineColor
    palette.dark: "#0b0b0b"
    palette.shadow: "#080808"
    palette.toolTipBase: "#252525"
    palette.toolTipText: textColor
    palette.link: "#ff6977"
    palette.linkVisited: "#de9ca4"
    palette.disabled.text: "#776b6e"
    palette.disabled.buttonText: "#776b6e"
    palette.disabled.windowText: "#776b6e"
    property string uiFontFamily: "Pretendard"
    font.family: uiFontFamily
    font.pixelSize: 14
    font.weight: Font.Normal
    font.hintingPreference: Font.PreferVerticalHinting
    property int currentScreen: 0
    onCurrentScreenChanged: {
        stopWheel()
        if (app)
            app.galleryVisible(currentScreen === 1)
        if (currentScreen === 1)
            Qt.callLater(updateFocus)
        if (app && currentScreen === 2)
            app.management.refresh()
    }
    property int densityChoice: 1
    readonly property int tileWidth: [168, 240, 320][densityChoice]
    property int liveDelegates: 0
    property int peakDelegates: 0
    property int visibleTiles: 0
    property int readyTiles: 0
    property string selectedThumb: ""
    property bool focusReady: false
    property real focusContentY: 0
    property bool sampleOnScroll: false
    property int topIndex: -1
    property int heldAnchor: -1
    property int densityRequest: -1
    onDensityRequestChanged: if (densityRequest >= 0) setDensity(densityRequest)
    readonly property real thumbnailPixelRatio: Math.max(1, window.Screen.devicePixelRatio)
    onThumbnailPixelRatioChanged: Qt.callLater(updateFocus)

    function refreshSelection(item) {
        selectedThumb = item ? item.thumbUrl : ""
        if (app && item && item.key)
            app.library.selectItem(item.key)
    }

    function accountIndex() {
        if (!app)
            return -1
        let rows = app.accounts.accounts
        for (let index = 0; index < rows.length; index++) {
            if (rows[index].user_id === app.accounts.selectedAccount)
                return index
        }
        return -1
    }

    onClosing: (close) => {
        if (app && !app.shutdownReady) {
            close.accepted = false
            app.requestClose()
        }
    }

    function stopWheel() {
        if (wheelMotion)
            wheelMotion.reset()
    }

    property int pendingAnchor: -1

    function columnCount() {
        return Math.max(1, Math.floor(gallery.width / Math.max(1, gallery.cellWidth)))
    }

    function setDensity(choice) {
        stopWheel()
        anchorTimer.stop()
        gallery.cancelFlick()
        let columns = columnCount()
        let found = Math.floor(Math.max(0, gallery.contentY - gallery.originY) / Math.max(1, gallery.cellHeight)) * columns
        pendingAnchor = found
        heldAnchor = found
        densityChoice = choice
    }

    function scheduledThumbSize() {
        let ratio = thumbnailPixelRatio
        return [Math.min(384, Math.max(1, Math.round((tileWidth - 28) * ratio))),
                Math.min(384, Math.max(1, Math.round((Math.round(tileWidth * 0.75) - 16) * ratio)))]
    }

    property int anchorGeneration: 0

    function placeAnchor() {
        if (pendingAnchor < 0)
            return
        anchorGeneration += 1
        anchorTimer.keep = pendingAnchor
        anchorTimer.tries = 0
        anchorTimer.generation = anchorGeneration
        gallery.highlightFollowsCurrentItem = false
        anchorTimer.restart()
    }

    Timer {
        id: anchorTimer
        interval: 40
        repeat: true
        property int keep: -1
        property int tries: 0
        property int generation: 0
        onTriggered: {
            tries += 1
            if (generation !== window.anchorGeneration || keep < 0 || gallery.width < 2 || gallery.cellWidth < 2 || gallery.cellHeight < 2) {
                if (tries >= 8) {
                    gallery.highlightFollowsCurrentItem = true
                    stop()
                }
                return
            }
            let columns = window.columnCount()
            let rowStart = keep - (keep % columns)
            let offset = (rowStart / columns) * gallery.cellHeight
            if (gallery.contentHeight < offset + gallery.cellHeight) {
                if (tries >= 8) {
                    gallery.highlightFollowsCurrentItem = true
                    stop()
                }
                return
            }
            let limit = Math.max(0, gallery.contentHeight - gallery.height)
            let target = gallery.originY + Math.min(offset, limit)
            gallery.cancelFlick()
            gallery.contentY = target
            window.topIndex = rowStart
            window.updateFocus()
            let placed = Math.abs(gallery.contentY - target) < 1
            if (placed || tries >= 8) {
                let visibleEnd = rowStart + columns * Math.max(1, Math.ceil(gallery.height / gallery.cellHeight))
                if (gallery.currentIndex < rowStart || gallery.currentIndex >= visibleEnd)
                    gallery.currentIndex = rowStart
                gallery.highlightFollowsCurrentItem = true
                stop()
            }
        }
    }

    function updateFocus() {
        if (currentScreen !== 1 || gallery.width <= 0 || gallery.height <= 0 || gallery.cellWidth <= 0 || gallery.cellHeight <= 0 || gallery.contentHeight <= 0 || gallery.count === 0)
            return
        let y = Math.max(0, gallery.contentY - gallery.originY)
        let direction = y >= focusContentY ? 1 : -1
        focusContentY = y
        let columns = columnCount()
        let first = Math.min(gallery.count - 1, Math.floor(y / gallery.cellHeight) * columns)
        let bottom = Math.min(gallery.contentHeight, y + gallery.height)
        let last = Math.min(gallery.count - 1, Math.max(first, Math.ceil(bottom / gallery.cellHeight) * columns - 1))
        topIndex = first
        focusReady = true
        let thumb = scheduledThumbSize()
        galleryModel.focusViewport(first | 0, last | 0, (direction * columns * 2) | 0, thumb[0], thumb[1])
    }

    function requestMore() {
        if (currentScreen === 1 && gallery.contentY - gallery.originY + gallery.height + gallery.cellHeight * 2 >= gallery.contentHeight)
            galleryModel.loadMore()
    }

    function sampleVisible() {
        let visible = 0
        let ready = 0
        for (let child of gallery.contentItem.children) {
            if (child.objectName === "galleryTile" && child.activeTile && child.y + child.height > gallery.contentY && child.y < gallery.contentY + gallery.height) {
                visible++
                if (child.imageReady)
                    ready++
            }
        }
        visibleTiles = visible
        readyTiles = ready
    }

    header: ToolBar {
        height: 64
        background: Rectangle {
            color: window.color
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: window.lineColor
            }
        }
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 24
            spacing: 8
            Image {
                source: "../icons/luvbox.png"
                Layout.preferredWidth: 28
                Layout.preferredHeight: 28
                sourceSize: Qt.size(28, 28)
                fillMode: Image.PreserveAspectFit
            }
            Label { text: "LuvBox"; font.pixelSize: 18; Layout.rightMargin: 16 }
            FlatButton { text: "저장"; tone: "nav"; checkable: true; checked: window.currentScreen === 0; onClicked: window.currentScreen = 0 }
            FlatButton { text: "갤러리"; tone: "nav"; checkable: true; checked: window.currentScreen === 1; onClicked: window.currentScreen = 1 }
            FlatButton { text: "관리"; objectName: "managementNavigation"; visible: !!window.app; tone: "nav"; checkable: true; checked: window.currentScreen === 2; onClicked: window.currentScreen = 2 }
            Item { Layout.fillWidth: true }
            Label { text: window.app ? window.app.version : ""; color: window.mutedColor; visible: window.width > 760 }
            FlatButton { text: "설정"; visible: !!window.app; enabled: window.app && !window.app.busy; onClicked: preferencesDialog.open() }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 18

        Label {
            visible: !window.app
            text: "생성한 사진으로 화면을 확인합니다. 실제 계정·저장 폴더는 연결하지 않습니다."
            color: window.mutedColor
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        StackLayout {
            currentIndex: window.currentScreen
            Layout.fillWidth: true
            Layout.fillHeight: true

            ScrollView {
                id: saveScroll
                clip: true
                contentWidth: availableWidth
                contentHeight: saveForm.implicitHeight + 32
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                ColumnLayout {
                    id: saveForm
                    width: Math.max(1, Math.min(saveScroll.availableWidth - 24, 640))
                    height: implicitHeight
                    x: Math.max(0, (saveScroll.availableWidth - width) / 2)
                    y: 16
                    spacing: 18
                    Label { text: "사진 저장"; font.pixelSize: 26 }
                    Label { text: "좋아요한 사진과 영상을 내 폴더에."; color: window.mutedColor }
                    Frame {
                        Layout.fillWidth: true
                        padding: 20
                        visible: !!window.app
                        background: Rectangle { color: window.cardColor; radius: 12 }
                        ColumnLayout {
                            width: parent.width
                            spacing: 12
                            Label { text: "X 계정"; color: window.mutedColor }
                            RowLayout {
                                Layout.fillWidth: true
                                FlatCombo {
                                    Layout.fillWidth: true
                                    model: window.app ? window.app.accounts.accounts : []
                                    textRole: "handle"
                                    valueRole: "user_id"
                                    currentIndex: window.accountIndex()
                                    displayText: currentIndex >= 0 ? "@" + currentText : "연결 계정 선택"
                                    visible: count > 0
                                    enabled: window.app && !window.app.busy
                                    onActivated: if (window.app) window.app.accounts.selectAccount(currentValue)
                                    Accessible.name: "연결된 X 계정"
                                }
                                Label {
                                    Layout.fillWidth: true
                                    visible: !window.app || window.app.accounts.accounts.length === 0
                                    text: window.app && window.app.accounts.connected ? "연결 정보 저장됨" : "X 로그인을 연결해 주세요"
                                    color: window.mutedColor
                                }
                                FlatButton {
                                    text: window.app && window.app.accounts.authenticationState === "reconnect_required" ? "다시 연결" : "X 연결"
                                    enabled: window.app && !window.app.busy
                                    onClicked: connectionDialog.open()
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: window.app && window.app.accounts.connected
                                text: window.app ? window.app.accounts.authenticationLabel : ""
                                color: window.app && window.app.accounts.authenticationState === "reconnect_required"
                                    ? window.errorColor : window.mutedColor
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                            }
                            FlatField {
                                id: accountInput
                                objectName: "saveAccountInput"
                                Layout.fillWidth: true
                                visible: window.app && !window.app.accounts.identity.handle
                                enabled: window.app && !window.app.busy
                                text: window.app ? window.app.account : ""
                                placeholderText: "저장할 X 아이디 (@ 없이)"
                                maximumLength: 15
                                validator: RegularExpressionValidator { regularExpression: /[A-Za-z0-9_]{1,15}/ }
                                selectByMouse: true
                                Accessible.name: "저장할 X 아이디"
                                onEditingFinished: if (window.app) window.app.setAccount(text)
                            }
                            Label { text: "저장 위치"; color: window.mutedColor; Layout.topMargin: 8 }
                            RowLayout {
                                Layout.fillWidth: true
                                Label {
                                    id: outputFolderLabel
                                    Layout.fillWidth: true
                                    text: window.app && window.app.outputFolder.length > 0
                                        ? window.app.outputFolder : "저장할 폴더를 선택해 주세요"
                                    color: window.app && window.app.outputFolder.length > 0
                                        ? window.textColor : window.mutedColor
                                    textFormat: Text.PlainText
                                    elide: Text.ElideMiddle
                                    HoverHandler { id: outputFolderHover }
                                    ToolTip.text: outputFolderLabel.text
                                    ToolTip.delay: 500
                                    ToolTip.visible: outputFolderHover.hovered && outputFolderLabel.truncated
                                }
                                FlatButton { text: "폴더 선택"; enabled: window.app && !window.app.busy; onClicked: window.chooseSaveFolder() }
                                FlatButton { text: "열기"; enabled: !!(window.app && window.app.outputFolder.length > 0); onClicked: if (window.app) window.app.openOutputFolder() }
                            }
                        }
                    }
                    Frame {
                        Layout.fillWidth: true
                        visible: !!window.app
                        padding: 20
                        background: Rectangle { color: window.cardColor; radius: 12 }
                        ColumnLayout {
                            width: parent.width
                            spacing: 14
                            Label {
                                id: saveStatus
                                text: window.app ? window.app.status : ""
                                textFormat: Text.PlainText
                                font.pixelSize: 18
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                                color: window.app && window.app.error.length > 0 && text === window.app.error
                                    ? window.errorColor : window.textColor
                            }
                            Label {
                                text: window.app ? window.app.error : ""
                                textFormat: Text.PlainText
                                visible: text.length > 0 && text !== saveStatus.text
                                color: window.errorColor
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                            FlatButton {
                                objectName: "repairButton"
                                text: "기록 복구"
                                visible: !!(window.app && window.app.download.repairNeeded)
                                enabled: window.app && !window.app.busy
                                Accessible.name: "손상된 저장 기록 복구"
                                onClicked: repairDialog.open()
                            }
                            Item {
                                id: saveProgress
                                Layout.fillWidth: true
                                implicitHeight: 4
                                clip: true
                                visible: window.app && window.app.busy && (sliding || amount > 0)
                                readonly property bool sliding: !!(window.app && window.app.busy &&
                                    (!window.app.download.running || window.app.download.progress < 0))
                                readonly property real amount: window.app
                                    ? Math.max(0, Math.min(1, window.app.download.progress)) : 0
                                property real slide: 0
                                Accessible.role: Accessible.ProgressBar
                                Accessible.name: "저장 진행"
                                Rectangle {
                                    anchors.fill: parent
                                    radius: height / 2
                                    color: "#2a2a2a"
                                }
                                Rectangle {
                                    height: parent.height
                                    radius: height / 2
                                    color: window.accentColor
                                    width: saveProgress.sliding ? parent.width * 0.34 : parent.width * saveProgress.amount
                                    x: saveProgress.sliding ? parent.width * saveProgress.slide : 0
                                }
                                NumberAnimation on slide {
                                    running: saveProgress.visible && saveProgress.sliding
                                    loops: Animation.Infinite
                                    from: -0.34
                                    to: 1
                                    duration: 1100
                                    easing.type: Easing.InOutQuad
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                objectName: "saveScheduleSummary"
                                Label {
                                    Layout.fillWidth: true
                                    text: window.app ? "자동 저장 · " + window.app.schedule.summary : ""
                                    color: window.app && window.app.schedule.registered && !window.app.schedule.current
                                        ? window.noticeColor : window.mutedColor
                                    textFormat: Text.PlainText
                                    wrapMode: Text.WordWrap
                                }
                                FlatButton {
                                    text: "예약 관리"
                                    visible: !!window.app
                                    onClicked: {
                                        if (window.app)
                                            window.app.management.setSection("schedule")
                                        window.currentScreen = 2
                                    }
                                }
                            }
                            Label {
                                text: window.app ? window.app.download.notice : ""
                                textFormat: Text.PlainText
                                visible: text.length > 0
                                color: window.noticeColor
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Label {
                                    Layout.fillWidth: true
                                    text: window.app ? "저장 " + window.app.download.savedCount.toLocaleString() +
                                        "개 · 건너뜀 " + window.app.download.skippedCount.toLocaleString() + "개" : ""
                                    color: window.mutedColor
                                }
                                FlatSwitch {
                                    objectName: "savedDetailsToggle"
                                    text: "자세히 보기"
                                    checked: window.app ? window.app.preferences.detailed_progress : false
                                    enabled: window.app && !window.app.closing
                                    onToggled: if (window.app) window.app.setPreference("detailed_progress", checked)
                                }
                            }
                            Label {
                                objectName: "saveReadinessHint"
                                Layout.fillWidth: true
                                text: {
                                    if (!window.app || window.app.busy)
                                        return ""
                                    if (!window.app.accounts.connected)
                                        return "저장하려면 먼저 X 계정을 연결해 주세요."
                                    if (!window.app.outputFolder.length)
                                        return "저장할 폴더를 선택해 주세요."
                                    if (!window.app.accounts.identity.handle && !accountInput.acceptableInput)
                                        return "저장할 X 아이디를 입력해 주세요."
                                    if (window.app.accounts.identity.handle && !window.app.canStart)
                                        return "연결된 X 계정을 선택해 주세요."
                                    return ""
                                }
                                visible: text.length > 0
                                color: window.mutedColor
                                textFormat: Text.PlainText
                                wrapMode: Text.WordWrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                FlatButton {
                                    objectName: "saveStartButton"
                                    text: window.app && window.app.accounts.authenticationState === "reconnect_required"
                                        ? "X 계정 다시 연결" : "저장 시작"
                                    tone: "primary"
                                    Layout.fillWidth: true
                                    enabled: window.app && !window.app.busy && window.app.accounts.connected &&
                                        window.app.outputFolder.length > 0 && (window.app.accounts.identity.handle
                                            ? window.app.canStart : accountInput.acceptableInput)
                                    onClicked: {
                                        window.app.setAccount(accountInput.text)
                                        window.app.startSave()
                                    }
                                }
                                FlatButton {
                                    text: window.app && window.app.download.stopping ? "중단하는 중" : "중단"
                                    visible: window.app && window.app.download.running
                                    enabled: window.app && !window.app.download.stopping
                                    onClicked: window.app.download.stop()
                                }
                            }
                        }
                    }
                    SavedDetails {
                        objectName: "savedDetailsPanel"
                        Layout.fillWidth: true
                        app: window.app
                        visible: window.app && window.app.download.detailsEnabled
                    }
                    FlatButton { text: "갤러리 보기"; visible: !window.app; onClicked: window.currentScreen = 1 }
                }
            }

            ColumnLayout {
                spacing: 10
                RowLayout {
                    spacing: 12
                    Layout.fillWidth: true
                    FlatField {
                        id: search
                        objectName: "searchBox"
                        placeholderText: "사진 검색"
                        implicitHeight: 40
                        Accessible.name: "사진 검색"
                        Layout.fillWidth: true
                        selectByMouse: true
                        onTextEdited: {
                            window.stopWheel()
                            searchDelay.restart()
                        }
                        onAccepted: {
                            window.stopWheel()
                            searchDelay.stop()
                            if (window.app)
                                window.app.library.search(text)
                            else
                                window.galleryModel.search(text)
                        }
                    }
                    FlatCombo {
                        objectName: "thumbSizeChoice"
                        model: ["작게", "보통", "크게"]
                        currentIndex: window.densityChoice
                        implicitWidth: 108
                        implicitHeight: 40
                        Accessible.name: "미리보기 크기"
                        onActivated: window.setDensity(currentIndex)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Label { text: window.galleryModel.total.toLocaleString() + "개"; color: window.mutedColor }
                    BusyIndicator {
                        running: window.galleryModel.busy
                        visible: running
                        implicitWidth: 24
                        implicitHeight: 24
                    }
                    Item { Layout.fillWidth: true }
                    FlatCombo {
                        id: mediaFilter
                        visible: !!window.app
                        model: ["전체", "사진", "영상", "GIF"]
                        implicitWidth: 112
                        Accessible.name: "미디어 종류"
                        onActivated: window.app.library.setFilters(
                            ["all", "photo", "video", "animated_gif"][currentIndex],
                            ["saved", "posted", "posted_oldest", "liked"][orderChoice.currentIndex], window.app.library.filterAuthor)
                    }
                    FlatCombo {
                        id: orderChoice
                        visible: !!window.app
                        model: ["최근 저장", "최신 게시물", "오래된 게시물", "좋아요 순"]
                        implicitWidth: 168
                        Accessible.name: "사진 정렬"
                        onActivated: window.app.library.setFilters(
                            ["all", "photo", "video", "animated_gif"][mediaFilter.currentIndex],
                            ["saved", "posted", "posted_oldest", "liked"][currentIndex], window.app.library.filterAuthor)
                    }
                    FlatButton {
                        visible: !!window.app
                        text: window.app && window.app.galleryChanged ? "새 사진 보기" : "새로고침"
                        onClicked: window.app.refreshGallery()
                    }
                    FlatButton {
                        text: "미리보기"
                        visible: gallery.currentIndex >= 0
                        onClicked: previewDialog.open()
                    }
                }

                Label {
                    objectName: "galleryLoadError"
                    Layout.fillWidth: true
                    text: window.galleryModel.error
                    visible: text.length > 0
                    color: window.errorColor
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }

                RowLayout {
                    Layout.fillWidth: true
                    visible: !!window.app
                    spacing: 8
                    FlatButton {
                        id: filterToggle
                        objectName: "galleryFilterToggle"
                        text: checked ? "필터 닫기" : "작성자·날짜 필터"
                        checkable: true
                        onClicked: {
                            if (checked && window.app) {
                                authorFilter.text = window.app.library.filterAuthor
                                dateFieldChoice.currentIndex = window.app.library.dateField === "saved" ? 1 : 0
                                dateFromFilter.text = window.app.library.dateFrom
                                dateToFilter.text = window.app.library.dateTo
                            }
                        }
                    }
                    FlatButton {
                        objectName: "galleryFilterReset"
                        text: "필터 초기화"
                        onClicked: {
                            searchDelay.stop()
                            search.text = ""
                            authorFilter.text = dateFromFilter.text = dateToFilter.text = ""
                            mediaFilter.currentIndex = orderChoice.currentIndex = dateFieldChoice.currentIndex = 0
                            window.app.library.resetFilters()
                        }
                    }
                    Item { Layout.fillWidth: true }
                    FlatButton {
                        objectName: "gallerySelectionToggle"
                        text: window.app && window.app.library.selectionMode ? "선택 끝내기" : "여러 개 선택"
                        enabled: window.app && !window.app.busy
                        onClicked: window.app.library.setSelectionMode(!window.app.library.selectionMode)
                    }
                }
                ColumnLayout {
                    id: filterBox
                    Layout.fillWidth: true
                    visible: !!window.app && filterToggle.checked
                    spacing: 6
                    function applyAdvancedFilter() {
                        if (window.app && window.app.library.setAdvancedFilters(authorFilter.text,
                                dateFieldChoice.currentIndex === 1 ? "saved" : "posted",
                                dateFromFilter.text, dateToFilter.text))
                            filterToggle.checked = false
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FlatField {
                            id: authorFilter
                            objectName: "galleryAuthorFilter"
                            Layout.fillWidth: true
                            placeholderText: "작성자 ID (정확히 일치)"
                            Accessible.name: "정확한 작성자 ID"
                            selectByMouse: true
                            maximumLength: 16
                            onAccepted: filterBox.applyAdvancedFilter()
                        }
                        FlatCombo {
                            id: dateFieldChoice
                            objectName: "galleryDateField"
                            model: ["게시일", "저장일"]
                            implicitWidth: 112
                            Accessible.name: "날짜 필터 기준"
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FlatField {
                            id: dateFromFilter
                            objectName: "galleryDateFrom"
                            Layout.fillWidth: true
                            placeholderText: "시작 YYYY-MM-DD"
                            Accessible.name: "시작 날짜 YYYY-MM-DD"
                            selectByMouse: true
                            maximumLength: 10
                            onAccepted: filterBox.applyAdvancedFilter()
                        }
                        Label { text: "~" }
                        FlatField {
                            id: dateToFilter
                            objectName: "galleryDateTo"
                            Layout.fillWidth: true
                            placeholderText: "종료 YYYY-MM-DD"
                            Accessible.name: "종료 날짜 YYYY-MM-DD"
                            selectByMouse: true
                            maximumLength: 10
                            onAccepted: filterBox.applyAdvancedFilter()
                        }
                        FlatButton {
                            objectName: "galleryFilterApply"
                            text: "적용"
                            onClicked: filterBox.applyAdvancedFilter()
                        }
                    }
                }
                Label {
                    objectName: "galleryActiveFilters"
                    Layout.fillWidth: true
                    text: window.app ? window.app.library.filterSummary : ""
                    visible: text.length > 0
                    wrapMode: Text.WordWrap
                    textFormat: Text.PlainText
                    color: window.mutedColor
                }
                Label {
                    Layout.fillWidth: true
                    text: window.app && !window.app.library.busy ? window.app.library.status : ""
                    visible: text.length > 0
                    wrapMode: Text.WordWrap
                    textFormat: Text.PlainText
                    color: window.noticeColor
                }
                Label {
                    objectName: "galleryManagementMessage"
                    Layout.fillWidth: true
                    text: window.app ? (window.app.management.error || window.app.management.status) : ""
                    visible: text.length > 0
                    wrapMode: Text.WordWrap
                    textFormat: Text.PlainText
                    color: window.app && window.app.management.error ? window.errorColor : window.mutedColor
                }
                RowLayout {
                    objectName: "gallerySelectionBar"
                    Layout.fillWidth: true
                    visible: window.app && window.app.library.selectionMode
                    spacing: 8
                    Label {
                        Layout.fillWidth: true
                        text: window.app ? window.app.library.selectedCount.toLocaleString() + "개 선택" : ""
                    }
                    FlatButton {
                        objectName: "gallerySelectLoaded"
                        text: "불러온 항목 선택"
                        enabled: window.app && !window.app.busy
                        onClicked: window.app.library.selectLoaded()
                    }
                    FlatButton {
                        objectName: "gallerySelectionClear"
                        text: "선택 해제"
                        onClicked: window.app.library.clearSelection()
                    }
                    FlatButton {
                        objectName: "galleryRecycleSelected"
                        text: "휴지통으로"
                        enabled: window.app && !window.app.busy && window.app.library.selectedCount > 0
                        onClicked: window.app.management.prepareDelete(window.app.library.selectedKeys)
                    }
                }

                GridView {
                    id: gallery
                    objectName: "galleryGrid"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: window.galleryModel
                    currentIndex: -1
                    cellWidth: Math.max(1, Math.floor(width / Math.max(1, Math.floor(width / window.tileWidth))))
                    cellHeight: Math.round(window.tileWidth * 0.75) + 48
                    onCellHeightChanged: {
                        window.stopWheel()
                        window.placeAnchor()
                        Qt.callLater(window.updateFocus)
                    }
                    onCellWidthChanged: {
                        window.stopWheel()
                        Qt.callLater(window.updateFocus)
                    }
                    onWidthChanged: {
                        window.stopWheel()
                        Qt.callLater(window.updateFocus)
                    }
                    onHeightChanged: {
                        window.stopWheel()
                        Qt.callLater(window.updateFocus)
                    }
                    onOriginYChanged: {
                        window.stopWheel()
                        Qt.callLater(window.updateFocus)
                    }
                    cacheBuffer: cellHeight
                    reuseItems: true
                    boundsBehavior: Flickable.StopAtBounds
                    keyNavigationEnabled: true
                    keyNavigationWraps: false
                    highlightMoveDuration: 0
                    focus: window.currentScreen === 1
                    ScrollBar.vertical: ScrollBar {
                        objectName: "galleryScrollBar"
                        policy: gallery.contentHeight > gallery.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                        onPressedChanged: if (pressed) window.stopWheel()
                    }
                    GalleryWheel {
                        id: wheelMotion
                        objectName: "galleryWheel"
                        view: gallery
                        inputFilter: window.wheelInputFilter
                        enabled: window.currentScreen === 1 && !window.modalOpen && (!window.app || !window.app.closing)
                    }
                    onDraggingChanged: if (dragging) window.stopWheel()
                    onContentYChanged: {
                        window.requestMore()
                        window.updateFocus()
                        if (window.sampleOnScroll)
                            window.sampleVisible()
                    }
                    onCountChanged: {
                        if (count === 0)
                            window.stopWheel()
                        window.focusReady = false
                        Qt.callLater(function() {
                            window.requestMore()
                            window.updateFocus()
                        })
                    }
                    onCurrentItemChanged: window.refreshSelection(currentItem)
                    Keys.onPressed: (event) => {
                        window.stopWheel()
                        event.accepted = false
                    }
                    Keys.onReturnPressed: {
                        window.stopWheel()
                        if (window.app && window.app.library.selectionMode && currentItem)
                            window.app.library.toggleSelection(currentItem.key)
                        else if (currentIndex >= 0)
                            previewDialog.open()
                    }
                    Keys.onEscapePressed: {
                        window.stopWheel()
                        if (window.app && window.app.library.selectionMode)
                            window.app.library.setSelectionMode(false)
                        currentIndex = -1
                    }

                    delegate: Item {
                        id: tile
                        objectName: "galleryTile"
                        required property int index
                        required property string key
                        required property string caption
                        required property string thumbUrl
                        required property bool thumbReady
                        property bool activeTile: true
                        readonly property bool batchSelected: window.app && window.app.library.selectedKeys.indexOf(tile.key) >= 0
                        readonly property bool imageReady: photo.status === Image.Ready
                        width: gallery.cellWidth
                        height: gallery.cellHeight
                        GridView.onPooled: activeTile = false
                        GridView.onReused: activeTile = true
                        Component.onCompleted: {
                            window.liveDelegates++
                            window.peakDelegates = Math.max(window.peakDelegates, window.liveDelegates)
                        }
                        Component.onDestruction: window.liveDelegates--

                        Rectangle {
                            anchors.fill: parent
                            anchors.margins: 6
                            radius: 10
                            color: tile.batchSelected || gallery.currentIndex === tile.index ? "#241416"
                                : (tileMouse.containsMouse ? "#212121" : window.cardColor)
                            border.width: tile.batchSelected || gallery.currentIndex === tile.index ? 1 : 0
                            border.color: window.accentColor
                            Image {
                                id: photo
                                anchors.top: parent.top
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.margins: 8
                                height: Math.round(window.tileWidth * 0.75) - 16
                                source: tile.activeTile && tile.thumbReady ? tile.thumbUrl : ""
                                sourceSize.width: window.scheduledThumbSize()[0]
                                sourceSize.height: window.scheduledThumbSize()[1]
                                asynchronous: true
                                cache: false
                                fillMode: Image.PreserveAspectFit
                            }
                            Label {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.margins: 10
                                text: tile.caption
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: 12
                                color: window.mutedColor
                            }
                            MouseArea {
                                id: tileMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onPressed: window.stopWheel()
                                onClicked: mouse => {
                                    gallery.currentIndex = tile.index
                                    gallery.forceActiveFocus()
                                    if (window.app && (window.app.library.selectionMode || (mouse.modifiers & Qt.ControlModifier)))
                                        window.app.library.toggleSelection(tile.key)
                                }
                                onDoubleClicked: if (!window.app || !window.app.library.selectionMode) previewDialog.open()
                            }
                            CheckBox {
                                objectName: "galleryItemSelection"
                                anchors.left: parent.left
                                anchors.top: parent.top
                                z: 2
                                visible: window.app && window.app.library.selectionMode
                                enabled: window.app && window.app.library.canSelect(tile.key)
                                checked: tile.batchSelected
                                Accessible.name: "사진 선택: " + tile.caption
                                onClicked: window.app.library.toggleSelection(tile.key)
                            }
                        }
                        Accessible.role: Accessible.ListItem
                        Accessible.name: caption
                        Accessible.selected: batchSelected || gallery.currentIndex === index
                        Accessible.onPressAction: {
                            gallery.currentIndex = index
                            if (window.app && window.app.library.selectionMode)
                                window.app.library.toggleSelection(key)
                        }
                    }

                    Label {
                        objectName: "galleryEmptyState"
                        anchors.centerIn: parent
                        visible: gallery.count === 0 && !window.galleryModel.busy && !window.galleryModel.error.length
                        text: search.text.length || (window.app && (window.app.library.filterSummary.length > 0
                            || mediaFilter.currentIndex > 0)) ? "필터에 맞는 사진이 없습니다." : "사진이 없습니다."
                        color: window.mutedColor
                    }
                }
            }
            ManagementPanel {
                app: window.app
            }
        }
    }

    Timer {
        id: searchDelay
        interval: 180
        onTriggered: {
            if (window.app)
                window.app.library.search(search.text)
            else
                window.galleryModel.search(search.text)
        }
    }
    Timer {
        interval: 100
        running: !window.app
        repeat: true
        onTriggered: {
            window.sampleVisible()
            if (!window.focusReady && window.currentScreen === 1)
                window.updateFocus()
        }
    }
    Shortcut {
        sequence: "Ctrl+F"
        enabled: window.currentScreen === 1
        onActivated: {
            search.forceActiveFocus()
            search.selectAll()
        }
    }
    Dialog {
        id: previewDialog
        objectName: "previewDialog"
        parent: Overlay.overlay
        property bool detailsVisible: false
        title: "미리보기"
        anchors.centerIn: parent
        width: Math.max(0, parent.width - 24)
        height: Math.max(0, parent.height - 24)
        padding: 16
        modal: true
        focus: true
        // A modal popup blocks shortcuts owned by the window underneath it.
        // Keep navigation in this popup's scope, including toolbar focus.
        Shortcut {
            sequence: "Left"
            enabled: previewDialog.visible && !bodyText.activeFocus && window.app && window.app.library.canPrevious
            onActivated: window.app.library.previousItem()
        }
        Shortcut {
            sequence: "Right"
            enabled: previewDialog.visible && !bodyText.activeFocus && window.app && window.app.library.canNext
            onActivated: window.app.library.nextItem()
        }
        Shortcut {
            sequence: "+"
            enabled: previewDialog.visible && !bodyText.activeFocus && selectedMedia.canZoomIn
            onActivated: selectedMedia.zoomIn()
        }
        Shortcut {
            sequence: "="
            enabled: previewDialog.visible && !bodyText.activeFocus && selectedMedia.canZoomIn
            onActivated: selectedMedia.zoomIn()
        }
        Shortcut {
            sequence: "-"
            enabled: previewDialog.visible && !bodyText.activeFocus && selectedMedia.canZoomOut
            onActivated: selectedMedia.zoomOut()
        }
        Shortcut {
            sequence: "0"
            enabled: previewDialog.visible && !bodyText.activeFocus && selectedMedia.ready
            onActivated: selectedMedia.fit()
        }
        Shortcut {
            sequence: "1"
            enabled: previewDialog.visible && !bodyText.activeFocus && selectedMedia.canActualSize
            onActivated: selectedMedia.actualSize()
        }
        onAboutToShow: {
            detailsVisible = false
            window.stopWheel()
        }
        onClosed: selectedMedia.release()
        background: Rectangle {
            color: "#141414"
            radius: 16
            border.width: 1
            border.color: "#2a2a2a"
        }
        header: RowLayout {
            spacing: 8
            Label {
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.topMargin: 12
                Layout.bottomMargin: 4
                text: previewDialog.title
                font.pixelSize: 18
            }
            FlatButton {
                objectName: "previewMaximizeButton"
                Layout.rightMargin: 12
                text: window.visibility === Window.Maximized || window.visibility === Window.FullScreen
                    ? "창 복원" : "창 최대화"
                onClicked: {
                    if (window.visibility === Window.Maximized || window.visibility === Window.FullScreen)
                        window.showNormal()
                    else
                        window.showMaximized()
                }
            }
        }
        footer: DialogButtonBox {
            background: null
            FlatButton {
                objectName: "previewDetailsButton"
                visible: window.app && window.app.library.selectedBody.length > 0
                text: previewDialog.detailsVisible ? "본문 접기" : "본문 보기"
                checkable: true
                checked: previewDialog.detailsVisible
                DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
                onClicked: previewDialog.detailsVisible = !previewDialog.detailsVisible
            }
            FlatButton {
                text: "닫기"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onRejected: previewDialog.close()
        }
        contentItem: ColumnLayout {
            spacing: 12
            Flow {
                Layout.fillWidth: true
                spacing: 8
                FlatButton {
                    objectName: "previewPreviousButton"
                    text: "이전"
                    enabled: window.app && window.app.library.canPrevious
                    onClicked: window.app.library.previousItem()
                }
                FlatButton {
                    objectName: "previewNextButton"
                    text: "다음"
                    enabled: window.app && window.app.library.canNext
                    onClicked: window.app.library.nextItem()
                }
                PreviewZoomControls { preview: selectedMedia }
            }
            MediaPreview {
                id: selectedMedia
                objectName: "selectedMedia"
                Layout.fillWidth: true
                Layout.fillHeight: true
                sourceUrl: window.app ? window.app.library.selectedPreviewUrl : window.selectedThumb
                mediaType: window.app ? window.app.library.selectedMediaType : "photo"
                sourcePixelWidth: window.app ? window.app.library.selectedPixelWidth : 0
                sourcePixelHeight: window.app ? window.app.library.selectedPixelHeight : 0
                sourceRawWidth: window.app ? window.app.library.selectedRawWidth : 0
                sourceRawHeight: window.app ? window.app.library.selectedRawHeight : 0
                playing: previewDialog.visible
            }
            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: window.app && window.app.library.selectedAuthor ? "@" + window.app.library.selectedAuthor : ""
                textFormat: Text.PlainText
                font.bold: true
                elide: Text.ElideRight
            }
            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: window.app ? window.app.library.selectedPublicInfo : ""
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                color: window.mutedColor
                font.pixelSize: 12
            }
            ScrollView {
                id: bodyScroll
                objectName: "previewBodyScroll"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(140, previewDialog.availableHeight * 0.25,
                                                Math.max(1, bodyText.implicitHeight))
                visible: previewDialog.detailsVisible && window.app && window.app.library.selectedBody.length > 0
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: bodyText.implicitHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                TextArea {
                    id: bodyText
                    width: bodyScroll.availableWidth
                    text: window.app ? window.app.library.selectedBody : ""
                    textFormat: TextEdit.PlainText
                    readOnly: true
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    color: "#f4f1f1"
                    selectionColor: window.accentColor
                    selectedTextColor: "white"
                    font.pixelSize: 13
                    background: null
                }
            }
            Label {
                Layout.fillWidth: true
                text: window.app ? window.app.library.status : ""
                textFormat: Text.PlainText
                visible: text.length > 0
                wrapMode: Text.WordWrap
                color: window.mutedColor
            }
            Label {
                visible: window.app && text.length > 0 && !window.app.library.busy
                text: window.app ? window.app.library.selectedMissingNote : ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                color: window.mutedColor
            }
            Flow {
                visible: !!window.app
                Layout.fillWidth: true
                spacing: 8
                FlatButton { text: "파일 열기"; enabled: window.app && !window.app.library.selectedMissing; onClicked: window.app.library.openSelected() }
                FlatButton { text: "폴더"; enabled: window.app && !window.app.library.selectedMissing; onClicked: window.app.library.revealSelected() }
                FlatButton {
                    objectName: "previewRecycle"
                    text: "휴지통으로"
                    enabled: window.app && !window.app.busy && !window.app.library.selectedMissing
                    onClicked: {
                        const key = window.app.library.selectedKey
                        selectedMedia.release()
                        previewDialog.close()
                        window.app.management.prepareDelete([key])
                    }
                }
                FlatButton { text: "원문 보기"; enabled: window.app && window.app.library.selectedSource.length > 0; onClicked: window.app.library.openSource() }
            }
        }
    }

    FolderDialog {
        id: folderDialog
        objectName: "saveFolderDialog"
        title: "사진을 저장할 폴더"
        // Qt's own folder dialog stays on the drive it opened on, so a folder
        // on D: made C: unreachable. The Windows dialog lists every drive.
        onAccepted: if (window.app) window.app.setOutputFolder(selectedFolder.toString())
    }

    DeleteConfirmation {
        id: deleteConfirmation
        parent: Overlay.overlay
        app: window.app
    }

    Connections {
        target: window.app ? window.app.management : null
        function onDeleteConfirmationRequested() {
            selectedMedia.release()
            previewDialog.close()
            Qt.callLater(function() {
                if (window.app && !window.app.closing && window.app.management.deleteConfirmationPending)
                    deleteConfirmation.open()
            })
        }
    }

    function chooseSaveFolder() {
        if (!window.app)
            return
        const current = window.app.outputUrl
        if (current && String(current).length > 0)
            folderDialog.currentFolder = current
        folderDialog.open()
    }

    ConnectionDialog {
        id: connectionDialog
        objectName: "connectionDialog"
        parent: Overlay.overlay
        accounts: window.app ? window.app.accounts : null
        app: window.app
    }

    PreferencesDialog {
        id: preferencesDialog
        parent: Overlay.overlay
        app: window.app
    }

    SaveReviewDialog {
        id: saveReviewDialog
        objectName: "saveReviewDialog"
        parent: Overlay.overlay
        app: window.app
    }

    Dialog {
        id: storageDialog
        parent: Overlay.overlay
        title: "저장 위치 확인"
        anchors.centerIn: parent
        width: Math.min(window.width - 48, 440)
        modal: true
        closePolicy: Popup.NoAutoClose
        background: Rectangle {
            color: "#141414"
            radius: 16
            border.width: 1
            border.color: "#2a2a2a"
        }
        header: Label {
            text: storageDialog.title
            leftPadding: 24
            rightPadding: 24
            topPadding: 20
            bottomPadding: 4
            font.pixelSize: 18
            background: null
        }
        footer: DialogButtonBox {
            background: null
            FlatButton {
                text: "계속"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
        onAccepted: if (window.app) window.app.confirmStorage(true)
        onRejected: if (window.app) window.app.confirmStorage(false)
        Label {
            width: parent.width
            text: window.app ? window.app.confirmation : ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
    }

    Dialog {
        id: repairDialog
        objectName: "repairDialog"
        parent: Overlay.overlay
        title: "기록 복구"
        anchors.centerIn: parent
        width: Math.min(window.width - 48, 460)
        modal: true
        closePolicy: Popup.CloseOnEscape
        background: Rectangle {
            color: "#141414"
            radius: 16
            border.width: 1
            border.color: "#2a2a2a"
        }
        header: Label {
            text: repairDialog.title
            leftPadding: 24
            rightPadding: 24
            topPadding: 20
            bottomPadding: 4
            font.pixelSize: 18
            background: null
        }
        footer: DialogButtonBox {
            background: null
            FlatButton {
                text: "복구"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
        onAccepted: if (window.app) window.app.repairRecords()
        Label {
            width: parent.width
            text: "손상된 기록 DB를 고칩니다.\n\n· 손상된 파일은 먼저 이 PC에 백업해 둡니다.\n" +
                  "· 읽을 수 있는 기록과 최근 백업으로 기록 DB를 다시 만듭니다.\n" +
                  "· 사진·영상과 중복 방지 기록은 건드리지 않습니다."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
    }

    Connections {
        target: window.app ? window.app.library : null
        function onChanged() {
            const index = window.app.library.selectedIndex
            if (index >= 0 && gallery.currentIndex !== index) {
                gallery.currentIndex = index
                if (previewDialog.visible)
                    gallery.positionViewAtIndex(index, GridView.Contain)
            }
        }
    }

    Connections {
        target: window.app
        function onChanged() {
            if (window.app.closing) {
                selectedMedia.release()
                previewDialog.close()
                connectionDialog.close()
                preferencesDialog.close()
                storageDialog.close()
                repairDialog.close()
                folderDialog.close()
                deleteConfirmation.close()
            }
        }
        function onLoginRequested() { connectionDialog.open() }
        function onRetryStarted() { window.currentScreen = 0 }
        function onStorageConfirmationRequested() { storageDialog.open() }
        function onReviewReady() { saveReviewDialog.open() }
        function onReviewCleared() { saveReviewDialog.close() }
    }
}
