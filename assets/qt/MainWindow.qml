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
    width: 1280
    height: 860
    minimumWidth: 680
    minimumHeight: 480
    // Grown only while a saved-file detail card needs more room than the current window.
    property bool saveFitReady: false
    property bool applyingSaveHeight: false
    property bool saveHeightExpanded: false
    property int saveRestoreHeight: 0
    property int fittedSaveNeed: 0
    property bool saveHeightHeld: false
    property string fittedDetailName: ""
    property int appliedSaveHeight: 0
    readonly property bool savedDetailVisible: !!(app && app.download && app.download.detailsEnabled
        && app.download.currentSavedItem && app.download.currentSavedItem.filename)
    visible: true
    readonly property var app: appController
    readonly property bool modalOpen: previewDialog.visible || connectionDialog.visible ||
        preferencesDialog.visible || saveReviewDialog.visible || storageDialog.visible || repairDialog.visible ||
        folderDialog.visible || deleteConfirmation.visible || scheduleDialog.visible ||
        runHistoryDialog.visible || unsavedDialog.visible || datePickerPopup.visible || statisticsDialog.visible ||
        tagBatchPopup.visible || aiClearConfirm.visible || teachPopup.visible || shortcutHelp.visible
    // A focused or open list takes letters for itself (jumping to "@bob" must not
    // toggle the favorites filter), so the single-letter shortcuts wait.
    readonly property bool listEngaged: [thumbSizeChoice, mediaFilter, orderChoice, authorFilter, hashtagFilter,
                                         userTagFilter, dateFieldChoice].some(
        function(combo) { return combo.activeFocus || combo.popup.visible })
    // An open menu is in the same position: a letter typed over it is not meant for the grid.
    readonly property bool menuOpen: aiMenu.visible || teachMenu.visible
    // Duplicates are reviewed group by group, not in the flat grid.
    readonly property bool duplicateMode: !!app && app.library.filterDuplicateOnly
    property bool duplicateNear: false
    readonly property bool letterKeysFree: currentScreen === 1 && !modalOpen && !search.activeFocus
        && !listEngaged && !menuOpen && !!app
    title: app ? "LuvBox · " + app.version : "LuvBox · 갤러리"
    color: Theme.window
    // The window is lit like the startup screen: a little lighter at the top, with
    // that screen's red light falling in from above. While a save runs on the
    // save screen the light breathes, as it does while the app is starting.
    readonly property bool lightBreathing: !!app && app.download.running && currentScreen === 0 && active
    background: Rectangle {
        gradient: Gradient {
            GradientStop { position: 0; color: Theme.windowTop }
            GradientStop { position: 0.5; color: Theme.window }
        }
        Glow {
            id: windowLight
            objectName: "windowLight"
            property real pulse: 1
            anchors.horizontalCenter: parent.horizontalCenter
            // A pool of light under the top edge, not a band across it.
            width: Math.min(parent.width * 1.3, 1700)
            height: Math.min(parent.height * 0.68, 520)
            strength: 0.3
            opacity: pulse
            SequentialAnimation {
                running: window.lightBreathing
                loops: Animation.Infinite
                NumberAnimation { target: windowLight; property: "pulse"; to: 0.5; duration: 1700; easing.type: Easing.InOutSine }
                NumberAnimation { target: windowLight; property: "pulse"; to: 1; duration: 1700; easing.type: Easing.InOutSine }
                onRunningChanged: if (!running) windowLight.pulse = 1
            }
        }
    }
    // Stock controls (tooltips, scroll bars, the busy ring) take their colors from here.
    palette.window: color
    palette.windowText: Theme.text
    palette.base: Theme.well
    palette.alternateBase: Theme.field
    palette.text: Theme.text
    palette.button: Theme.raised
    palette.buttonText: Theme.text
    palette.highlight: Theme.accent
    palette.highlightedText: "white"
    palette.placeholderText: Theme.textMuted
    palette.light: Theme.lineStrong
    palette.midlight: Theme.raisedHover
    palette.mid: Theme.lineStrong
    palette.dark: Theme.window
    palette.shadow: "black"
    palette.toolTipBase: Theme.raised
    palette.toolTipText: Theme.text
    palette.link: Theme.link
    palette.linkVisited: Theme.accentText
    palette.disabled.text: Theme.textDisabled
    palette.disabled.buttonText: Theme.textDisabled
    palette.disabled.windowText: Theme.textDisabled
    property string uiFontFamily: "Pretendard"
    font.family: uiFontFamily
    font.pixelSize: Theme.fontBody
    font.weight: Font.Normal
    font.hintingPreference: Font.PreferVerticalHinting
    property int currentScreen: 0
    onCurrentScreenChanged: {
        stopWheel()
        datePickerPopup.close()
        if (app)
            app.galleryVisible(currentScreen === 1)
        if (currentScreen === 1)
            Qt.callLater(updateFocus)
        Qt.callLater(fitSaveWindow)
    }
    onSavedDetailVisibleChanged: Qt.callLater(fitSaveWindow)
    onHeightChanged: {
        if (applyingSaveHeight || !saveFitReady || height === appliedSaveHeight)
            return
        // The person resized the window. Keep that choice until the open detail changes.
        saveHeightHeld = true
    }
    Component.onCompleted: {
        // The thumbnail size chosen last time, before the first tiles are laid out.
        const remembered = app ? app.preferences.gallery_density : undefined
        if (remembered === 0 || remembered === 1 || remembered === 2)
            densityChoice = remembered
        Qt.callLater(function() {
            saveFitReady = true
            fitSaveWindow()
        })
    }

    function screenHeightLimit() {
        // The virtual desktop spans every monitor. Grow only within the screen this window is on.
        let available = Screen.desktopAvailableHeight
        const geometry = window.screen ? window.screen.availableGeometry : null
        if (geometry && geometry.height > 0)
            available = Math.min(available, geometry.height)
        if (available < minimumHeight)
            return height
        return Math.max(minimumHeight, available - 48)
    }

    function saveNeededHeight() {
        // The window height at which the save form fits, plus a few pixels so the scrollbar stays hidden.
        return height + (saveScroll.contentHeight - saveScroll.height) + 8
    }

    function fitSaveWindow() {
        if (!saveFitReady || currentScreen !== 0 || applyingSaveHeight || saveScroll.contentHeight < 40)
            return
        const detailName = savedDetailVisible ? (app.download.currentSavedItem.filename || "") : ""
        if (!savedDetailVisible) {
            fittedSaveNeed = 0
            fittedDetailName = ""
            saveHeightHeld = false
            if (saveHeightExpanded && saveRestoreHeight >= minimumHeight) {
                appliedSaveHeight = Math.min(saveRestoreHeight, screenHeightLimit())
                applyingSaveHeight = true
                height = appliedSaveHeight
                applyingSaveHeight = false
                saveHeightExpanded = false
            }
            return
        }
        if (saveHeightHeld && detailName === fittedDetailName)
            return
        saveHeightHeld = false
        fittedDetailName = detailName
        const needed = saveNeededHeight()
        const limit = screenHeightLimit()
        if (needed > height + 4 && needed > fittedSaveNeed + 4) {
            const next = Math.min(Math.max(needed, minimumHeight), limit)
            if (next > height + 4) {
                if (!saveHeightExpanded)
                    saveRestoreHeight = height
                saveHeightExpanded = true
                appliedSaveHeight = next
                applyingSaveHeight = true
                height = next
                applyingSaveHeight = false
            }
            fittedSaveNeed = needed
            return
        }
        if (saveHeightExpanded && needed + 4 < height && needed + 4 < fittedSaveNeed) {
            const next = Math.max(saveRestoreHeight, Math.min(needed, limit))
            if (next + 4 < height) {
                appliedSaveHeight = next
                applyingSaveHeight = true
                height = next
                applyingSaveHeight = false
                if (height <= saveRestoreHeight + 4)
                    saveHeightExpanded = false
            }
            fittedSaveNeed = needed
        }
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
            if (!app.closing) {
                // The height grown for a saved-file card is temporary; keep the person's own.
                const own = saveHeightExpanded && saveRestoreHeight >= minimumHeight ? saveRestoreHeight : height
                app.rememberWindow(x, y, width, own, visibility === Window.Maximized || visibility === Window.FullScreen)
            }
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

    // Apply one changed filter and keep the others as they are applied now.
    // A date range stays in order: the date just chosen wins over the other end.
    function applyFilters(changes) {
        if (!app)
            return
        const library = app.library
        const hasAdvancedChange = changes.author !== undefined || changes.dateField !== undefined
            || changes.from !== undefined || changes.to !== undefined || changes.hashtag !== undefined
        if (changes.userTag !== undefined && !hasAdvancedChange) {
            library.setUserTag(changes.userTag)
            return
        }
        const author = changes.author !== undefined ? changes.author : library.filterAuthor
        const dateField = changes.dateField !== undefined ? changes.dateField : library.dateField
        let from = changes.from !== undefined ? changes.from : library.dateFrom
        let to = changes.to !== undefined ? changes.to : library.dateTo
        if (from.length > 0 && to.length > 0 && from > to) {
            if (changes.from !== undefined)
                to = from
            else
                from = to
        }
        const hashtag = changes.hashtag !== undefined ? changes.hashtag : library.filterHashtag
        if (changes.userTag !== undefined)
            library.setUserTag(changes.userTag)
        library.setAdvancedFilters(author, dateField, from, to, hashtag)
    }

    property string transientNotice: ""
    // The gallery has one status line. The library (scans, selection notes) and file
    // management (moves to the Recycle Bin) both report there; the one whose text
    // changed last is shown, so an old result never hides what is happening now.
    property string statusSource: ""
    property string seenLibraryStatus: ""
    property string seenManagementStatus: ""

    Connections {
        target: window.app ? window.app.library : null
        function onChanged() {
            const text = window.app.library.status
            if (text === window.seenLibraryStatus)
                return
            window.seenLibraryStatus = text
            if (text.length > 0)
                window.statusSource = "library"
        }
    }
    Connections {
        target: window.app ? window.app.management : null
        function onChanged() {
            const text = window.app.management.error || window.app.management.status
            if (text === window.seenManagementStatus)
                return
            window.seenManagementStatus = text
            if (text.length > 0)
                window.statusSource = "management"
        }
    }

    function flashNotice(text) {
        transientNotice = text
        noticeTimer.restart()
    }

    function teachCategory(category, selection) {
        if (!app)
            return
        const started = selection ? app.library.teachSelection(category) : app.library.teachSelected(category)
        if (started) {
            const name = category.length ? category : "어느 쪽도 아님"
            flashNotice(name + " 분류를 저장하는 중입니다. 매우 닮은 사진도 함께 바뀝니다.")
        }
    }

    Timer {
        id: noticeTimer
        interval: 3500
        onTriggered: window.transientNotice = ""
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
        height: Theme.titleBarHeight
        // See-through, so the window's light passes behind it; only the line under it is drawn.
        background: Item {
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: Qt.rgba(1, 1, 1, 0.08)
            }
        }
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.pageMargin
            anchors.rightMargin: Theme.pageMargin
            spacing: Theme.gapSmall
            Image {
                source: "../icons/luvbox.png"
                Layout.preferredWidth: 26
                Layout.preferredHeight: 26
                sourceSize: Qt.size(26, 26)
                fillMode: Image.PreserveAspectFit
                // The heart keeps a little of its own light, as on the startup screen.
                Glow {
                    z: -1
                    anchors.centerIn: parent
                    width: 170
                    height: 120
                    focusY: 0.5
                    strength: 0.34
                }
            }
            Label {
                text: "LuvBox"
                font.pixelSize: Theme.fontHeading
                font.bold: true
                Layout.rightMargin: Theme.gapLarge
            }
            FlatButton {
                text: "저장"
                tone: "nav"
                checkable: true
                checked: window.currentScreen === 0
                onClicked: {
                    window.currentScreen = 0
                    checked = Qt.binding(function() { return window.currentScreen === 0 })
                }
            }
            FlatButton {
                text: "갤러리"
                tone: "nav"
                checkable: true
                checked: window.currentScreen === 1
                onClicked: {
                    window.currentScreen = 1
                    checked = Qt.binding(function() { return window.currentScreen === 1 })
                }
            }
            Item { Layout.fillWidth: true }
            Label {
                text: window.app ? window.app.version : ""
                color: Theme.textFaint
                font.pixelSize: Theme.fontSmall
                visible: window.width > 760
                Layout.rightMargin: Theme.gapTiny
            }
            // The two gallery windows keep the places they had: one click, no menu.
            FlatButton {
                objectName: "galleryStatisticsButton"
                text: "통계"
                tone: "ghost"
                visible: !!window.app && window.currentScreen === 1
                enabled: window.app && !window.app.closing
                tip: "저장한 사진과 작성자·태그 요약 (S)"
                onClicked: statisticsDialog.open()
            }
            FlatButton {
                objectName: "shortcutHelpButton"
                text: "단축키"
                tone: "ghost"
                visible: !!window.app && window.currentScreen === 1
                enabled: window.app && !window.app.closing
                Accessible.name: "단축키 안내"
                tip: "갤러리와 미리보기에서 쓰는 단축키 (F1)"
                onClicked: shortcutHelp.open()
            }
            // Settings stay reachable during a save: its records are read-only, and the
            // changeable options are locked inside the dialog until the work ends.
            FlatButton {
                objectName: "settingsButton"
                text: "설정"
                visible: !!window.app
                enabled: window.app && !window.app.closing
                onClicked: preferencesDialog.open()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.pageMargin
        anchors.rightMargin: Theme.pageMargin
        anchors.topMargin: Theme.gapLarge
        anchors.bottomMargin: Theme.gapLarge
        spacing: Theme.gap

        Label {
            visible: !window.app
            text: "생성한 사진으로 화면을 확인합니다. 실제 계정·저장 폴더는 연결하지 않습니다."
            color: Theme.textMuted
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        UpdateNotice {
            id: updateNotice
            Layout.fillWidth: true
            app: window.app
            // Closing the window remembers its size and ends every part; the
            // launcher is started again only after that.
            onRestartRequested: if (window.app && window.app.prepareRestart()) window.close()
        }

        StackLayout {
            currentIndex: window.currentScreen
            Layout.fillWidth: true
            Layout.fillHeight: true

            FocusScrollView {
                id: saveScroll
                objectName: "saveScroll"
                onContentHeightChanged: Qt.callLater(window.fitSaveWindow)
                clip: true
                contentWidth: availableWidth
                contentHeight: saveForm.implicitHeight + 16
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                ColumnLayout {
                    id: saveForm
                    onImplicitHeightChanged: Qt.callLater(window.fitSaveWindow)
                    width: Math.max(1, Math.min(saveScroll.availableWidth - 24, 640))
                    height: implicitHeight
                    x: Math.max(0, (saveScroll.availableWidth - width) / 2)
                    y: 4
                    spacing: Theme.gapLarge
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.gapSmall
                        spacing: Theme.gapTiny
                        Label { text: "사진 저장"; font.pixelSize: Theme.fontTitle; font.bold: true }
                        Label { text: "좋아요한 사진과 영상을 내 폴더에."; color: Theme.textMuted }
                    }
                    Frame {
                        Layout.fillWidth: true
                        padding: Theme.cardPadding
                        visible: !!window.app
                        background: Card {}
                        ColumnLayout {
                            width: parent.width
                            spacing: Theme.gapSmall
                            Label { text: "X 계정"; color: Theme.textMuted; font.pixelSize: Theme.fontSmall }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.gapSmall
                                FlatCombo {
                                    Layout.fillWidth: true
                                    model: window.app ? window.app.accounts.accounts : []
                                    textRole: "handle"
                                    valueRole: "user_id"
                                    currentIndex: window.accountIndex()
                                    displayText: currentIndex >= 0 ? "@" + currentText : "연결 계정 선택"
                                    visible: count > 0
                                    enabled: window.app && !window.app.busy
                                    onActivated: {
                                        if (window.app)
                                            window.app.accounts.selectAccount(currentValue)
                                        currentIndex = Qt.binding(function() { return window.accountIndex() })
                                    }
                                    Accessible.name: "연결된 X 계정"
                                }
                                Label {
                                    Layout.fillWidth: true
                                    visible: !window.app || window.app.accounts.accounts.length === 0
                                    text: window.app && window.app.accounts.connected ? "연결 정보 저장됨" : "X 로그인을 연결해 주세요"
                                    color: Theme.textMuted
                                }
                                FlatButton {
                                    text: window.app && window.app.accounts.authenticationState === "reconnect_required" ? "다시 연결" : "X 연결"
                                    implicitHeight: Theme.inputHeight
                                    enabled: window.app && !window.app.busy
                                    onClicked: connectionDialog.open()
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: window.app && window.app.accounts.connected
                                text: window.app ? window.app.accounts.authenticationLabel : ""
                                font.pixelSize: Theme.fontSmall
                                color: window.app && window.app.accounts.authenticationState === "reconnect_required"
                                    ? Theme.error : Theme.textFaint
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
                                maximumLength: 16
                                validator: RegularExpressionValidator { regularExpression: /@?[A-Za-z0-9_]{1,15}/ }
                                selectByMouse: true
                                Accessible.name: "저장할 X 아이디"
                                onEditingFinished: if (window.app) window.app.setAccount(text)
                                onAccepted: if (window.app) window.app.setAccount(text)
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.topMargin: Theme.gapSmall
                                Layout.bottomMargin: Theme.gapTiny
                                implicitHeight: 1
                                color: Theme.line
                            }
                            Label { text: "저장 위치"; color: Theme.textMuted; font.pixelSize: Theme.fontSmall }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.gapSmall
                                Label {
                                    id: outputFolderLabel
                                    Layout.fillWidth: true
                                    text: window.app && window.app.outputFolder.length > 0
                                        ? window.app.outputFolder : "저장할 폴더를 선택해 주세요"
                                    color: window.app && window.app.outputFolder.length > 0
                                        ? Theme.text : Theme.textMuted
                                    textFormat: Text.PlainText
                                    elide: Text.ElideMiddle
                                    HoverHandler { id: outputFolderHover }
                                    ToolTip.text: outputFolderLabel.text
                                    ToolTip.delay: 500
                                    ToolTip.visible: outputFolderHover.hovered && outputFolderLabel.truncated
                                }
                                FlatButton { text: "폴더 선택"; enabled: window.app && !window.app.busy; onClicked: window.chooseSaveFolder() }
                                FlatButton {
                                    text: "열기"
                                    tone: "ghost"
                                    enabled: !!(window.app && window.app.outputFolder.length > 0)
                                    onClicked: if (window.app) window.app.openOutputFolder()
                                }
                            }
                        }
                    }
                    Frame {
                        Layout.fillWidth: true
                        visible: !!window.app
                        padding: Theme.cardPadding
                        background: Card {}
                        ColumnLayout {
                            width: parent.width
                            spacing: Theme.gap
                            Label {
                                id: saveStatus
                                text: window.app ? window.app.status : ""
                                textFormat: Text.PlainText
                                font.pixelSize: Theme.fontHeading
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                                color: window.app && window.app.error.length > 0 && text === window.app.error
                                    ? Theme.error : Theme.text
                            }
                            Label {
                                text: window.app ? window.app.error : ""
                                textFormat: Text.PlainText
                                visible: text.length > 0 && text !== saveStatus.text
                                color: Theme.error
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
                                visible: window.app && window.app.busy && !window.app.management.deleteConfirmationPending && (sliding || amount > 0)
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
                                    color: Theme.line
                                }
                                Rectangle {
                                    height: parent.height
                                    radius: height / 2
                                    color: Theme.accent
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
                                spacing: Theme.gap
                                Label {
                                    objectName: "saveCounters"
                                    Layout.fillWidth: true
                                    text: window.app ? "저장 " + window.app.download.savedCount.toLocaleString() +
                                        "개 · 이미 저장됨 " + window.app.download.skippedCount.toLocaleString() + "개" +
                                        (window.app.download.unsavedCount > 0
                                            ? " · 저장 못 함 " + window.app.download.unsavedCount.toLocaleString() + "개" : "") : ""
                                    color: Theme.textMuted
                                    wrapMode: Text.WordWrap
                                }
                                FlatSwitch {
                                    objectName: "savedDetailsToggle"
                                    text: "자세히 보기"
                                    checked: window.app ? window.app.preferences.detailed_progress : false
                                    enabled: window.app && !window.app.closing
                                    onToggled: {
                                        if (window.app)
                                            window.app.setPreference("detailed_progress", checked)
                                        checked = Qt.binding(function() {
                                            return window.app ? window.app.preferences.detailed_progress : false
                                        })
                                    }
                                }
                            }
                            Label {
                                text: window.app ? window.app.download.notice : ""
                                textFormat: Text.PlainText
                                visible: text.length > 0
                                color: Theme.notice
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
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
                                color: Theme.textMuted
                                textFormat: Text.PlainText
                                wrapMode: Text.WordWrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.gapSmall
                                FlatButton {
                                    objectName: "saveStartButton"
                                    text: window.app && window.app.accounts.authenticationState === "reconnect_required"
                                        ? "X 계정 다시 연결" : "저장 시작"
                                    tone: "primary"
                                    large: true
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
                                    large: true
                                    visible: window.app && window.app.download.running
                                    enabled: window.app && !window.app.download.stopping
                                    onClicked: window.app.download.stop()
                                }
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.topMargin: Theme.gapTiny
                                implicitHeight: 1
                                color: Theme.line
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                objectName: "saveScheduleSummary"
                                spacing: Theme.gap
                                Label {
                                    Layout.fillWidth: true
                                    text: !window.app ? "" : window.app.schedule.summary.indexOf("자동 저장") === 0
                                        ? window.app.schedule.summary : "자동 저장 · " + window.app.schedule.summary
                                    color: window.app && window.app.schedule.registered && !window.app.schedule.current
                                        ? Theme.notice : Theme.textMuted
                                    textFormat: Text.PlainText
                                    wrapMode: Text.WordWrap
                                }
                                FlatButton {
                                    objectName: "saveScheduleButton"
                                    text: "자동 저장 설정"
                                    visible: !!window.app
                                    onClicked: scheduleDialog.open()
                                }
                            }
                            RowLayout {
                                objectName: "saveUnsavedSummary"
                                Layout.fillWidth: true
                                visible: !!window.app && window.app.records.unsavedCount > 0
                                spacing: Theme.gap
                                Label {
                                    Layout.fillWidth: true
                                    text: window.app ? "저장하지 못한 미디어 " + window.app.records.unsavedCount.toLocaleString()
                                        + "개가 있습니다." : ""
                                    color: Theme.notice
                                    wrapMode: Text.WordWrap
                                }
                                FlatButton {
                                    objectName: "saveUnsavedButton"
                                    text: "확인"
                                    onClicked: unsavedDialog.open()
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
                spacing: Theme.gapSmall
                // Row 1: what to look for and how the grid shows it.
                RowLayout {
                    objectName: "gallerySearchRow"
                    spacing: Theme.gapSmall
                    Layout.fillWidth: true
                    FlatField {
                        id: search
                        objectName: "searchBox"
                        placeholderText: "사진 검색"
                        Accessible.name: "사진 검색"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 120
                        selectByMouse: true
                        rightPadding: clearSearchButton.visible ? 36 : 14
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
                        Keys.onEscapePressed: event => {
                            if (text.length > 0) {
                                text = ""
                                window.stopWheel()
                                searchDelay.stop()
                                if (window.app)
                                    window.app.library.search("")
                                else
                                    window.galleryModel.search("")
                                event.accepted = true
                            } else {
                                gallery.forceActiveFocus()
                                event.accepted = true
                            }
                        }
                        ToolButton {
                            id: clearSearchButton
                            objectName: "searchClearButton"
                            anchors.right: parent.right
                            anchors.rightMargin: 6
                            anchors.verticalCenter: parent.verticalCenter
                            width: 24
                            height: 24
                            visible: search.text.length > 0
                            focusPolicy: Qt.NoFocus
                            Accessible.name: "검색어 지우기"
                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                            contentItem: Text {
                                text: "×"
                                font.pixelSize: Theme.fontSection
                                font.bold: true
                                color: clearSearchButton.hovered ? Theme.text : Theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: Theme.radiusCard
                                color: clearSearchButton.hovered ? Theme.raisedHover : "transparent"
                            }
                            onClicked: {
                                search.text = ""
                                window.stopWheel()
                                searchDelay.stop()
                                if (window.app)
                                    window.app.library.search("")
                                else
                                    window.galleryModel.search("")
                                search.forceActiveFocus()
                            }
                        }
                    }
                    FlatCombo {
                        id: orderChoice
                        objectName: "galleryOrderChoice"
                        visible: !!window.app
                        label: "정렬"
                        model: ["최근 저장", "최신 게시물", "오래된 게시물", "좋아요 순"]
                        enabled: !window.duplicateMode
                        implicitWidth: 172
                        Accessible.name: "사진 정렬"
                        onActivated: window.app.library.setFilters(
                            ["all", "photo", "video", "animated_gif"][mediaFilter.currentIndex],
                            ["saved", "posted", "posted_oldest", "liked"][currentIndex], window.app.library.filterAuthor)
                    }
                    FlatCombo {
                        id: mediaFilter
                        objectName: "galleryMediaFilter"
                        visible: !!window.app
                        label: "종류"
                        model: ["전체", "사진", "영상", "GIF"]
                        implicitWidth: 116
                        Accessible.name: "미디어 종류"
                        onActivated: window.app.library.setFilters(
                            ["all", "photo", "video", "animated_gif"][currentIndex],
                            ["saved", "posted", "posted_oldest", "liked"][orderChoice.currentIndex], window.app.library.filterAuthor)
                    }
                    FlatCombo {
                        id: thumbSizeChoice
                        objectName: "thumbSizeChoice"
                        label: "크기"
                        model: ["작게", "보통", "크게"]
                        currentIndex: window.densityChoice
                        implicitWidth: 116
                        Accessible.name: "미리보기 크기"
                        onActivated: {
                            window.setDensity(currentIndex)
                            if (window.app)
                                window.app.setPreference("gallery_density", window.densityChoice)
                            currentIndex = Qt.binding(function() { return window.densityChoice })
                        }
                    }
                }

                // Row 2: quick filters on the left, things to do with the grid on the right.
                RowLayout {
                    objectName: "galleryFilterRow"
                    Layout.fillWidth: true
                    visible: !!window.app
                    spacing: Theme.gapSmall
                    WrapRow {
                        id: filterHost
                        objectName: "galleryFilterBar"
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignTop
                        // Every control applies the moment it changes, so the grid
                        // always matches what the controls show.
                        FlatButton {
                            id: favoriteFilterButton
                            objectName: "galleryFavoriteFilter"
                            tone: "chip"
                            width: implicitWidth
                            checkable: true
                            checked: window.app ? window.app.library.filterFavoriteOnly : false
                            text: checked ? "★ 즐겨찾기" : "☆ 즐겨찾기"
                            Accessible.name: "즐겨찾기 필터"
                            tip: checked ? "즐겨찾기 필터 해제 (B)" : "즐겨찾기한 사진·영상만 보기 (B)"
                            onClicked: {
                                window.app.library.setFavoriteOnly(!window.app.library.filterFavoriteOnly)
                                checked = Qt.binding(function() { return window.app ? window.app.library.filterFavoriteOnly : false })
                            }
                        }
                        FlatButton {
                            id: duplicateFilterButton
                            objectName: "galleryDuplicateFilter"
                            tone: "chip"
                            width: implicitWidth
                            checkable: true
                            checked: window.app ? window.app.library.filterDuplicateOnly : false
                            text: {
                                const count = window.app ? window.app.library.duplicateCount : 0
                                return count > 0 ? "중복 사진 " + count.toLocaleString() : "중복 사진"
                            }
                            Accessible.name: "중복 사진 필터"
                            tip: checked ? "중복 사진 보기 끝내기 (D)" : "중복 사진 보기 및 정리 (D)"
                            onClicked: {
                                window.app.library.setDuplicateOnly(!window.app.library.filterDuplicateOnly)
                                checked = Qt.binding(function() { return window.app ? window.app.library.filterDuplicateOnly : false })
                            }
                        }
                        FlatButton {
                            id: aiMenuButton
                            objectName: "galleryAiMenuButton"
                            tone: "chip"
                            width: implicitWidth
                            readonly property string applied: window.app ? window.app.library.filterAiCategory : ""
                            chosen: applied.length > 0
                            dropdown: true
                            text: window.app && window.app.library.aiScanning ? "AI 분류 중…"
                                : (applied.length ? "AI 분류: " + applied : "AI 분류")
                            Accessible.name: applied.length ? "AI 분류 " + applied + " 보는 중" : "AI 분류"
                            tip: !aiMenu.visible ? "사진을 다섯 분류로 나눠 보고, 자동분류를 실행합니다" : ""
                            onClicked: aiMenu.popup(aiMenuButton, 0, aiMenuButton.height + 4)
                        }
                        FlatCombo {
                            id: authorFilter
                            objectName: "galleryAuthorFilter"
                            chip: true
                            chosen: applied.length > 0
                            minimumPopupWidth: 280
                            implicitContentWidthPolicy: ComboBox.ContentItemImplicitWidth
                            width: Math.min(220, Math.max(96, implicitContentWidth + leftPadding + rightPadding))
                            height: implicitHeight
                            // The closed chip names the author; the count belongs to the list.
                            displayText: currentIndex > 0 && model[currentIndex] ? "@" + model[currentIndex].value : "작성자"
                            Accessible.name: "작성자 선택"
                            textRole: "label"
                            valueRole: "value"
                            readonly property string applied: window.app ? window.app.library.filterAuthor : ""
                            readonly property var authorRows: {
                                if (!window.app)
                                    return []
                                const rows = window.app.library.authors
                                return rows ? rows : []
                            }
                            // "미디어 N개" is the author's number of saved photos, videos and GIFs
                            // in the library, whatever filters are active.
                            model: {
                                const items = [{ label: "전체 작성자", value: "" }]
                                let found = !authorFilter.applied.length
                                for (let i = 0; i < authorRows.length; i++) {
                                    const row = authorRows[i]
                                    const handle = row && row.author ? String(row.author) : ""
                                    if (!handle.length)
                                        continue
                                    if (handle.toLowerCase() === authorFilter.applied.toLowerCase())
                                        found = true
                                    const count = row.count !== undefined ? Number(row.count).toLocaleString() : "0"
                                    items.push({ label: "@" + handle + " · 미디어 " + count + "개", value: handle })
                                }
                                if (!found)
                                    items.push({ label: "@" + authorFilter.applied, value: authorFilter.applied })
                                return items
                            }
                            function authorIndex() {
                                const wanted = authorFilter.applied.toLowerCase()
                                for (let i = 1; i < model.length; i++) {
                                    if (String(model[i].value).toLowerCase() === wanted)
                                        return i
                                }
                                return 0
                            }
                            currentIndex: authorIndex()
                            onActivated: index => {
                                window.applyFilters({ author: index > 0 && model[index] ? model[index].value : "" })
                                currentIndex = Qt.binding(function() { return authorFilter.authorIndex() })
                            }
                        }
                        FlatCombo {
                            id: hashtagFilter
                            objectName: "galleryHashtagFilter"
                            chip: true
                            chosen: applied.length > 0
                            minimumPopupWidth: 240
                            implicitContentWidthPolicy: ComboBox.ContentItemImplicitWidth
                            width: Math.min(200, Math.max(96, implicitContentWidth + leftPadding + rightPadding))
                            height: implicitHeight
                            displayText: currentIndex > 0 && model[currentIndex] ? model[currentIndex].value : "해시태그"
                            Accessible.name: "해시태그 선택"
                            textRole: "label"
                            valueRole: "value"
                            readonly property string applied: window.app ? window.app.library.filterHashtag : ""
                            readonly property var hashtagRows: {
                                if (!window.app)
                                    return []
                                const rows = window.app.library.hashtags
                                return rows ? rows : []
                            }
                            model: {
                                const items = [{ label: "해시태그 전체", value: "" }]
                                let found = !hashtagFilter.applied.length
                                for (let i = 0; i < hashtagRows.length; i++) {
                                    const row = hashtagRows[i]
                                    const tag = row && row.tag ? String(row.tag) : ""
                                    if (!tag.length)
                                        continue
                                    if (tag.toLowerCase() === hashtagFilter.applied.toLowerCase())
                                        found = true
                                    const count = row.count !== undefined ? Number(row.count).toLocaleString() : "0"
                                    items.push({ label: tag + " · 미디어 " + count + "개", value: tag })
                                }
                                if (!found && hashtagFilter.applied.length)
                                    items.push({ label: hashtagFilter.applied, value: hashtagFilter.applied })
                                return items
                            }
                            function hashtagIndex() {
                                const wanted = hashtagFilter.applied.toLowerCase()
                                for (let i = 1; i < model.length; i++) {
                                    if (String(model[i].value).toLowerCase() === wanted)
                                        return i
                                }
                                return 0
                            }
                            currentIndex: hashtagIndex()
                            onActivated: index => {
                                window.applyFilters({ hashtag: index > 0 && model[index] ? model[index].value : "" })
                                currentIndex = Qt.binding(function() { return hashtagFilter.hashtagIndex() })
                            }
                        }
                        FlatCombo {
                            id: userTagFilter
                            objectName: "galleryUserTagFilter"
                            chip: true
                            chosen: applied.length > 0
                            minimumPopupWidth: 200
                            implicitContentWidthPolicy: ComboBox.ContentItemImplicitWidth
                            width: Math.min(200, Math.max(96, implicitContentWidth + leftPadding + rightPadding))
                            height: implicitHeight
                            displayText: currentIndex > 0 && model[currentIndex] ? "#" + model[currentIndex].value : "내 태그"
                            Accessible.name: "내 태그 선택"
                            textRole: "label"
                            valueRole: "value"
                            readonly property string applied: window.app ? window.app.library.filterUserTag : ""
                            readonly property var tagRows: {
                                if (!window.app)
                                    return []
                                const rows = window.app.library.userTags
                                return rows ? rows : []
                            }
                            model: {
                                const items = [{ label: "내 태그 전체", value: "" }]
                                let found = !userTagFilter.applied.length
                                for (let i = 0; i < tagRows.length; i++) {
                                    const row = tagRows[i]
                                    const tag = row && row.tag ? String(row.tag) : ""
                                    if (!tag.length)
                                        continue
                                    if (tag.toLowerCase() === userTagFilter.applied.toLowerCase())
                                        found = true
                                    const count = row.count !== undefined ? Number(row.count).toLocaleString() : "0"
                                    items.push({ label: "#" + tag + " · " + count + "개", value: tag })
                                }
                                if (!found && userTagFilter.applied.length)
                                    items.push({ label: "#" + userTagFilter.applied, value: userTagFilter.applied })
                                return items
                            }
                            function userTagIndex() {
                                const wanted = userTagFilter.applied.toLowerCase()
                                for (let i = 1; i < model.length; i++) {
                                    if (String(model[i].value).toLowerCase() === wanted)
                                        return i
                                }
                                return 0
                            }
                            currentIndex: userTagIndex()
                            onActivated: index => {
                                window.applyFilters({ userTag: index > 0 && model[index] ? model[index].value : "" })
                                currentIndex = Qt.binding(function() { return userTagFilter.userTagIndex() })
                            }
                        }
                        // The date chip opens the range beside it. It is lit while a date
                        // is applied, whether the range is showing or not. (Scene tests
                        // know it by the name of the filter panel it replaced.)
                        FlatButton {
                            id: filterToggle
                            objectName: "galleryFilterToggle"
                            tone: "chip"
                            width: implicitWidth
                            checkable: true
                            checkLights: false
                            chosen: !!window.app && (window.app.library.dateFrom.length > 0 || window.app.library.dateTo.length > 0)
                            dropdown: true
                            dropdownOpen: checked
                            text: "날짜"
                            Accessible.name: "날짜 필터"
                            tip: checked ? "날짜 범위 접기" : "게시일이나 저장일로 좁히기"
                            onClicked: if (!checked) datePickerPopup.close()
                        }
                        // Which date, from when and until when stay in one group, so the
                        // range does not split across lines.
                        Row {
                            id: dateRange
                            visible: filterToggle.checked
                            spacing: 6
                            FlatCombo {
                                id: dateFieldChoice
                                objectName: "galleryDateField"
                                model: ["게시일", "저장일"]
                                currentIndex: window.app && window.app.library.dateField === "saved" ? 1 : 0
                                implicitHeight: Theme.controlHeight
                                implicitWidth: 96
                                Accessible.name: "날짜 필터 기준"
                                onActivated: index => {
                                    window.applyFilters({ dateField: index === 1 ? "saved" : "posted" })
                                    currentIndex = Qt.binding(function() {
                                        return window.app && window.app.library.dateField === "saved" ? 1 : 0
                                    })
                                }
                            }
                            DateField {
                                id: dateFromFilter
                                objectName: "galleryDateFrom"
                                text: window.app ? window.app.library.dateFrom : ""
                                placeholder: "시작 날짜"
                                accessibleLabel: "시작 날짜"
                                pickerOpen: datePickerPopup.visible && datePickerPopup.targetField === dateFromFilter
                                width: 136
                                height: Theme.controlHeight
                                onPickRequested: datePickerPopup.openFor(dateFromFilter)
                                onCleared: {
                                    if (datePickerPopup.targetField === dateFromFilter)
                                        datePickerPopup.close()
                                    window.applyFilters({ from: "" })
                                }
                            }
                            Label {
                                width: 16
                                height: Theme.controlHeight
                                verticalAlignment: Text.AlignVCenter
                                horizontalAlignment: Text.AlignHCenter
                                text: "~"
                                color: Theme.textMuted
                            }
                            DateField {
                                id: dateToFilter
                                objectName: "galleryDateTo"
                                text: window.app ? window.app.library.dateTo : ""
                                placeholder: "종료 날짜"
                                accessibleLabel: "종료 날짜"
                                pickerOpen: datePickerPopup.visible && datePickerPopup.targetField === dateToFilter
                                width: 136
                                height: Theme.controlHeight
                                onPickRequested: datePickerPopup.openFor(dateToFilter)
                                onCleared: {
                                    if (datePickerPopup.targetField === dateToFilter)
                                        datePickerPopup.close()
                                    window.applyFilters({ to: "" })
                                }
                            }
                        }
                        // Shown only while there is something to clear.
                        FlatButton {
                            id: filterReset
                            objectName: "galleryFilterReset"
                            tone: "ghost"
                            width: implicitWidth
                            leftPadding: 10
                            rightPadding: 10
                            text: "× 필터 지우기"
                            Accessible.name: "필터 초기화"
                            visible: !!window.app && (window.app.library.filterSummary.length > 0 || search.text.length > 0
                                || mediaFilter.currentIndex > 0 || orderChoice.currentIndex > 0 || window.app.library.filterFavoriteOnly || window.app.library.filterDuplicateOnly)
                            onClicked: {
                                searchDelay.stop()
                                datePickerPopup.close()
                                search.text = ""
                                mediaFilter.currentIndex = orderChoice.currentIndex = 0
                                window.app.library.resetFilters()
                            }
                        }
                    }
                    // Shown once a picture is current. It comes first, so the two
                    // after it never move when it appears.
                    FlatButton {
                        objectName: "galleryOpenPreview"
                        Layout.alignment: Qt.AlignTop
                        text: "미리보기"
                        visible: gallery.currentIndex >= 0 && !window.duplicateMode
                        tip: "고른 사진을 크게 보기 (Enter)"
                        onClicked: previewDialog.open()
                    }
                    FlatButton {
                        id: refreshButton
                        objectName: "galleryRefresh"
                        Layout.alignment: Qt.AlignTop
                        // New files saved while the gallery was open light the button up.
                        readonly property bool fresh: !!window.app && window.app.galleryChanged
                        tone: fresh ? "chip" : "quiet"
                        chosen: fresh
                        text: fresh ? "새 사진 보기" : "새로고침"
                        tip: fresh ? "저장된 새 사진을 목록에 넣기 (F5)" : "목록 다시 읽기 (F5)"
                        onClicked: window.app.refreshGallery()
                    }
                    // Starts and ends choosing several files; what to do with them is in the bar at the bottom.
                    FlatButton {
                        id: selectionToggle
                        objectName: "gallerySelectionToggle"
                        Layout.alignment: Qt.AlignTop
                        readonly property bool choosing: !!window.app && window.app.library.selectionMode
                        tone: choosing ? "chip" : "quiet"
                        chosen: choosing
                        text: choosing ? "선택 끝내기" : "여러 개 선택"
                        enabled: !!window.app
                        tip: choosing ? "여러 개 선택 끝내기 (Esc)" : "여러 장을 골라 한 번에 정리하기"
                        onClicked: window.app.library.setSelectionMode(!window.app.library.selectionMode)
                    }
                }

                // One status line: how many, which filters, and whatever last had something to say.
                RowLayout {
                    id: statusLine
                    objectName: "galleryStatusLine"
                    Layout.fillWidth: true
                    Layout.minimumHeight: 28
                    spacing: Theme.gapSmall
                    readonly property bool loadFailed: window.galleryModel.error.length > 0
                    readonly property string managementText: window.transientNotice
                        || (window.app ? (window.app.management.error || window.app.management.status) : "")
                    readonly property string workText: window.app ? window.app.library.status : ""
                    // A notice flashes over anything; otherwise the source that changed last speaks.
                    readonly property bool showManagement: !loadFailed && managementText.length > 0
                        && (window.transientNotice.length > 0 || workText.length === 0 || window.statusSource === "management")
                    readonly property bool showWork: !loadFailed && workText.length > 0 && !showManagement
                    Label {
                        objectName: "galleryCount"
                        text: window.galleryModel.total.toLocaleString() + "개"
                        font.bold: true
                        color: Theme.text
                    }
                    Item {
                        objectName: "galleryBusy"
                        implicitWidth: 20
                        implicitHeight: 20
                        visible: window.galleryModel.busy
                        BusyIndicator {
                            anchors.centerIn: parent
                            running: window.galleryModel.busy
                            implicitWidth: 20
                            implicitHeight: 20
                        }
                    }
                    Label {
                        id: activeFilters
                        objectName: "galleryActiveFilters"
                        Layout.fillWidth: true
                        text: window.app ? window.app.library.filterSummary : ""
                        visible: text.length > 0
                        elide: Text.ElideRight
                        textFormat: Text.PlainText
                        color: Theme.textMuted
                        HoverHandler { id: activeFiltersHover }
                        ToolTip.text: text
                        ToolTip.delay: 500
                        ToolTip.visible: activeFiltersHover.hovered && truncated
                    }
                    Item { Layout.fillWidth: true; visible: !activeFilters.visible }
                    Label {
                        objectName: "galleryLoadError"
                        Layout.maximumWidth: Math.max(160, statusLine.width - 220)
                        text: window.galleryModel.error
                        visible: statusLine.loadFailed
                        color: Theme.error
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                    Label {
                        objectName: "galleryManagementMessage"
                        Layout.maximumWidth: Math.max(160, statusLine.width * 0.62)
                        text: statusLine.managementText
                        visible: statusLine.showManagement
                        horizontalAlignment: Text.AlignRight
                        wrapMode: Text.WordWrap
                        maximumLineCount: 3
                        elide: Text.ElideRight
                        textFormat: Text.PlainText
                        color: window.app && window.app.management.error && !window.transientNotice
                            ? Theme.error : Theme.textMuted
                    }
                    Label {
                        objectName: "galleryWorkStatus"
                        Layout.maximumWidth: Math.max(160, statusLine.width * 0.62)
                        text: statusLine.workText
                        visible: statusLine.showWork
                        horizontalAlignment: Text.AlignRight
                        wrapMode: Text.WordWrap
                        maximumLineCount: 3
                        elide: Text.ElideRight
                        textFormat: Text.PlainText
                        color: Theme.notice
                    }
                    ProgressBar {
                        id: workProgress
                        objectName: "galleryWorkProgress"
                        Layout.preferredWidth: 140
                        Layout.preferredHeight: 4
                        readonly property bool aiWork: window.app && window.app.library.aiProgressTotal > 0
                        readonly property bool duplicateWork: window.app && window.app.library.duplicateProgressTotal > 0
                        readonly property bool preparingAi: window.app && window.app.library.aiScanning && !aiWork && !duplicateWork
                        visible: window.app && window.app.library.busy && (aiWork || duplicateWork || preparingAi)
                        indeterminate: preparingAi || (duplicateWork && window.app.library.duplicateProgressCurrent >= window.app.library.duplicateProgressTotal)
                        from: 0
                        to: aiWork ? window.app.library.aiProgressTotal
                            : (duplicateWork ? window.app.library.duplicateProgressTotal : 1)
                        value: aiWork ? window.app.library.aiProgressCurrent
                            : (window.app ? window.app.library.duplicateProgressCurrent : 0)
                        Accessible.name: "작업 진행"
                    }
                    FlatButton {
                        objectName: "cancelDuplicateScanButton"
                        text: "검사 중단"
                        compact: true
                        visible: window.app && window.app.library.copiesLocked
                        Accessible.name: "중복 검사 중단"
                        onClicked: if (window.app) window.app.library.cancelDuplicateScan()
                    }
                    FlatButton {
                        objectName: "cancelAiScanButton"
                        text: "분류 중단"
                        compact: true
                        visible: window.app && window.app.library.aiScanning
                        Accessible.name: "AI 분류 중단"
                        onClicked: if (window.app) window.app.library.cancelAiScan()
                    }
                }

                // Duplicates are reviewed group by group; what that view needs sits right above it.
                Card {
                    objectName: "duplicateStrip"
                    Layout.fillWidth: true
                    visible: window.duplicateMode
                    implicitHeight: duplicateStripLayout.implicitHeight + 2 * Theme.gap
                    GridLayout {
                        id: duplicateStripLayout
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: Theme.gapLarge
                        anchors.rightMargin: Theme.gap
                        // The buttons for "중복 아님" marks appear only while there are marks; with them
                        // the guide needs a wider window to stay beside the buttons.
                        readonly property bool marks: !!window.app && (window.app.library.canUndoDistinct
                                                                      || window.app.library.distinctDuplicateCount > 0)
                        columns: parent.width >= (marks ? 1240 : 900) ? 2 : 1
                        columnSpacing: Theme.gapLarge
                        rowSpacing: Theme.gapSmall
                        Label {
                            objectName: "duplicateGuide"
                            Layout.fillWidth: true
                            text: "같은 사진끼리 묶었습니다. 묶음마다 ‘남김’ 한 장을 두고 ‘사본’을 골라 휴지통으로 보내세요. "
                                + "남길 사진은 즐겨찾기한 것, 그다음 용량이 큰 것으로 정합니다. "
                                + "같은 사진이 아니거나 모두 남기려면 묶음의 ‘중복 아님’을 누르세요."
                            wrapMode: Text.WordWrap
                            textFormat: Text.PlainText
                            font.pixelSize: Theme.fontSmall
                            color: Theme.textMuted
                        }
                        RowLayout {
                            spacing: Theme.gapSmall
                            Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                            FlatButton {
                                id: duplicateNearToggle
                                objectName: "galleryDuplicateNear"
                                tone: "chip"
                                enabled: !!window.app && !window.app.library.busy
                                checkable: true
                                checked: window.duplicateNear
                                text: checked ? "✓ 화질만 다른 사진 포함" : "화질만 다른 사진 포함"
                                Accessible.name: "크기나 화질만 다른 같은 사진도 찾기"
                                tip: "켜고 검사하면 크기·화질만 다르게 저장된 같은 사진도 묶습니다. 두 사진의 화면을 직접 비교해 99% 이상 같을 때만 묶습니다."
                                onClicked: {
                                    window.duplicateNear = !window.duplicateNear
                                    checked = Qt.binding(function() { return window.duplicateNear })
                                }
                            }
                            FlatButton {
                                id: scanDuplicatesButton
                                objectName: "galleryScanDuplicates"
                                enabled: !!window.app && !window.app.library.busy
                                text: "중복 검사"
                                tip: window.duplicateNear
                                    ? "완전히 같은 파일과, 크기·화질만 다르게 다시 저장된 같은 사진을 찾습니다"
                                    : "내용이 완전히 같은 파일만 찾습니다"
                                onClicked: {
                                    if (window.app.library.scanDuplicates(window.duplicateNear))
                                        window.flashNotice("중복 검사를 시작합니다. 그동안 갤러리를 보고 즐겨찾기를 바꿀 수 있습니다.")
                                }
                            }
                            FlatButton {
                                id: selectDuplicatesButton
                                objectName: "gallerySelectDuplicates"
                                enabled: !!window.app && !window.app.library.copiesLocked && window.galleryModel.count > 0
                                tip: window.app && window.app.library.copiesLocked
                                    ? window.app.library.holdReason
                                    : "가장 화질이 좋거나 즐겨찾기한 원본 1장씩만 남기고 나머지 사본을 선택합니다"
                                text: "사본 일괄 선택"
                                onClicked: {
                                    const count = window.app.library.selectDuplicateCopies()
                                    if (count > 0)
                                        window.flashNotice(count + "개의 중복 사본이 선택되었습니다. '휴지통으로'를 누르면 정리됩니다.")
                                    else
                                        window.flashNotice(window.app.library.status)
                                }
                            }
                            // "중복 아님" marks: take the last one back, or forget them all.
                            FlatButton {
                                objectName: "galleryUndoDistinct"
                                visible: !!window.app && window.app.library.canUndoDistinct
                                enabled: !!window.app && !window.app.library.copiesLocked
                                text: "중복 아님 되돌리기"
                                tip: "방금 ‘중복 아님’으로 표시한 묶음을 다시 보입니다"
                                Accessible.name: "중복 아님 표시 되돌리기"
                                onClicked: window.app.library.undoDistinct()
                            }
                            FlatButton {
                                objectName: "galleryClearDistinct"
                                visible: !!window.app && window.app.library.distinctDuplicateCount > 0
                                enabled: !!window.app && !window.app.library.copiesLocked
                                text: "중복 아님 " + (window.app ? window.app.library.distinctDuplicateCount.toLocaleString() : "0") + "개 초기화"
                                tip: "‘중복 아님’으로 표시한 묶음을 모두 다시 보이게 합니다. 사진은 지우지 않습니다."
                                Accessible.name: "중복 아님 표시 모두 초기화"
                                onClicked: window.app.library.clearDistinct()
                            }
                        }
                    }
                }
                DuplicateReview {
                    id: duplicateReview
                    objectName: "duplicateReview"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    visible: window.duplicateMode
                    app: window.app
                    galleryModel: window.galleryModel
                    tileWidth: window.tileWidth
                    onPreviewRequested: key => {
                        window.app.library.selectItem(key)
                        previewDialog.open()
                    }
                }
                GridView {
                    id: gallery
                    objectName: "galleryGrid"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    visible: !window.duplicateMode
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
                    // Dragging draws a selection rectangle, as in a file manager. The wheel,
                    // the scrollbar and the keys scroll.
                    interactive: false
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
                        // About two notches a row of tiles, and more while the wheel keeps turning.
                        notchPixels: Math.max(120, Math.round(gallery.cellHeight * 0.6))
                        enabled: window.currentScreen === 1 && !window.modalOpen && !datePickerPopup.visible
                            && !window.duplicateMode && (!window.app || !window.app.closing)
                    }
                    DragHandler {
                        id: bandDrag
                        objectName: "galleryBandDrag"
                        target: null
                        acceptedDevices: PointerDevice.Mouse
                        acceptedButtons: Qt.LeftButton
                        enabled: !!window.app && !window.modalOpen && !window.app.closing
                        dragThreshold: 10
                        property real startX: 0
                        property real startY: 0
                        property real currentX: 0
                        property real currentY: 0
                        // Ctrl or Shift held when the button went down adds to the selection,
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
                            const columns = window.columnCount()
                            window.app.library.bandSelect(
                                Math.floor(Math.max(0, top) / gallery.cellHeight),
                                Math.floor(Math.max(0, bottom) / gallery.cellHeight),
                                Math.floor(Math.max(0, left) / gallery.cellWidth),
                                Math.floor(Math.max(0, right) / gallery.cellWidth), columns)
                        }
                        // Where a pointer position is inside the grid's visible area. The handler
                        // reports positions in the scrolling content, which already include the
                        // scroll offset and go stale while the grid moves under a resting pointer,
                        // so the scene position is mapped instead.
                        function inView(scenePoint) {
                            return gallery.mapFromItem(null, scenePoint.x, scenePoint.y)
                        }
                        function follow() {
                            const point = inView(centroid.scenePosition)
                            currentX = Math.max(0, Math.min(gallery.width, point.x))
                            currentY = point.y + gallery.contentY - gallery.originY
                            update()
                        }
                        onActiveChanged: {
                            if (active) {
                                window.stopWheel()
                                const pressed = inView(centroid.scenePressPosition)
                                startX = Math.max(0, Math.min(gallery.width, pressed.x))
                                startY = pressed.y + gallery.contentY - gallery.originY
                                const held = pressModifiers | centroid.modifiers
                                window.app.library.beginBandSelection(
                                    (held & (Qt.ControlModifier | Qt.ShiftModifier)) !== 0)
                                follow()
                            } else {
                                window.app.library.endBandSelection()
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
                            else if (y > gallery.height - 28)
                                step = Math.min(48, 8 + (y - (gallery.height - 28)) * 0.6)
                            if (!step)
                                return
                            const minimum = gallery.originY
                            const maximum = minimum + Math.max(0, gallery.contentHeight - gallery.height)
                            const next = Math.max(minimum, Math.min(maximum, gallery.contentY + step))
                            if (next !== gallery.contentY) {
                                gallery.contentY = next
                                bandDrag.follow()
                            }
                        }
                    }
                    Rectangle {
                        objectName: "galleryBand"
                        // Drawn over the visible area, not inside the scrolling content.
                        parent: gallery
                        visible: bandDrag.active
                        z: 5
                        x: Math.min(bandDrag.startX, bandDrag.currentX)
                        y: Math.min(bandDrag.startY, bandDrag.currentY) - (gallery.contentY - gallery.originY)
                        width: Math.abs(bandDrag.currentX - bandDrag.startX)
                        height: Math.abs(bandDrag.currentY - bandDrag.startY)
                        color: Qt.alpha(Theme.accent, 0.2)
                        border.width: 1
                        border.color: Theme.accent
                    }
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
                    Keys.onSpacePressed: {
                        window.stopWheel()
                        if (window.app && window.app.library.selectionMode && currentItem)
                            window.app.library.toggleSelection(currentItem.key)
                        else if (currentIndex >= 0)
                            previewDialog.open()
                    }
                    Keys.onDeletePressed: {
                        window.stopWheel()
                        if (!window.app)
                            return
                        if (window.app.library.filesLocked) {
                            window.flashNotice(window.app.library.holdReason)
                            return
                        }
                        if (window.app.busy) {
                            window.flashNotice("작업이 끝난 뒤 휴지통으로 이동할 수 있습니다.")
                            return
                        }
                        if (window.app.library.selectionMode && window.app.library.selectedCount > 0)
                            window.app.management.prepareDelete(window.app.library.selectedKeys)
                        else if (!window.app.library.selectionMode && currentItem && !window.app.library.selectedMissing)
                            window.app.management.prepareDelete([currentItem.key])
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
                        required property bool thumbMissing
                        required property bool favorite
                        property bool activeTile: true
                        readonly property bool batchSelected: {
                            const revision = window.app ? window.app.library.selectionRevision : -1
                            return revision >= 0 && window.app.library.isSelected(tile.key)
                        }
                        readonly property bool imageReady: photo.status === Image.Ready
                        readonly property bool imageMissing: !tile.imageReady
                            && (tile.thumbMissing || photo.status === Image.Error)
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
                            radius: Theme.radiusCard
                            // Only the picture's own frame is always drawn; the tile around it
                            // shows when it is pointed at, current or chosen.
                            color: tile.batchSelected || gallery.currentIndex === tile.index ? Theme.accentSoft
                                : (tileMouse.containsMouse ? Theme.card : "transparent")
                            border.width: tile.batchSelected || gallery.currentIndex === tile.index ? 1 : 0
                            border.color: tile.batchSelected ? Theme.accent : Theme.accentLine
                            Rectangle {
                                anchors.fill: photo
                                radius: Theme.radiusControl
                                color: Theme.card
                                visible: !tile.imageMissing
                            }
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
                            Rectangle {
                                objectName: "galleryTileMissingPlaceholder"
                                anchors.fill: photo
                                radius: Theme.radiusControl
                                color: Theme.well
                                border.width: 1
                                border.color: Theme.line
                                visible: tile.imageMissing
                                Column {
                                    anchors.centerIn: parent
                                    spacing: 6
                                    Canvas {
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        width: 32
                                        height: 26
                                        onPaint: {
                                            const ctx = getContext("2d")
                                            ctx.reset()
                                            ctx.strokeStyle = Theme.textFaint
                                            ctx.lineWidth = 1.6
                                            ctx.lineJoin = "round"
                                            ctx.lineCap = "round"
                                            ctx.strokeRect(2, 2, 28, 22)
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
                                            ctx.strokeStyle = Theme.accent
                                            ctx.lineWidth = 1.8
                                            ctx.beginPath()
                                            ctx.moveTo(4, 4)
                                            ctx.lineTo(28, 22)
                                            ctx.stroke()
                                        }
                                    }
                                    Label {
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        text: tile.caption.indexOf("클라우드") >= 0 ? "클라우드"
                                            : tile.caption.indexOf("파일 없음") >= 0 ? "파일 없음" : "미리보기 없음"
                                        font.pixelSize: Theme.fontTiny
                                        color: Theme.textFaint
                                        textFormat: Text.PlainText
                                    }
                                }
                            }
                            Label {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.margins: 10
                                text: tile.caption
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textMuted
                            }
                            MouseArea {
                                id: tileMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onPressed: window.stopWheel()
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                onClicked: mouse => {
                                    gallery.currentIndex = tile.index
                                    gallery.forceActiveFocus()
                                    if (mouse.button === Qt.RightButton) {
                                        teachMenu.targetKey = tile.key
                                        teachMenu.popup()
                                        return
                                    }
                                    if (!window.app)
                                        return
                                    if (mouse.modifiers & Qt.ShiftModifier)
                                        window.app.library.selectRangeTo(tile.key)
                                    else if (window.app.library.selectionMode || (mouse.modifiers & Qt.ControlModifier))
                                        window.app.library.toggleSelection(tile.key)
                                    else
                                        window.app.library.setSelectionAnchor(tile.key)
                                }
                                onDoubleClicked: if (!window.app || !window.app.library.selectionMode) previewDialog.open()
                            }
                            Rectangle {
                                id: favBadge
                                objectName: "galleryItemFavorite"
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 12
                                z: 3
                                width: 28
                                height: 28
                                radius: height / 2
                                color: tile.favorite ? Theme.accent : (favMouse.containsMouse ? Theme.lineStrong : Theme.scrim)
                                visible: tile.favorite || tileMouse.containsMouse || favMouse.containsMouse
                                Text {
                                    anchors.centerIn: parent
                                    text: "★"
                                    font.pixelSize: Theme.fontBody
                                    color: tile.favorite || favMouse.containsMouse ? "white" : Theme.textMuted
                                }
                                MouseArea {
                                    id: favMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    ToolTip.visible: containsMouse
                                    ToolTip.text: tile.favorite ? "즐겨찾기 해제 (F)" : "즐겨찾기 추가 (F)"
                                    onClicked: {
                                        if (window.app)
                                            window.app.library.toggleFavorite(tile.key)
                                    }
                                }
                            }
                            CheckBox {
                                id: tileCheck
                                objectName: "galleryItemSelection"
                                anchors.left: parent.left
                                anchors.top: parent.top
                                anchors.margins: 6
                                z: 2
                                padding: 6
                                visible: window.app && window.app.library.selectionMode
                                enabled: window.app && window.app.library.canSelect(tile.key)
                                checked: tile.batchSelected
                                Accessible.name: "사진 선택: " + tile.caption
                                HoverHandler { cursorShape: tileCheck.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
                                indicator: Rectangle {
                                    implicitWidth: 22
                                    implicitHeight: 22
                                    x: tileCheck.leftPadding
                                    y: tileCheck.topPadding
                                    radius: Theme.radiusSmall
                                    color: tileCheck.checked ? Theme.accent : Theme.scrim
                                    border.width: tileCheck.visualFocus ? 2 : (tileCheck.checked ? 0 : 1)
                                    border.color: tileCheck.visualFocus ? Theme.focus
                                        : (tileCheck.enabled ? Theme.textMuted : Theme.textDisabled)
                                    Text {
                                        anchors.centerIn: parent
                                        visible: tileCheck.checked
                                        text: "✓"
                                        font.pixelSize: Theme.fontBody
                                        font.bold: true
                                        color: "white"
                                    }
                                }
                                onClicked: {
                                    window.app.library.toggleSelection(tile.key)
                                    checked = Qt.binding(function() { return tile.batchSelected })
                                }
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
                        text: {
                            if (window.app && window.app.library.filterDuplicateOnly && window.app.library.copiesLocked)
                                return "중복 사진을 찾는 중 · 끝나면 여기에 모아 둘게요"
                            if (window.app && window.app.library.filterDuplicateOnly)
                                return "중복 사진이 없어요 · 위의 ‘중복 검사’로 다시 찾아볼 수 있어요"
                            if (window.app && window.app.library.filterFavoriteOnly)
                                return "아직 즐겨찾기가 없어요 · 사진의 ★이나 F 키로 담아 보세요"
                            return search.text.length || (window.app && (window.app.library.filterSummary.length > 0
                                || mediaFilter.currentIndex > 0)) ? "조건에 맞는 사진이 없어요"
                                : "아직 사진이 없어요 · 저장 화면에서 좋아요를 담아 보세요"
                        }
                        width: Math.max(0, parent.width - 32)
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        color: Theme.textMuted
                    }
                }
                // While files are being chosen, what can be done with them stays at the
                // bottom edge, where it does not push the pictures down.
                Card {
                    id: selectionBar
                    objectName: "gallerySelectionBar"
                    Layout.fillWidth: true
                    visible: !!window.app && window.app.library.selectionMode
                    border.color: Theme.accentLine
                    glow: 0.12
                    implicitHeight: selectionLayout.implicitHeight + 2 * Theme.gapSmall
                    GridLayout {
                        id: selectionLayout
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: Theme.gapLarge
                        anchors.rightMargin: Theme.gapSmall
                        columns: selectionBar.width >= 800 ? 2 : 1
                        columnSpacing: Theme.gap
                        rowSpacing: Theme.gapTiny
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.gapTiny
                            Label {
                                objectName: "gallerySelectionCount"
                                Layout.fillWidth: true
                                text: window.app ? window.app.library.selectedCount.toLocaleString() + "개 선택"
                                    + (window.app.busy && window.app.library.selectedCount > 0
                                       ? " · 작업이 끝나면 휴지통으로 이동할 수 있습니다" : "") : ""
                                font.bold: true
                                elide: Text.ElideRight
                            }
                            FlatButton {
                                objectName: "gallerySelectLoaded"
                                tone: "ghost"
                                text: "불러온 항목 선택"
                                enabled: !!window.app
                                tip: "지금 불러온 사진을 모두 선택 (Ctrl+A)"
                                onClicked: window.app.library.selectLoaded()
                            }
                            FlatButton {
                                objectName: "gallerySelectionClear"
                                tone: "ghost"
                                text: "선택 해제"
                                onClicked: window.app.library.clearSelection()
                            }
                        }
                        RowLayout {
                            Layout.alignment: Qt.AlignRight
                            spacing: Theme.gapSmall
                            FlatButton {
                                objectName: "galleryTeachSelected"
                                text: "분류 가르치기"
                                enabled: window.app && !window.app.library.tagsLocked && !window.app.library.filesLocked
                                    && window.app.library.selectedCount > 0
                                tip: window.app && window.app.library.holdReason
                                    ? window.app.library.holdReason
                                    : "선택한 사진을 하나의 AI 분류로 가르칩니다. 매우 닮은 사진도 함께 바뀝니다."
                                onClicked: teachPopup.open()
                            }
                            FlatButton {
                                objectName: "galleryAddTagSelected"
                                text: "태그 추가"
                                enabled: window.app && !window.app.busy && !window.app.library.tagsLocked && window.app.library.selectedCount > 0
                                tip: window.app && window.app.library.tagsLocked ? (window.app ? window.app.library.holdReason : "") : ""
                                onClicked: tagBatchPopup.open()
                            }
                            FlatButton {
                                objectName: "galleryRecycleSelected"
                                tone: "danger"
                                text: "휴지통으로"
                                enabled: window.app && !window.app.busy && !window.app.library.filesLocked && window.app.library.selectedCount > 0
                                tip: window.app && window.app.library.filesLocked ? (window.app ? window.app.library.holdReason : "") : ""
                                onClicked: window.app.management.prepareDelete(window.app.library.selectedKeys)
                            }
                        }
                    }
                }
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
        enabled: window.currentScreen === 1 && !window.modalOpen
        onActivated: {
            search.forceActiveFocus()
            search.selectAll()
        }
    }
    Shortcut {
        sequence: "Ctrl+A"
        enabled: window.currentScreen === 1 && !window.modalOpen && !search.activeFocus && !!window.app
        onActivated: {
            window.app.library.selectLoaded()
        }
    }
    Shortcut {
        sequence: "D"
        enabled: window.letterKeysFree
        onActivated: {
            window.app.library.setDuplicateOnly(!window.app.library.filterDuplicateOnly)
        }
    }
    Shortcut {
        sequence: "B"
        enabled: window.letterKeysFree
        onActivated: {
            window.app.library.setFavoriteOnly(!window.app.library.filterFavoriteOnly)
        }
    }
    Shortcut {
        sequence: "S"
        enabled: window.letterKeysFree && !statisticsDialog.visible
        onActivated: {
            statisticsDialog.open()
        }
    }
    Shortcut {
        sequence: "Escape"
        enabled: window.currentScreen === 1 && !window.modalOpen && !search.activeFocus && !!window.app && window.app.library.selectedCount > 0
        onActivated: {
            window.app.library.clearSelection()
        }
    }
    Shortcut {
        sequence: "F"
        enabled: window.letterKeysFree
            && (!!window.app.library.selectedKey || gallery.currentIndex >= 0)
        onActivated: {
            const key = window.app.library.selectedKey || (gallery.currentItem ? gallery.currentItem.key : "")
            if (!key)
                return
            window.flashNotice(window.app.library.favoriteNotice(key))
        }
    }
    Shortcut {
        sequence: "F1"
        enabled: window.currentScreen === 1 && !window.modalOpen && !!window.app && !window.app.closing
        onActivated: shortcutHelp.open()
    }
    Shortcut {
        sequence: "F5"
        // Like the button whose tip names this key: also during a save, when new photos wait to be shown.
        enabled: window.currentScreen === 1 && !window.modalOpen && !!window.app && !window.app.closing
        onActivated: window.app.refreshGallery()
    }
    Dialog {
        id: previewDialog
        objectName: "previewDialog"
        parent: Overlay.overlay
        property bool detailsVisible: false
        readonly property bool textInputFocused: bodyText.activeFocus || inlineTagField.activeFocus
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
            enabled: previewDialog.visible && !previewDialog.textInputFocused && window.app && window.app.library.canPrevious
            onActivated: {
                inlineTagField.text = ""
                window.app.library.previousItem()
            }
        }
        Shortcut {
            sequence: "Right"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && window.app && window.app.library.canNext
            onActivated: {
                inlineTagField.text = ""
                window.app.library.nextItem()
            }
        }
        Shortcut {
            sequence: "F"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && !!window.app
            onActivated: {
                window.flashNotice(window.app.library.favoriteNotice(window.app.library.selectedKey))
            }
        }
        Shortcut {
            sequence: "+"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.canZoomIn
            onActivated: selectedMedia.zoomIn()
        }
        Shortcut {
            sequence: "="
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.canZoomIn
            onActivated: selectedMedia.zoomIn()
        }
        Shortcut {
            sequence: "-"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.canZoomOut
            onActivated: selectedMedia.zoomOut()
        }
        Shortcut {
            sequence: "0"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.ready
            onActivated: selectedMedia.fit()
        }
        Shortcut {
            sequence: "1"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.canActualSize
            onActivated: selectedMedia.actualSize()
        }
        Shortcut {
            sequence: "m"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.mediaType === "video"
                && selectedMedia.videoSource && selectedMedia.ready
            onActivated: selectedMedia.muted = !selectedMedia.muted
        }
        Shortcut {
            sequence: "Shift+M"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && selectedMedia.mediaType === "video"
                && selectedMedia.videoSource && selectedMedia.ready
            onActivated: selectedMedia.muted = !selectedMedia.muted
        }
        Shortcut {
            sequence: "Delete"
            enabled: previewDialog.visible && !previewDialog.textInputFocused && !!window.app && !!window.app.library.selectedKey
            onActivated: {
                if (window.app.library.filesLocked) {
                    window.flashNotice(window.app.library.holdReason)
                    return
                }
                if (window.app.busy) {
                    window.flashNotice("작업이 끝난 뒤 휴지통으로 이동할 수 있습니다.")
                    return
                }
                if (!window.app.library.selectedMissing) {
                    window.app.management.prepareDelete([window.app.library.selectedKey])
                }
            }
        }
        onAboutToShow: {
            detailsVisible = false
            inlineTagField.text = ""
            window.stopWheel()
        }
        onClosed: {
            inlineTagField.text = ""
            selectedMedia.release()
        }
        background: Card {
            radius: Theme.radiusDialog
            topColor: Theme.dialogTop
            bottomColor: Theme.dialog
            border.color: Theme.lineStrong
            glow: 0.1
        }
        header: RowLayout {
            spacing: 8
            Label {
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.topMargin: 12
                Layout.bottomMargin: 4
                text: previewDialog.title
                font.pixelSize: Theme.fontHeading
                font.bold: true
            }
            FlatButton {
                objectName: "previewMaximizeButton"
                Layout.rightMargin: 12
                Layout.topMargin: 8
                tone: "ghost"
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
        footer: ColumnLayout {
            width: parent ? parent.width : 0
            spacing: 8
            Flow {
                visible: !!window.app
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.rightMargin: 16
                spacing: 8
                FlatButton { text: "파일 열기"; enabled: window.app && !window.app.library.selectedMissing; onClicked: window.app.library.openSelected() }
                FlatButton { text: "폴더"; enabled: window.app && !window.app.library.selectedMissing; onClicked: window.app.library.revealSelected() }
                FlatButton {
                    objectName: "previewRecycle"
                    tone: "danger"
                    text: "휴지통으로"
                    enabled: window.app && !window.app.busy && !window.app.library.filesLocked && !window.app.library.selectedMissing
                    tip: window.app && window.app.library.filesLocked ? (window.app ? window.app.library.holdReason : "") : ""
                    onClicked: window.app.management.prepareDelete([window.app.library.selectedKey])
                }
                FlatButton { text: "원문 보기"; enabled: window.app && window.app.library.selectedSource.length > 0; onClicked: window.app.library.openSource() }
                FlatButton {
                    objectName: "previewFavoriteButton"
                    text: window.app && window.app.library.selectedIsFavorite ? "★ 즐겨찾기 해제" : "☆ 즐겨찾기"
                    // A state that is on, like a filter: lit, not the dialog's main action.
                    tone: "chip"
                    chosen: !!window.app && window.app.library.selectedIsFavorite
                    enabled: !!window.app && !!window.app.library.selectedKey
                    tip: window.app && window.app.library.selectedIsFavorite ? "즐겨찾기 해제 (F)" : "즐겨찾기 추가 (F)"
                    onClicked: {
                        window.flashNotice(window.app.library.favoriteNotice(window.app.library.selectedKey))
                    }
                }
            }
            DialogButtons {
                Layout.fillWidth: true
                leftPadding: 16
                rightPadding: 16
                topPadding: 4
                bottomPadding: 14
                FlatButton {
                    objectName: "previewDetailsButton"
                    visible: window.app && window.app.library.selectedBody.length > 0
                    text: previewDialog.detailsVisible ? "본문 접기" : "본문 보기"
                    checkable: true
                    checked: previewDialog.detailsVisible
                    DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
                    onClicked: {
                        previewDialog.detailsVisible = !previewDialog.detailsVisible
                        checked = Qt.binding(function() { return previewDialog.detailsVisible })
                    }
                }
                FlatButton {
                    text: "닫기"
                    DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
                }
                onRejected: previewDialog.close()
            }
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
                    tip: "이전 미디어 (←)"
                    onClicked: {
                        inlineTagField.text = ""
                        window.app.library.previousItem()
                    }
                }
                FlatButton {
                    objectName: "previewNextButton"
                    text: "다음"
                    enabled: window.app && window.app.library.canNext
                    tip: "다음 미디어 (→)"
                    onClicked: {
                        inlineTagField.text = ""
                        window.app.library.nextItem()
                    }
                }
                PreviewZoomControls { preview: selectedMedia }
            }
            MediaPreview {
                id: selectedMedia
                objectName: "selectedMedia"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 80
                sourceUrl: window.app ? window.app.library.selectedPreviewUrl : window.selectedThumb
                mediaType: window.app ? window.app.library.selectedMediaType : "photo"
                sourcePixelWidth: window.app ? window.app.library.selectedPixelWidth : 0
                sourcePixelHeight: window.app ? window.app.library.selectedPixelHeight : 0
                sourceRawWidth: window.app ? window.app.library.selectedRawWidth : 0
                sourceRawHeight: window.app ? window.app.library.selectedRawHeight : 0
                missingNote: window.app ? window.app.library.selectedMissingNote : ""
                // Only the opened item's own load. A folder-wide scan used to keep the
                // spinner on a missing file for as long as the scan ran.
                externalBusy: window.app ? window.app.library.selectionBusy : false
                playing: previewDialog.visible
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Label {
                    id: previewAuthorLabel
                    text: window.app && window.app.library.selectedAuthor ? "@" + window.app.library.selectedAuthor : ""
                    textFormat: Text.PlainText
                    font.bold: true
                    elide: Text.ElideRight
                    color: authorMouse.containsMouse ? Theme.accent : Theme.text
                    MouseArea {
                        id: authorMouse
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: {
                            if (window.app && window.app.library.selectedAuthor) {
                                window.applyFilters({ author: window.app.library.selectedAuthor })
                                previewDialog.close()
                            }
                        }
                    }
                    ToolTip.visible: authorMouse.containsMouse && text.length > 0
                    ToolTip.text: "작성자 모아보기"
                }
            }
            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: window.app ? window.app.library.selectedPublicInfo : ""
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                color: Theme.textMuted
                font.pixelSize: Theme.fontCaption
            }
            Flow {
                Layout.fillWidth: true
                spacing: 6
                visible: window.app && (window.app.library.selectedHashtags.length > 0 || window.app.library.selectedTags.length > 0 || !!window.app.library.selectedKey)

                Repeater {
                    model: window.app ? window.app.library.selectedHashtags : []
                    delegate: Rectangle {
                        objectName: "previewHashtagChip"
                        required property string modelData
                        color: hashMouse.containsMouse ? Theme.hashtagHover : Theme.hashtagFill
                        radius: Theme.radiusSmall
                        border.width: 1
                        border.color: Theme.hashtagLine
                        implicitWidth: hashLabel.implicitWidth + 16
                        implicitHeight: Theme.compactHeight

                        Label {
                            id: hashLabel
                            anchors.centerIn: parent
                            text: modelData
                            font.pixelSize: Theme.fontSmall
                            color: Theme.hashtagText
                            ToolTip.visible: hashMouse.containsMouse
                            ToolTip.text: "해시태그 모아보기"
                        }
                        MouseArea {
                            id: hashMouse
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: {
                                window.applyFilters({ hashtag: modelData })
                                previewDialog.close()
                            }
                        }
                    }
                }

                Repeater {
                    model: window.app ? window.app.library.selectedTags : []
                    delegate: Rectangle {
                        objectName: "previewTagChip"
                        // Declared, or the fields of a tag read as undefined: the chip
                        // then showed no name, and an AI label looked like a typed tag.
                        required property var modelData
                        readonly property string tagName: modelData.tag || ""
                        readonly property bool isConfirmed: modelData.source === "confirmed"
                        readonly property bool isAi: modelData.source === "ai" || isConfirmed
                        color: userTagMouse.containsMouse ? (isAi ? Theme.aiTagHover : Theme.userTagHover) : (isAi ? Theme.aiTagFill : Theme.userTagFill)
                        radius: Theme.radiusSmall
                        border.width: 1
                        border.color: isAi ? Theme.aiTagLine : Theme.userTagLine
                        implicitWidth: userTagRow.implicitWidth + 14
                        implicitHeight: Theme.compactHeight

                        RowLayout {
                            id: userTagRow
                            anchors.centerIn: parent
                            spacing: 4

                            Label {
                                objectName: "previewTagName"
                                text: (isAi ? "AI · #" : "#") + (modelData.tag || "")
                                font.pixelSize: Theme.fontSmall
                                color: isAi ? Theme.aiTagText : Theme.userTagText
                                ToolTip.visible: userTagMouse.containsMouse
                                ToolTip.text: isConfirmed ? "내가 가르친 AI 분류 (누르면 이 분류만 보기)"
                                    : (isAi ? "AI 자동분류 (누르면 이 분류만 보기)" : "내 태그 (누르면 이 태그만 보기)")
                                MouseArea {
                                    id: userTagMouse
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    hoverEnabled: true
                                    onClicked: {
                                        if (modelData.source === "ai" || modelData.source === "confirmed")
                                            window.app.library.setAiFilter(modelData.tag, "")
                                        else
                                            window.applyFilters({ userTag: modelData.tag })
                                        previewDialog.close()
                                    }
                                }
                            }
                            Label {
                                visible: !isAi
                                text: "×"
                                font.pixelSize: Theme.fontSmall
                                font.bold: true
                                leftPadding: 3
                                rightPadding: 3
                                color: removeMouse.containsMouse ? Theme.error : Theme.textMuted
                                ToolTip.visible: removeMouse.containsMouse
                                ToolTip.text: "태그 삭제"
                                MouseArea {
                                    id: removeMouse
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    hoverEnabled: true
                                    enabled: !window.app || !window.app.library.tagsLocked
                                    onClicked: {
                                        window.app.library.removeSelectedTag(modelData.tag)
                                    }
                                }
                            }
                        }
                    }
                }

                // The same height and type size as the other small buttons of the app
                // (it used to be a row of 22-pixel controls in the smallest type).
                RowLayout {
                    objectName: "previewTagTools"
                    spacing: Theme.gapSmall
                    visible: !!window.app && !!window.app.library.selectedKey
                    FlatField {
                        id: inlineTagField
                        objectName: "inlineTagField"
                        readonly property string cleanTag: text.trim().replace(/^#+/, "").trim()
                        implicitHeight: Theme.compactHeight
                        Layout.preferredWidth: 132
                        leftPadding: Theme.gap
                        rightPadding: Theme.gap
                        maximumLength: 50
                        placeholderText: "새 태그"
                        font.pixelSize: Theme.fontSmall
                        Keys.onEscapePressed: event => {
                            text = ""
                            focus = false
                            event.accepted = true
                        }
                        onAccepted: {
                            if (cleanTag.length > 0 && window.app.library.addSelectedTag(cleanTag))
                                text = ""
                        }
                    }
                    FlatButton {
                        objectName: "inlineTagAdd"
                        text: "+ 태그"
                        compact: true
                        enabled: inlineTagField.cleanTag.length > 0 && (!window.app || !window.app.library.tagsLocked)
                        tip: window.app && window.app.library.tagsLocked ? (window.app ? window.app.library.holdReason : "") : ""
                        onClicked: {
                            if (window.app && window.app.library.addSelectedTag(inlineTagField.cleanTag))
                                inlineTagField.text = ""
                        }
                    }
                    Label {
                        visible: {
                            if (!window.app || window.app.library.selectedMediaType !== "photo")
                                return false
                            return window.app.library.selectedAiConfirmed || window.app.library.selectedAiCategory.length > 0
                        }
                        text: {
                            if (!window.app)
                                return ""
                            const library = window.app.library
                            if (library.selectedAiConfirmed && library.selectedAiCategory.length)
                                return library.selectedAiCategory + " · 확정"
                            if (library.selectedAiConfirmed)
                                return "분류 없음 · 확정"
                            return "AI " + library.selectedAiCategory
                                + (library.selectedAiConfidence.length ? " " + library.selectedAiConfidence : "")
                        }
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                    }
                    FlatButton {
                        objectName: "previewAiConfirm"
                        text: "맞아요"
                        compact: true
                        visible: !!window.app && window.app.library.selectedAiCategory.length > 0 && !window.app.library.selectedAiConfirmed
                        enabled: !!window.app && !window.app.library.tagsLocked
                        onClicked: {
                            if (window.app)
                                window.teachCategory(window.app.library.selectedAiCategory, false)
                        }
                    }
                    FlatButton {
                        objectName: "previewAiCorrect"
                        text: "분류 고치기"
                        compact: true
                        visible: !!window.app && window.app.library.selectedMediaType === "photo"
                        enabled: !!window.app && !window.app.library.tagsLocked
                        tip: "다섯 분류 중 하나로 가르치거나, 어느 쪽도 아니라고 지정합니다"
                        onClicked: {
                            if (!window.app)
                                return
                            teachMenu.targetKey = window.app.library.selectedKey
                            teachMenu.popup()
                        }
                    }
                }
            }
            ScrollView {
                id: bodyScroll
                objectName: "previewBodyScroll"
                SmoothWheel { flickable: bodyScroll.contentItem }
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
                    color: Theme.text
                    selectionColor: Theme.accent
                    selectedTextColor: "white"
                    font.pixelSize: Theme.fontSmall
                    background: null
                }
            }
            Label {
                Layout.fillWidth: true
                text: window.app ? window.app.library.status : ""
                textFormat: Text.PlainText
                visible: text.length > 0
                wrapMode: Text.WordWrap
                color: Theme.textMuted
            }
            Label {
                visible: window.app && text.length > 0 && !window.app.library.selectionBusy && !selectedMedia.unavailable
                text: window.app ? window.app.library.selectedMissingNote : ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                color: Theme.textMuted
            }
        }
    }

    Connections {
        target: window.app ? window.app.library : null
        function onChanged() {
            // A filter or account change clears the selection. An empty preview
            // would otherwise say the file is gone while the list reloads.
            if (previewDialog.visible && window.app && !window.app.library.selectionBusy
                    && window.app.library.selectedKey.length === 0)
                previewDialog.close()
        }
    }

    Popup {
        id: datePickerPopup
        objectName: "galleryDatePickerPopup"
        parent: Overlay.overlay
        width: 280
        margins: 8
        padding: 12
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property var targetField: null
        property int viewYear: 2026
        property int viewMonth: 0
        property string todayIso: ""
        readonly property int firstWeekday: new Date(viewYear, viewMonth, 1).getDay()
        readonly property int daysInMonth: new Date(viewYear, viewMonth + 1, 0).getDate()

        function pad2(n) {
            return n < 10 ? "0" + n : String(n)
        }

        function formatIso(y, m, d) {
            return String(y) + "-" + pad2(m) + "-" + pad2(d)
        }

        function stepMonth(delta) {
            const next = new Date(viewYear, viewMonth + delta, 1)
            viewYear = next.getFullYear()
            viewMonth = next.getMonth()
        }

        // Store the picked date (or "" to clear it) as the start or the end of the range.
        function choose(iso) {
            const start = targetField === dateFromFilter
            close()
            window.applyFilters(start ? { from: iso } : { to: iso })
        }

        function focusDay(day) {
            if (day < 1 || day > daysInMonth)
                return false
            const pending = [contentItem]
            while (pending.length) {
                const item = pending.pop()
                if (!item)
                    continue
                if (item.objectName === "datePickerDay_" + day) {
                    item.forceActiveFocus()
                    return true
                }
                const nested = item.children || []
                for (let index = 0; index < nested.length; index++)
                    pending.push(nested[index])
            }
            return false
        }

        function openFor(field) {
            window.stopWheel()
            targetField = field
            const now = new Date()
            todayIso = formatIso(now.getFullYear(), now.getMonth() + 1, now.getDate())
            const raw = field && field.text ? String(field.text).trim() : ""
            const fallback = field === dateToFilter && dateFromFilter.text
                ? String(dateFromFilter.text).trim()
                : (field === dateFromFilter && dateToFilter.text ? String(dateToFilter.text).trim() : "")
            const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw || fallback)
            if (match) {
                viewYear = Number(match[1])
                viewMonth = Math.max(0, Math.min(11, Number(match[2]) - 1))
            } else {
                viewYear = now.getFullYear()
                viewMonth = now.getMonth()
            }
            if (field && Overlay.overlay) {
                const pos = field.mapToItem(Overlay.overlay, 0, field.height + 6)
                const popupHeight = Math.max(308, height, implicitHeight)
                x = Math.max(8, Math.min(Overlay.overlay.width - width - 8, pos.x))
                y = Math.max(8, Math.min(Overlay.overlay.height - popupHeight - 8, pos.y))
            }
            open()
        }

        background: Card {
            topColor: Theme.dialogTop
            bottomColor: Theme.dialog
            border.color: Theme.lineStrong
        }

        contentItem: ColumnLayout {
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                spacing: 4
                FlatButton {
                    objectName: "datePickerPrevYear"
                    text: "«"
                    Accessible.name: "이전 해"
                    implicitWidth: 32
                    implicitHeight: 30
                    leftPadding: 0
                    rightPadding: 0
                    onClicked: datePickerPopup.viewYear -= 1
                }
                FlatButton {
                    objectName: "datePickerPrevMonth"
                    text: "‹"
                    Accessible.name: "이전 달"
                    implicitWidth: 32
                    implicitHeight: 30
                    leftPadding: 0
                    rightPadding: 0
                    onClicked: datePickerPopup.stepMonth(-1)
                }
                Label {
                    objectName: "datePickerHeaderLabel"
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: datePickerPopup.viewYear + "년 " + (datePickerPopup.viewMonth + 1) + "월"
                    font.bold: true
                    font.pixelSize: Theme.fontBody
                }
                FlatButton {
                    objectName: "datePickerNextMonth"
                    text: "›"
                    Accessible.name: "다음 달"
                    implicitWidth: 32
                    implicitHeight: 30
                    leftPadding: 0
                    rightPadding: 0
                    onClicked: datePickerPopup.stepMonth(1)
                }
                FlatButton {
                    objectName: "datePickerNextYear"
                    text: "»"
                    Accessible.name: "다음 해"
                    implicitWidth: 32
                    implicitHeight: 30
                    leftPadding: 0
                    rightPadding: 0
                    onClicked: datePickerPopup.viewYear += 1
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 7
                rowSpacing: 2
                columnSpacing: 2
                Repeater {
                    model: ["일", "월", "화", "수", "목", "금", "토"]
                    delegate: Label {
                        required property string modelData
                        Layout.fillWidth: true
                        Layout.preferredWidth: 34
                        horizontalAlignment: Text.AlignHCenter
                        text: modelData
                        font.pixelSize: Theme.fontTiny
                        color: Theme.textFaint
                    }
                }
                Repeater {
                    model: 42
                    delegate: Rectangle {
                        id: dayCell
                        required property int index
                        readonly property int dayNumber: index - datePickerPopup.firstWeekday + 1
                        readonly property bool validDay: dayNumber >= 1 && dayNumber <= datePickerPopup.daysInMonth
                        readonly property string isoDate: validDay
                            ? datePickerPopup.formatIso(datePickerPopup.viewYear, datePickerPopup.viewMonth + 1, dayNumber)
                            : ""
                        readonly property bool selectedDay: validDay && datePickerPopup.targetField
                            && datePickerPopup.targetField.text === isoDate
                        readonly property bool isToday: validDay && isoDate === datePickerPopup.todayIso
                        objectName: validDay ? "datePickerDay_" + dayNumber : ""
                        Layout.fillWidth: true
                        Layout.preferredWidth: 34
                        Layout.preferredHeight: 30
                        radius: Theme.radiusSmall
                        activeFocusOnTab: validDay
                        Accessible.role: Accessible.Button
                        Accessible.name: validDay
                            ? datePickerPopup.viewYear + "년 " + (datePickerPopup.viewMonth + 1) + "월 " + dayNumber + "일"
                            : ""
                        border.width: activeFocus ? 2 : (!selectedDay && isToday ? 1 : 0)
                        border.color: activeFocus ? Theme.focus : Theme.textFaint
                        Keys.onPressed: event => {
                            if (!validDay)
                                return
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                datePickerPopup.choose(isoDate)
                                event.accepted = true
                            } else if (event.key === Qt.Key_Left) {
                                datePickerPopup.focusDay(dayNumber - 1)
                                event.accepted = true
                            } else if (event.key === Qt.Key_Right) {
                                datePickerPopup.focusDay(dayNumber + 1)
                                event.accepted = true
                            } else if (event.key === Qt.Key_Up) {
                                datePickerPopup.focusDay(dayNumber - 7)
                                event.accepted = true
                            } else if (event.key === Qt.Key_Down) {
                                datePickerPopup.focusDay(dayNumber + 7)
                                event.accepted = true
                            }
                        }
                        color: !validDay ? "transparent"
                            : (selectedDay ? Theme.accent : (dayMouse.containsMouse ? Theme.raised : "transparent"))
                        Label {
                            anchors.centerIn: parent
                            visible: dayCell.validDay
                            text: dayCell.validDay ? String(dayCell.dayNumber) : ""
                            font.pixelSize: Theme.fontCaption
                            font.bold: dayCell.selectedDay
                            color: dayCell.selectedDay ? "white" : Theme.text
                        }
                        MouseArea {
                            id: dayMouse
                            anchors.fill: parent
                            enabled: dayCell.validDay
                            hoverEnabled: dayCell.validDay
                            cursorShape: dayCell.validDay ? Qt.PointingHandCursor : Qt.ArrowCursor
                            onClicked: {
                                if (datePickerPopup.targetField && dayCell.validDay)
                                    datePickerPopup.choose(dayCell.isoDate)
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 6
                FlatButton {
                    objectName: "datePickerToday"
                    Layout.fillWidth: true
                    implicitHeight: 32
                    text: "오늘"
                    onClicked: {
                        const now = new Date()
                        datePickerPopup.choose(datePickerPopup.formatIso(
                            now.getFullYear(), now.getMonth() + 1, now.getDate()))
                    }
                }
                FlatButton {
                    objectName: "datePickerClear"
                    Layout.fillWidth: true
                    implicitHeight: 32
                    text: "초기화"
                    onClicked: datePickerPopup.choose("")
                }
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

    // A move to the Recycle Bin that started in the preview returns to it:
    // on the next photo after the move, on the same one after a cancel.
    property bool resumePreview: false

    Connections {
        target: window.app ? window.app.management : null
        function onDeleteConfirmationRequested() {
            window.resumePreview = previewDialog.visible
            selectedMedia.release()
            previewDialog.close()
            Qt.callLater(function() {
                if (window.app && !window.app.closing && window.app.management.deleteConfirmationPending)
                    deleteConfirmation.open()
            })
        }
        function onChanged() {
            if (!window.resumePreview || window.app.management.mutating
                    || window.app.management.deleteConfirmationPending)
                return
            window.resumePreview = false
            // Only with a photo to show: the library has already opened the one that took the place.
            if (window.app && !window.app.closing && window.currentScreen === 1 && !previewDialog.visible
                    && (window.app.library.selectionBusy || window.app.library.selectedKey.length > 0))
                previewDialog.open()
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
        onScheduleRequested: scheduleDialog.open()
        onRunHistoryRequested: runHistoryDialog.open()
        onUnsavedRequested: unsavedDialog.open()
    }

    ScheduleDialog {
        id: scheduleDialog
        parent: Overlay.overlay
        app: window.app
    }

    RunHistoryDialog {
        id: runHistoryDialog
        parent: Overlay.overlay
        app: window.app
    }

    FlatDialog {
        id: aiClearConfirm
        objectName: "aiClearConfirm"
        parent: Overlay.overlay
        title: "AI 태그를 초기화할까요?"
        preferredWidth: 460
        windowMargin: 48
        onOpened: aiClearCancel.forceActiveFocus()
        contentItem: Label {
            text: "자동분류로 붙은 AI 태그만 지웁니다. 직접 추가한 태그와 사진 파일은 그대로 둡니다."
            wrapMode: Text.Wrap
            color: Theme.textMuted
            textFormat: Text.PlainText
        }
        footer: DialogButtons {
            FlatButton {
                objectName: "aiClearConfirmButton"
                text: "AI 태그 초기화"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                id: aiClearCancel
                objectName: "aiClearCancelButton"
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onAccepted: {
                if (window.app && !window.app.library.busy) {
                    window.app.library.clearAiTags()
                    window.flashNotice("AI 자동분류 태그를 초기화하는 중입니다…")
                }
            }
            onRejected: aiClearConfirm.close()
        }
    }

    ShortcutHelp {
        id: shortcutHelp
        parent: Overlay.overlay
    }

    StatisticsDialog {
        id: statisticsDialog
        objectName: "statisticsDialog"
        parent: Overlay.overlay
        app: window.app
        onAuthorSelected: author => {
            window.currentScreen = 1
            window.applyFilters({ author: author })
            window.flashNotice("@" + author + " 작성자 모아보기를 적용했습니다.")
        }
        onAiCategorySelected: category => {
            window.currentScreen = 1
            if (window.app)
                window.app.library.setAiFilter(category, "")
            window.flashNotice("AI 분류 " + category + " 모아보기를 적용했습니다.")
        }
        onUserTagSelected: tag => {
            window.currentScreen = 1
            window.applyFilters({ userTag: tag })
            window.flashNotice("#" + tag + " 내 태그 모아보기를 적용했습니다.")
        }
        onDuplicateSelected: {
            window.currentScreen = 1
            if (window.app && !window.app.library.filterDuplicateOnly) {
                window.app.library.setDuplicateOnly(true)
            }
            window.flashNotice("중복 사진 모아보기를 적용했습니다.")
        }
    }

    Popup {
        id: teachPopup
        objectName: "teachPopup"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        anchors.centerIn: parent
        width: 320
        height: 380
        padding: 16
        background: Card {
            topColor: Theme.dialogTop
            bottomColor: Theme.dialog
            border.color: Theme.lineStrong
            glow: 0.12
        }
        ColumnLayout {
            anchors.fill: parent
            spacing: 10
            Label {
                text: "선택한 사진을 이 분류로 가르칩니다."
                color: Theme.text
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Repeater {
                model: window.app ? window.app.library.aiCategories : []
                delegate: FlatButton {
                    required property string modelData
                    Layout.fillWidth: true
                    text: modelData
                    enabled: window.app && !window.app.library.tagsLocked
                    onClicked: {
                        window.teachCategory(modelData, true)
                        teachPopup.close()
                    }
                }
            }
            FlatButton {
                Layout.fillWidth: true
                text: "어느 쪽도 아님"
                enabled: window.app && !window.app.library.tagsLocked
                onClicked: {
                    window.teachCategory("", true)
                    teachPopup.close()
                }
            }
        }
    }

    FlatMenu {
        id: aiMenu
        objectName: "galleryAiMenu"
        readonly property var counts: window.app ? window.app.library.aiCategoryCounts : []
        readonly property string applied: window.app ? window.app.library.filterAiCategory : ""
        function amount(value) { return Number(value || 0).toLocaleString(Qt.locale(), "f", 0) }
        FlatMenuItem {
            objectName: "galleryAiAll"
            text: "모든 사진"
            checkable: true
            checked: aiMenu.applied.length === 0
            onTriggered: if (window.app) window.app.library.setAiFilter("", "")
        }
        Instantiator {
            model: aiMenu.counts
            FlatMenuItem {
                required property var modelData
                objectName: "galleryAiCategory_" + (modelData.category || "")
                text: (modelData.category || "") + "  ·  " + aiMenu.amount(modelData.count) + "장"
                checkable: true
                checked: aiMenu.applied === modelData.category
                onTriggered: if (window.app) window.app.library.setAiFilter(modelData.category, "")
            }
            onObjectAdded: (index, object) => aiMenu.insertItem(index + 1, object)
            onObjectRemoved: (index, object) => aiMenu.removeItem(object)
        }
        FlatMenuSeparator {}
        FlatMenuItem {
            objectName: "galleryAiClassify"
            text: window.app && window.app.library.aiScanning ? "분류 중단" : "AI 자동분류 실행"
            enabled: !!window.app && (window.app.library.aiScanning || !window.app.library.busy)
            onTriggered: {
                if (!window.app)
                    return
                if (window.app.library.aiScanning)
                    window.app.library.cancelAiScan()
                else if (window.app.library.scanAiTags())
                    window.flashNotice("AI 자동분류를 시작합니다. 그동안 갤러리를 보고 즐겨찾기를 바꿀 수 있습니다.")
            }
        }
        FlatMenuItem {
            objectName: "galleryAiClear"
            text: "AI 태그 초기화…"
            enabled: !!window.app && !window.app.library.busy
            onTriggered: aiClearConfirm.open()
        }
    }

    FlatMenu {
        id: teachMenu
        property string targetKey: ""
        Instantiator {
            model: window.app ? window.app.library.aiCategories : []
            FlatMenuItem {
                required property string modelData
                text: modelData
                onTriggered: {
                    if (window.app && window.app.library.selectionMode && window.app.library.selectedCount > 1
                            && window.app.library.isSelected(teachMenu.targetKey))
                        window.teachCategory(modelData, true)
                    else if (window.app)
                        window.app.library.teachOne(teachMenu.targetKey, modelData)
                }
            }
            onObjectAdded: (index, object) => teachMenu.insertItem(index, object)
            onObjectRemoved: (index, object) => teachMenu.removeItem(object)
        }
        FlatMenuItem {
            text: "어느 쪽도 아님"
            onTriggered: {
                if (window.app && window.app.library.selectionMode && window.app.library.selectedCount > 1
                        && window.app.library.isSelected(teachMenu.targetKey))
                    window.teachCategory("", true)
                else if (window.app)
                    window.app.library.teachOne(teachMenu.targetKey, "")
            }
        }
    }

    Popup {
        id: tagBatchPopup
        objectName: "tagBatchPopup"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        anchors.centerIn: parent
        width: 320
        padding: 16
        onOpened: {
            batchTagInput.text = ""
            batchTagInput.forceActiveFocus()
        }
        background: Card {
            topColor: Theme.dialogTop
            bottomColor: Theme.dialog
            border.color: Theme.lineStrong
            glow: 0.12
        }
        contentItem: ColumnLayout {
            spacing: 12
            Label {
                text: "선택한 항목에 태그 추가"
                font.bold: true
                font.pixelSize: Theme.fontSection
                color: Theme.text
            }
            FlatField {
                id: batchTagInput
                objectName: "batchTagInput"
                readonly property string cleanTag: text.trim().replace(/^#+/, "").trim()
                Layout.fillWidth: true
                maximumLength: 50
                placeholderText: "태그 이름 입력"
                onAccepted: {
                    if (cleanTag.length > 0 && window.app.library.addTagToSelection(cleanTag) > 0) {
                        text = ""
                        tagBatchPopup.close()
                    }
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: 8
                FlatButton {
                    text: "취소"
                    onClicked: tagBatchPopup.close()
                }
                FlatButton {
                    text: "추가"
                    tone: "primary"
                    enabled: batchTagInput.cleanTag.length > 0 && (!window.app || !window.app.library.tagsLocked)
                    onClicked: {
                        if (window.app.library.addTagToSelection(batchTagInput.cleanTag) > 0) {
                            batchTagInput.text = ""
                            tagBatchPopup.close()
                        }
                    }
                }
            }
        }
    }

    UnsavedMediaDialog {
        id: unsavedDialog
        parent: Overlay.overlay
        app: window.app
    }

    SaveReviewDialog {
        id: saveReviewDialog
        objectName: "saveReviewDialog"
        parent: Overlay.overlay
        app: window.app
    }

    FlatDialog {
        id: storageDialog
        objectName: "storageDialog"
        parent: Overlay.overlay
        title: "저장 위치 확인"
        preferredWidth: 440
        windowMargin: 48
        footer: DialogButtons {
            FlatButton {
                id: storageContinue
                text: "계속"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
        onOpened: storageContinue.forceActiveFocus()
        onAccepted: if (window.app) window.app.confirmStorage(true)
        onRejected: if (window.app) window.app.confirmStorage(false)
        Label {
            width: parent.width
            text: window.app ? window.app.confirmation : ""
            color: Theme.textMuted
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
    }

    FlatDialog {
        id: repairDialog
        objectName: "repairDialog"
        parent: Overlay.overlay
        title: "기록 복구"
        preferredWidth: 460
        windowMargin: 48
        footer: DialogButtons {
            FlatButton {
                id: repairAccept
                text: "복구"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
        onOpened: repairAccept.forceActiveFocus()
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
                scheduleDialog.close()
                runHistoryDialog.close()
                statisticsDialog.close()
                shortcutHelp.close()
                tagBatchPopup.close()
                aiClearConfirm.close()
                teachPopup.close()
                teachMenu.close()
                aiMenu.close()
                unsavedDialog.close()
                storageDialog.close()
                repairDialog.close()
                folderDialog.close()
                deleteConfirmation.close()
                datePickerPopup.close()
            }
        }
        function onLoginRequested() { connectionDialog.open() }
        // A retry runs on the save screen, where its progress shows.
        function onRetryStarted() {
            unsavedDialog.close()
            preferencesDialog.close()
            window.currentScreen = 0
        }
        function onStorageConfirmationRequested() { storageDialog.open() }
        function onReviewReady() { saveReviewDialog.open() }
        function onReviewCleared() { saveReviewDialog.close() }
    }
}
