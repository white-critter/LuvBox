pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import QtQuick.Window

// "폴더 정리": a folder on this PC looked at like the gallery. Its duplicates are
// cleared with the gallery's duplicate view and the same Recycle Bin checks, and
// its photos get AI tags from the AI tags every library shares. Nothing is
// written into the folder itself.
Item {
    id: view
    objectName: "localFolderView"
    property var app: null
    property int tileWidth: 168
    property int densityChoice: 1
    property int currentScreen: 0
    property bool saveReviewOpen: false
    property Item popupParent: null
    property bool windowModalOpen: false
    property bool windowShown: true
    signal densitySelected(int choice)
    property bool duplicateNear: false
    readonly property var hostWindow: view.Window.window
    readonly property var folder: app ? app.localFolder : null
    readonly property var manager: app ? app.localManagement : null
    readonly property string folderPath: folder ? folder.folderPath : ""
    readonly property bool checkingFolder: !!folder && folder.openingFolder
    readonly property bool opened: !!folder && folder.hasFolder
    readonly property bool duplicateMode: !!folder && folder.filterDuplicateOnly
    readonly property bool modalOpen: folderPicker.visible || previewPopup.visible || tagPopup.visible
        || teachPopup.visible || aiManager.visible || deleteConfirmation.visible || alikeConfirmation.visible
        || clearConfirm.visible || forgetDialog.visible
    readonly property bool listEngaged: [orderCombo, mediaTypeCombo, densityCombo, userTagCombo].some(
        function(combo) { return combo.activeFocus || combo.popup.visible })
    readonly property bool menuOpen: aiMenu.visible || teachMenu.visible || recentMenu.visible
    readonly property bool screenActive: view.currentScreen === 3 && view.opened && !view.modalOpen && !view.windowModalOpen
    readonly property bool letterKeysFree: view.screenActive && !searchField.activeFocus && !view.listEngaged && !view.menuOpen
    readonly property real thumbPixelRatio: Math.max(1, Screen.devicePixelRatio)
    readonly property int thumbDecodeW: Math.min(384, Math.max(1, Math.round((tileWidth - 28) * thumbPixelRatio)))
    readonly property int thumbDecodeH: Math.min(384, Math.max(1, Math.round((Math.round(tileWidth * 0.75) - 16) * thumbPixelRatio)))
    onThumbPixelRatioChanged: Qt.callLater(updateFocus)
    property real focusContentY: 0
    property int topIndex: 0
    property int pendingAnchor: -1
    property int anchorGeneration: 0
    property bool resumePreview: false
    // The status line, as in the gallery: the folder's work and the Recycle Bin's results; the one
    // that changed last speaks, and a key refused just now says why.
    property string statusSource: "folder"
    property string seenFolderStatus: ""
    property string seenManagerStatus: ""
    property string keyNotice: ""
    readonly property string managerText: manager ? (manager.error || manager.status) : ""
    readonly property string statusText: checkingFolder ? "폴더를 확인하는 중…" : keyNotice.length > 0 ? keyNotice
        : (statusSource === "manager" && managerText.length > 0 ? managerText : (folder ? folder.status : ""))
    readonly property string lockReason: !!manager && manager.locked
        ? (folder && folder.checkingXOverlap && !folder.overlapsXLibrary
            ? "X 저장 폴더와 겹치는지 확인하는 중입니다. 확인이 끝난 뒤 다시 시도해 주세요."
            : "저장 중에는 X 저장 폴더와 겹치는 이 폴더의 사진을 휴지통으로 옮기지 않습니다. 저장이 끝난 뒤 다시 시도해 주세요.")
        : (!!folder && folder.filesLocked ? folder.holdReason : "")

    // Made when the tab is first shown: the folder opened last opens again then.
    Component.onCompleted: if (folder) folder.openLastFolder()
    // The keys go to what is shown: the list once a folder is open, the groups in the duplicate view.
    onOpenedChanged: {
        Qt.callLater(takeFocus)
        Qt.callLater(updateFocus)
    }
    onFolderPathChanged: {
        searchDelay.stop()
        searchField.clearText()
        view.seenFolderStatus = ""
        view.topIndex = 0
        cancelAnchor()
    }
    onDuplicateModeChanged: {
        localDuplicateHelp.close()
        cancelAnchor()
        Qt.callLater(takeFocus)
        Qt.callLater(updateFocus)
    }
    onCurrentScreenChanged: {
        if (currentScreen !== 3)
            localDuplicateHelp.close()
    }
    onTileWidthChanged: {
        if (localWheel)
            localWheel.reset()
        localAnchorTimer.stop()
        grid.cancelFlick()
        if (view.opened && !view.duplicateMode && grid.count > 0 && grid.width > 0 && grid.cellHeight > 0) {
            const cols = Math.max(1, grid.columns)
            const first = Math.floor(Math.max(0, grid.contentY - grid.originY) / Math.max(1, grid.cellHeight)) * cols
            view.pendingAnchor = Math.min(grid.count - 1, Math.max(0, first))
            placeAnchor()
        } else {
            cancelAnchor()
            Qt.callLater(updateFocus)
        }
    }
    function cancelAnchor() {
        pendingAnchor = -1
        localAnchorTimer.stop()
        grid.highlightFollowsCurrentItem = true
    }
    function placeAnchor() {
        if (pendingAnchor < 0)
            return
        anchorGeneration += 1
        localAnchorTimer.keep = pendingAnchor
        localAnchorTimer.tries = 0
        localAnchorTimer.generation = anchorGeneration
        grid.highlightFollowsCurrentItem = false
        localAnchorTimer.restart()
    }
    Timer {
        id: localAnchorTimer
        interval: 40
        repeat: true
        property int keep: -1
        property int tries: 0
        property int generation: 0
        onTriggered: {
            tries += 1
            if (generation !== view.anchorGeneration || keep < 0 || grid.width < 2 || grid.cellWidth < 2 || grid.cellHeight < 2) {
                if (tries >= 8) {
                    grid.highlightFollowsCurrentItem = true
                    stop()
                }
                return
            }
            const columns = Math.max(1, grid.columns)
            const rowStart = keep - (keep % columns)
            const offset = (rowStart / columns) * grid.cellHeight
            if (grid.contentHeight < offset + grid.cellHeight) {
                if (tries >= 8) {
                    grid.highlightFollowsCurrentItem = true
                    stop()
                }
                return
            }
            const limit = Math.max(0, grid.contentHeight - grid.height)
            const target = grid.originY + Math.min(offset, limit)
            grid.cancelFlick()
            grid.contentY = target
            view.topIndex = rowStart
            view.updateFocus()
            if (Math.abs(grid.contentY - target) < 1 || tries >= 8) {
                const visibleEnd = rowStart + columns * Math.max(1, Math.ceil(grid.height / grid.cellHeight))
                if (grid.currentIndex >= 0 && (grid.currentIndex < rowStart || grid.currentIndex >= visibleEnd))
                    grid.currentIndex = rowStart
                grid.highlightFollowsCurrentItem = true
                view.pendingAnchor = -1
                stop()
            }
        }
    }
    onWindowShownChanged: if (windowShown) Qt.callLater(updateFocus)

    function flashNotice(text) {
        keyNotice = text
        if (text && text.length > 0)
            localNoticeTimer.restart()
        else
            localNoticeTimer.stop()
    }

    Timer {
        id: localNoticeTimer
        interval: 3500
        onTriggered: view.keyNotice = ""
    }

    function takeFocus() {
        if (view.currentScreen !== 3 || view.modalOpen || view.windowModalOpen)
            return
        if (opened && !duplicateMode)
            grid.forceActiveFocus()
        else
            view.forceActiveFocus()
    }

    function updateFocus() {
        if (!folder || view.currentScreen !== 3 || view.duplicateMode || !grid.visible || !windowShown
                || grid.width <= 0 || grid.height <= 0 || grid.cellWidth <= 0 || grid.cellHeight <= 0
                || grid.contentHeight <= 0 || grid.count === 0)
            return
        const y = Math.max(0, grid.contentY - grid.originY)
        const direction = y >= view.focusContentY ? 1 : -1
        view.focusContentY = y
        const columns = Math.max(1, grid.columns)
        const first = Math.min(grid.count - 1, Math.floor(y / grid.cellHeight) * columns)
        const bottom = Math.min(grid.contentHeight, y + grid.height)
        const last = Math.min(grid.count - 1, Math.max(first, Math.ceil(bottom / grid.cellHeight) * columns - 1))
        view.topIndex = first
        folder.folderModel.focusViewport(first | 0, last | 0, (direction * columns * 2) | 0,
                                         view.thumbDecodeW | 0, view.thumbDecodeH | 0)
    }

    function closeMenus() {
        aiMenu.close()
        teachMenu.close()
        recentMenu.close()
        localDuplicateHelp.close()
    }

    // The application is closing: nothing of this tab stays open.
    function closeAll() {
        if (localWheel)
            localWheel.reset()
        localPreviewMedia.release()
        closeMenus()
        forgetDialog.close()
        folderPicker.close()
        previewPopup.close()
        tagPopup.close()
        teachPopup.close()
        aiManager.close()
        clearConfirm.close()
        deleteConfirmation.close()
        alikeConfirmation.close()
    }

    function openFolderPicker() {
        const candidate = folderPath || (folder && folder.recentFolders.length > 0 ? folder.recentFolders[0] : "")
        if (candidate.length > 0) {
            const normalized = candidate.replace(/\\/g, "/")
            const encoded = normalized.split("/").map(function(part, index) {
                return (index === 0 && /^[A-Za-z]:$/.test(part)) ? part : encodeURIComponent(part)
            }).join("/")
            folderPicker.currentFolder = (encoded.startsWith("//") ? "file:" : encoded.startsWith("/") ? "file://" : "file:///") + encoded
        }
        folderPicker.open()
    }

    function openPreview(key) {
        if (!folder || !key)
            return
        folder.selectItem(key)
        previewPopup.open()
    }

    function requestMore() {
        if (folder && grid.contentY - grid.originY + grid.height + grid.cellHeight * 2 >= grid.contentHeight)
            folder.folderModel.loadMore()
    }

    function teachCategory(category, selection) {
        if (!folder || folder.tagsLocked)
            return
        const started = selection ? folder.teachSelection(category) : folder.teachSelected(category)
        if (started)
            flashNotice(category.length ? "#" + category + " 분류 저장 중…" : "AI 태그 제외 내용을 저장하는 중…")
    }

    // Keys handled on the grid itself (Space/Enter/Esc); screen-wide shortcuts sit in Shortcut items below.
    function handleGridKey(event) {
        if (!folder || !opened || modalOpen || windowModalOpen || menuOpen || searchField.activeFocus)
            return false
        const ctrl = (event.modifiers & Qt.ControlModifier) !== 0
        const shift = (event.modifiers & Qt.ShiftModifier) !== 0
        const altMeta = (event.modifiers & (Qt.AltModifier | Qt.MetaModifier)) !== 0
        const plain = !ctrl && !shift && !altMeta
        const currentTile = grid.currentItem as FolderTile
        const key = folder.selectedKey || (currentTile ? currentTile.tileKey : "")

        if ((event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                && plain && !duplicateMode && key) {
            if (folder.selectionMode)
                folder.toggleSelection(key)
            else
                previewPopup.open()
            return true
        }
        if (event.key === Qt.Key_Escape && plain) {
            if (localDuplicateHelp.showing) {
                localDuplicateHelp.close()
            } else if (folder.selectedCount > 0) {
                folder.clearSelection()
            } else if (folder.selectionMode) {
                folder.setSelectionMode(false)
            } else {
                grid.currentIndex = -1
                folder.forgetOpened()
            }
            return true
        }
        return false
    }

    Shortcut {
        sequence: "Ctrl+F"
        enabled: view.screenActive
        onActivated: {
            searchField.forceActiveFocus()
            searchField.selectAll()
        }
    }
    Shortcut {
        sequence: "Ctrl+A"
        enabled: view.screenActive && !view.listEngaged && !view.menuOpen && !searchField.activeFocus && !!view.folder
        onActivated: view.folder.selectLoaded()
    }
    Shortcut {
        sequence: "D"
        enabled: view.letterKeysFree
        onActivated: if (view.folder) view.folder.setDuplicateOnly(!view.folder.filterDuplicateOnly)
    }
    Shortcut {
        sequence: "B"
        enabled: view.letterKeysFree
        onActivated: if (view.folder) view.folder.setFavoriteOnly(!view.folder.filterFavoriteOnly)
    }
    Shortcut {
        sequence: "F"
        enabled: view.letterKeysFree && !view.duplicateMode && !!view.folder
            && (!!view.folder.selectedKey || grid.currentIndex >= 0)
        onActivated: {
            const currentTile = grid.currentItem as FolderTile
            const key = view.folder.selectedKey || (currentTile ? currentTile.tileKey : "")
            if (key)
                view.flashNotice(view.folder.favoriteNotice(key))
        }
    }
    Shortcut {
        sequence: "Escape"
        enabled: view.screenActive && !view.menuOpen && localDuplicateHelp.showing
        onActivated: localDuplicateHelp.close()
    }
    Shortcut {
        sequence: "Escape"
        enabled: view.screenActive && !view.listEngaged && !view.menuOpen && !localDuplicateHelp.showing
            && !searchField.activeFocus && !!view.folder
            && (view.folder.selectedCount > 0 || view.folder.selectionMode)
        onActivated: {
            if (view.folder.selectedCount > 0)
                view.folder.clearSelection()
            else
                view.folder.setSelectionMode(false)
        }
    }
    Shortcut {
        sequence: "Delete"
        enabled: view.screenActive && !view.listEngaged && !view.menuOpen && !searchField.activeFocus
            && !!view.folder && !!view.manager && (!view.app || !view.app.closing)
            && (view.folder.selectedCount > 0 || (!view.duplicateMode && !view.folder.selectionMode && view.folder.selectedKey.length > 0))
        onActivated: {
            if (localWheel)
                localWheel.reset()
            if (view.lockReason.length > 0) {
                view.flashNotice(view.lockReason)
                return
            }
            if (view.manager.mutating)
                return
            if (view.folder.selectedCount > 0) {
                view.manager.prepareDelete(view.folder.selectedKeys)
            } else if (view.folder.selectedKey.length > 0 && !view.folder.selectedMissing) {
                view.manager.prepareDelete([view.folder.selectedKey])
            }
        }
    }
    Shortcut {
        sequence: "F5"
        enabled: view.screenActive && !!view.folder && !view.folder.busy && (!view.app || !view.app.closing)
        onActivated: view.folder.refreshFolder()
    }

    FolderDialog {
        id: folderPicker
        objectName: "localFolderPicker"
        title: "정리할 폴더"
        onAccepted: if (view.folder) view.folder.openFolder(selectedFolder.toString())
    }

    // --- No folder yet: what this tab does, and one button.
    Item {
        anchors.fill: parent
        visible: !view.opened
        Card {
            objectName: "localFolderEmpty"
            anchors.centerIn: parent
            width: Math.min(parent.width - 48, 560)
            height: Math.min(emptyColumn.implicitHeight + Theme.cardPadding * 2, Math.max(220, parent.height - 24))
            glow: 0.12
            ColumnLayout {
                id: emptyColumn
                anchors.fill: parent
                anchors.margins: Theme.cardPadding
                spacing: Theme.gap
                Label {
                    Layout.fillWidth: true
                    text: "정리할 폴더를 골라 보세요"
                    font.pixelSize: Theme.fontHeading
                    font.bold: true
                    color: Theme.text
                    wrapMode: Text.Wrap
                }
                Label {
                    Layout.fillWidth: true
                    text: "중복 사진 찾기와 AI 자동분류를 바로 쓸 수 있어요. 원본 폴더 안에는 별도 파일을 만들지 않아요."
                    color: Theme.textMuted
                    wrapMode: Text.Wrap
                }
                FlatButton {
                    objectName: "localFolderChoose"
                    text: "폴더 선택"
                    tone: "primary"
                    // Hidden once a folder opens: clicked with the mouse it must not keep the keys.
                    focusPolicy: Qt.TabFocus
                    onClicked: view.openFolderPicker()
                }
                Label {
                    objectName: "localFolderError"
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: view.checkingFolder ? "폴더를 확인하는 중…" : view.folder ? view.folder.folderError : ""
                    color: view.checkingFolder ? Theme.notice : Theme.error
                    wrapMode: Text.Wrap
                }
                Label {
                    visible: !!view.folder && view.folder.recentFolders.length > 0
                    text: "최근 연 폴더"
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSmall
                }
                FocusScrollView {
                    id: recentScroll
                    visible: !!view.folder && view.folder.recentFolders.length > 0
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredHeight: recentColumn.implicitHeight
                    Layout.minimumHeight: Math.min(recentColumn.implicitHeight, 44)
                    clip: true
                    contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ColumnLayout {
                        id: recentColumn
                        width: recentScroll.availableWidth
                        spacing: Theme.gapSmall
                        Repeater {
                            model: view.folder ? view.folder.recentFolders.slice(0, 8) : []
                            delegate: RowLayout {
                                id: recentRow
                                required property string modelData
                                Layout.fillWidth: true
                                spacing: Theme.gapSmall
                                FlatButton {
                                    id: recentButtonItem
                                    objectName: "localFolderRecent"
                                    Layout.fillWidth: true
                                    tone: "ghost"
                                    compact: true
                                    focusPolicy: Qt.TabFocus
                                    elideMode: Text.ElideMiddle
                                    text: recentRow.modelData
                                    ToolTip.visible: hovered && recentButtonItem.truncated
                                    ToolTip.delay: 500
                                    ToolTip.text: recentRow.modelData
                                    onClicked: if (view.folder) view.folder.openFolder(recentRow.modelData)
                                }
                                // Off the list, and the folder's record too when asked (forgetDialog).
                                FlatButton {
                                    objectName: "localFolderForget"
                                    text: "×"
                                    tone: "ghost"
                                    compact: true
                                    focusPolicy: Qt.TabFocus
                                    tip: "최근 폴더에서 빼기"
                                    Accessible.name: "최근 폴더에서 빼기"
                                    onClicked: {
                                        forgetDialog.path = recentRow.modelData
                                        forgetDialog.open()
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // --- A folder is open.
    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.gapSmall
        visible: view.opened

        // Folder bar: opened path and folder-level switches, regular size matching Gallery chrome.
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            Label {
                id: folderPathLabel
                objectName: "localFolderPath"
                Layout.fillWidth: true
                Layout.minimumWidth: 80
                text: view.folder ? view.folder.folderPath : ""
                elide: Text.ElideMiddle
                color: Theme.text
                font.bold: true
                textFormat: Text.PlainText
                HoverHandler { id: folderPathHover }
                ToolTip.visible: folderPathHover.hovered && folderPathLabel.truncated
                ToolTip.delay: 500
                ToolTip.text: folderPathLabel.text
            }
            FlatSwitch {
                objectName: "localFolderSubfolders"
                text: "하위 폴더 포함"
                focusPolicy: Qt.TabFocus
                checked: !!view.folder && view.folder.includeSubfolders
                enabled: !!view.folder && !view.folder.busy
                onToggled: {
                    if (view.folder)
                        view.folder.setIncludeSubfolders(checked)
                    checked = Qt.binding(function() { return !!view.folder && view.folder.includeSubfolders })
                }
            }
            FlatSwitch {
                objectName: "localFolderShareLearning"
                text: "AI 학습 공유"
                focusPolicy: Qt.TabFocus
                checked: !!view.folder && view.folder.shareLearning
                enabled: !!view.folder && !view.folder.filesLocked && !view.folder.busy
                onToggled: {
                    if (view.folder)
                        view.folder.setShareLearning(checked)
                    checked = Qt.binding(function() { return !!view.folder && view.folder.shareLearning })
                }
                ToolTip.visible: hovered
                ToolTip.delay: 500
                ToolTip.text: "이 폴더에서 가르친 AI 태그를 갤러리와 함께 쓰는 AI 학습에 반영합니다"
            }
            FlatButton {
                id: recentButton
                objectName: "localFolderRecentButton"
                text: "최근 폴더"
                dropdown: true
                dropdownOpen: recentMenu.visible
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
                onClicked: recentMenu.popup(recentButton, 0, recentButton.height + 4)
            }
            FlatButton {
                objectName: "localFolderChange"
                text: "폴더 변경"
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
                onClicked: view.openFolderPicker()
            }
        }

        // Row 1: Search, sort, media type, and thumbnail density — matching Gallery's toolbar.
        RowLayout {
            objectName: "localFolderSearchRow"
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            FlatField {
                id: searchField
                objectName: "localFolderSearch"
                Layout.fillWidth: true
                Layout.minimumWidth: 120
                placeholderText: "파일명 검색"
                Accessible.name: "파일명 검색"
                selectByMouse: true
                rightPadding: searchClearButton.visible ? 36 : 14
                onTextEdited: {
                    if (localWheel)
                        localWheel.reset()
                    searchDelay.restart()
                }
                onPreeditTextChanged: {
                    if (activeFocus)
                        searchDelay.restart()
                }
                onAccepted: {
                    if (localWheel)
                        localWheel.reset()
                    searchDelay.stop()
                    if (view.folder)
                        view.folder.search(displayText)
                }
                Keys.onEscapePressed: event => {
                    if (displayText.length > 0) {
                        if (localWheel)
                            localWheel.reset()
                        searchDelay.stop()
                        clearText()
                        if (view.folder)
                            view.folder.search("")
                        event.accepted = true
                    } else {
                        view.takeFocus()
                        event.accepted = true
                    }
                }
                ToolButton {
                    id: searchClearButton
                    anchors.right: parent.right
                    anchors.rightMargin: 6
                    anchors.verticalCenter: parent.verticalCenter
                    width: 24
                    height: 24
                    visible: searchField.displayText.length > 0
                    focusPolicy: Qt.NoFocus
                    Accessible.name: "검색어 지우기"
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    contentItem: Text {
                        text: "×"
                        font.pixelSize: Theme.fontSection
                        font.bold: true
                        color: searchClearButton.hovered ? Theme.text : Theme.textMuted
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: Theme.radiusCard
                        color: searchClearButton.hovered ? Theme.raisedHover : "transparent"
                    }
                    onClicked: {
                        if (localWheel)
                            localWheel.reset()
                        searchDelay.stop()
                        searchField.clearText()
                        if (view.folder)
                            view.folder.search("")
                        searchField.forceActiveFocus()
                    }
                }
            }
            FlatCombo {
                id: orderCombo
                objectName: "localFolderOrder"
                label: "정렬"
                model: ["수정일순", "이름순", "크기순"]
                enabled: !!view.folder && !view.duplicateMode
                implicitWidth: 152
                focusPolicy: Qt.TabFocus
                escapeFocus: grid
                Accessible.name: "사진 정렬"
                readonly property var orderKeys: ["modified", "name", "size"]
                currentIndex: {
                    const idx = view.folder ? orderKeys.indexOf(view.folder.folderOrder) : 0
                    return idx >= 0 ? idx : 0
                }
                onActivated: index => {
                    if (view.folder && orderKeys[index])
                        view.folder.setFolderOrder(orderKeys[index])
                    currentIndex = Qt.binding(function() {
                        const idx = view.folder ? orderKeys.indexOf(view.folder.folderOrder) : 0
                        return idx >= 0 ? idx : 0
                    })
                }
            }
            FlatCombo {
                id: mediaTypeCombo
                objectName: "localFolderMediaType"
                label: "종류"
                model: ["전체", "사진", "GIF"]
                enabled: !!view.folder && !view.duplicateMode
                implicitWidth: 116
                focusPolicy: Qt.TabFocus
                escapeFocus: grid
                Accessible.name: "미디어 종류"
                readonly property var mediaKeys: ["all", "photo", "animated_gif"]
                currentIndex: {
                    const idx = view.folder ? mediaKeys.indexOf(view.folder.folderMediaType) : 0
                    return idx >= 0 ? idx : 0
                }
                onActivated: index => {
                    if (view.folder && mediaKeys[index])
                        view.folder.setFolderMediaType(mediaKeys[index])
                    currentIndex = Qt.binding(function() {
                        const idx = view.folder ? mediaKeys.indexOf(view.folder.folderMediaType) : 0
                        return idx >= 0 ? idx : 0
                    })
                }
            }
            FlatCombo {
                id: densityCombo
                objectName: "localFolderDensity"
                label: "크기"
                model: ["작게", "보통", "크게"]
                currentIndex: view.densityChoice
                implicitWidth: 116
                focusPolicy: Qt.TabFocus
                escapeFocus: view.duplicateMode ? duplicateReview : grid
                Accessible.name: "사진 크기"
                onActivated: index => {
                    view.densitySelected(index)
                    currentIndex = Qt.binding(function() { return view.densityChoice })
                }
            }
        }

        // Row 2: Quick filter chips on the left, preview/refresh/select on the right.
        RowLayout {
            objectName: "localFolderFilterRow"
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            WrapRow {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                FlatButton {
                    objectName: "localFolderFavorites"
                    focusPolicy: Qt.TabFocus
                    tone: "chip"
                    width: implicitWidth
                    checkable: true
                    checked: !!view.folder && view.folder.filterFavoriteOnly
                    text: checked ? "★ 즐겨찾기" : "☆ 즐겨찾기"
                    Accessible.name: "즐겨찾기 필터"
                    tip: checked ? "즐겨찾기 필터 해제 (B)" : "즐겨찾기한 사진만 보기 (B)"
                    onClicked: {
                        if (view.folder)
                            view.folder.setFavoriteOnly(!view.folder.filterFavoriteOnly)
                        checked = Qt.binding(function() { return !!view.folder && view.folder.filterFavoriteOnly })
                    }
                }
                FlatButton {
                    objectName: "localFolderDuplicates"
                    focusPolicy: Qt.TabFocus
                    tone: "chip"
                    width: implicitWidth
                    checkable: true
                    checked: view.duplicateMode
                    text: {
                        const count = view.folder ? view.folder.duplicateCount : 0
                        return count > 0 ? "중복 사진 " + count.toLocaleString() : "중복 사진"
                    }
                    Accessible.name: "중복 사진 필터"
                    tip: checked ? "중복 사진 보기 끝내기 (D)" : "중복 사진 보기 및 정리 (D)"
                    onClicked: {
                        if (view.folder)
                            view.folder.setDuplicateOnly(!view.duplicateMode)
                        checked = Qt.binding(function() { return view.duplicateMode })
                    }
                }
                FlatButton {
                    id: aiButton
                    objectName: "localFolderAiMenuButton"
                    focusPolicy: Qt.TabFocus
                    tone: "chip"
                    width: implicitWidth
                    readonly property string applied: view.folder ? view.folder.filterAiCategory : ""
                    chosen: applied.length > 0
                    dropdown: true
                    dropdownOpen: aiMenu.visible
                    text: view.folder && view.folder.aiScanning ? "AI 분류 중…"
                        : (applied.length ? "AI 분류: " + applied : "AI 분류")
                    Accessible.name: applied.length ? "AI 분류 " + applied + " 보는 중" : "AI 분류"
                    tip: !aiMenu.visible ? "AI 분류 보기 · 실행" : ""
                    onClicked: aiMenu.popup(aiButton, 0, aiButton.height + 4)
                }
                FilterChipCombo {
                    id: userTagCombo
                    objectName: "localFolderUserTag"
                    chosen: !!view.folder && view.folder.filterUserTag.length > 0
                    maxChipWidth: 200
                    minimumPopupWidth: 260
                    escapeFocus: view.duplicateMode ? duplicateReview : grid
                    Accessible.name: "내 태그 선택"
                    readonly property var rawTags: view.folder ? view.folder.userTags : []
                    readonly property string activeTag: view.folder ? view.folder.filterUserTag : ""
                    readonly property var tags: activeTag.length > 0 && rawTags.findIndex(item => item.tag === activeTag) < 0
                        ? rawTags.concat([{ tag: activeTag, count: 0 }]) : rawTags
                    model: ["내 태그 전체"].concat(tags.map(item => "#" + item.tag + " · " + Number(item.count || 0).toLocaleString(Qt.locale(), "f", 0) + "개"))
                    displayText: currentIndex > 0 && tags[currentIndex - 1] ? "#" + tags[currentIndex - 1].tag : "내 태그"
                    currentIndex: {
                        if (!view.folder || !view.folder.filterUserTag)
                            return 0
                        const found = tags.findIndex(item => item.tag === view.folder.filterUserTag)
                        return found >= 0 ? found + 1 : 0
                    }
                    onActivated: index => {
                        if (view.folder)
                            view.folder.setUserTag(index > 0 && tags[index - 1] ? tags[index - 1].tag : "")
                        currentIndex = Qt.binding(function() {
                            if (!view.folder || !view.folder.filterUserTag)
                                return 0
                            const found = tags.findIndex(item => item.tag === view.folder.filterUserTag)
                            return found >= 0 ? found + 1 : 0
                        })
                    }
                }
                FlatButton {
                    objectName: "localFolderFilterReset"
                    text: "× 필터 지우기"
                    tone: "ghost"
                    focusPolicy: Qt.TabFocus
                    Accessible.name: "필터 초기화"
                    visible: !!view.folder && (view.folder.filterSummary.length > 0 || searchField.displayText.length > 0
                        || mediaTypeCombo.currentIndex > 0 || orderCombo.currentIndex > 0
                        || view.folder.filterFavoriteOnly || view.folder.filterDuplicateOnly)
                    onClicked: {
                        searchDelay.stop()
                        searchField.clearText()
                        if (view.folder)
                            view.folder.resetFilters()
                    }
                }
            }
            FlatButton {
                objectName: "localFolderOpenPreview"
                focusPolicy: Qt.TabFocus
                Layout.alignment: Qt.AlignTop
                text: "미리보기"
                visible: grid.currentIndex >= 0 && !view.duplicateMode
                tip: "고른 사진을 크게 보기 (Enter)"
                onClicked: previewPopup.open()
            }
            FlatButton {
                objectName: "localFolderRefresh"
                focusPolicy: Qt.TabFocus
                Layout.alignment: Qt.AlignTop
                tone: "quiet"
                text: "새로고침"
                enabled: !!view.folder && !view.folder.busy
                tip: "목록 다시 읽기 (F5)"
                onClicked: if (view.folder) view.folder.refreshFolder()
            }
            FlatButton {
                objectName: "localFolderSelectionToggle"
                focusPolicy: Qt.TabFocus
                Layout.alignment: Qt.AlignTop
                readonly property bool choosing: !!view.folder && view.folder.selectionMode
                tone: choosing ? "chip" : "quiet"
                chosen: choosing
                text: choosing ? "선택 끝내기" : "여러 개 선택"
                enabled: !!view.folder
                tip: choosing ? "여러 개 선택 끝내기 (Esc)" : "여러 장을 골라 한 번에 정리하기"
                onClicked: if (view.folder) view.folder.setSelectionMode(!view.folder.selectionMode)
            }
        }

        Rectangle {
            objectName: "localFolderOverlap"
            Layout.fillWidth: true
            visible: !!view.folder && view.folder.overlapsXLibrary
            implicitHeight: overlapText.implicitHeight + Theme.gap
            radius: Theme.radiusControl
            color: Theme.noticeSoft
            Label {
                id: overlapText
                anchors.fill: parent
                anchors.margins: Theme.gapSmall
                verticalAlignment: Text.AlignVCenter
                text: "이 폴더는 X 사진 저장 폴더와 겹칩니다. 여기서 휴지통으로 옮긴 사진은 갤러리에 ‘파일 없음’으로 "
                    + "남고, 다음 저장에서 다시 받을 수 있습니다. X에서 받은 사진은 갤러리에서 정리하세요."
                color: Theme.notice
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSmall
            }
        }

        RowLayout {
            id: localStatusLine
            Layout.fillWidth: true
            Layout.minimumHeight: 28
            spacing: Theme.gapSmall
            readonly property bool loadFailed: !!view.folder && view.folder.folderModel.error.length > 0
            readonly property bool working: !!view.folder && (view.checkingFolder || view.folder.folderModel.busy
                || view.folder.scanningFolder)
            readonly property bool isManagerError: view.keyNotice.length === 0 && view.statusSource === "manager"
                && !!view.manager && view.manager.error.length > 0
            Label {
                text: !view.folder ? "0개" : Number(view.folder.folderModel.total).toLocaleString(Qt.locale(), "f", 0) + "개"
                font.bold: true
                color: Theme.text
            }
            Item {
                implicitWidth: 20
                implicitHeight: 20
                visible: localStatusLine.working
                BusyIndicator {
                    anchors.centerIn: parent
                    running: view.windowShown && localStatusLine.working
                    implicitWidth: 20
                    implicitHeight: 20
                }
            }
            Label {
                id: localActiveFilters
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: view.folder ? view.folder.filterSummary : ""
                visible: text.length > 0
                elide: Text.ElideRight
                textFormat: Text.PlainText
                color: Theme.textMuted
                HoverHandler { id: localActiveFiltersHover }
                ToolTip.text: text
                ToolTip.delay: 500
                ToolTip.visible: localActiveFiltersHover.hovered && truncated
            }
            Item { Layout.fillWidth: true; Layout.minimumWidth: 0; visible: !localActiveFilters.visible }
            Label {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.maximumWidth: Math.max(160, localStatusLine.width - 220)
                text: view.folder ? view.folder.folderModel.error : ""
                visible: localStatusLine.loadFailed
                color: Theme.error
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            Label {
                id: noticeText
                objectName: "localFolderStatus"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.maximumWidth: Math.max(160, localStatusLine.width * 0.62)
                text: view.statusText
                visible: !localStatusLine.loadFailed && text.length > 0
                horizontalAlignment: Text.AlignRight
                wrapMode: Text.WordWrap
                maximumLineCount: 3
                elide: Text.ElideRight
                textFormat: Text.PlainText
                readonly property bool showingFolderStatus: view.keyNotice.length === 0
                    && (view.statusSource === "folder" || view.managerText.length === 0)
                color: localStatusLine.isManagerError ? Theme.error
                    : (showingFolderStatus ? Theme.notice : Theme.textMuted)
                HoverHandler { id: localNoticeHover }
                ToolTip.text: noticeText.text
                ToolTip.delay: 500
                ToolTip.visible: localNoticeHover.hovered && noticeText.truncated
                Connections {
                    target: view.folder
                    function onChanged() {
                        const text = view.folder ? view.folder.status : ""
                        if (text === view.seenFolderStatus)
                            return
                        view.seenFolderStatus = text
                        if (text.length > 0) {
                            view.flashNotice("")
                            view.statusSource = "folder"
                            view.seenManagerStatus = ""
                        }
                    }
                }
                Connections {
                    target: view.manager
                    function onChanged() {
                        const text = view.managerText
                        if (text === view.seenManagerStatus)
                            return
                        view.seenManagerStatus = text
                        if (text.length > 0) {
                            view.flashNotice("")
                            view.statusSource = "manager"
                            view.seenFolderStatus = ""
                        }
                    }
                }
            }
            ProgressBar {
                objectName: "localFolderProgress"
                Layout.preferredWidth: 140
                Layout.preferredHeight: 4
                readonly property bool aiWork: !!view.folder && view.folder.aiProgressTotal > 0
                readonly property bool duplicateWork: !!view.folder && view.folder.duplicateProgressTotal > 0
                readonly property bool preparingAi: !!view.folder && view.folder.aiScanning && !aiWork && !duplicateWork
                readonly property bool preparingDup: !!view.folder && view.folder.copiesLocked
                    && (!duplicateWork || view.folder.duplicateProgressCurrent >= view.folder.duplicateProgressTotal)
                visible: !!view.folder && view.folder.busy && (aiWork || duplicateWork || preparingAi || preparingDup)
                indeterminate: view.windowShown && (preparingAi || preparingDup)
                from: 0
                to: aiWork ? view.folder.aiProgressTotal : (duplicateWork ? view.folder.duplicateProgressTotal : 1)
                value: aiWork ? view.folder.aiProgressCurrent : (duplicateWork ? view.folder.duplicateProgressCurrent : 0)
                Accessible.name: "작업 진행"
            }
            FlatButton {
                objectName: "localFolderCancelDuplicate"
                text: "검사 중단"
                compact: true
                focusPolicy: Qt.TabFocus
                visible: !!view.folder && view.folder.copiesLocked
                Accessible.name: "중복 검사 중단"
                onClicked: if (view.folder) view.folder.cancelDuplicateScan()
            }
            FlatButton {
                objectName: "localFolderCancel"
                text: view.folder && view.folder.aiScanning ? "분류 중단" : "중단"
                compact: true
                focusPolicy: Qt.TabFocus
                visible: !!view.folder && view.folder.aiCancellable
                Accessible.name: "AI 분류 중단"
                onClicked: if (view.folder) view.folder.cancelAiScan()
            }
        }

        // Duplicate tools: same layout and regular controls as Gallery's duplicateStrip.
        Card {
            objectName: "localFolderDuplicateTools"
            Layout.fillWidth: true
            visible: view.duplicateMode
            implicitHeight: dupToolsLayout.implicitHeight + 2 * Theme.gap
            GridLayout {
                id: dupToolsLayout
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Theme.gapLarge
                anchors.rightMargin: Theme.gap
                columns: width >= dupGuideRow.implicitWidth + dupButtonsRow.implicitWidth + columnSpacing ? 2 : 1
                columnSpacing: Theme.gapLarge
                rowSpacing: Theme.gapSmall
                RowLayout {
                    id: dupGuideRow
                    Layout.fillWidth: true
                    spacing: Theme.gapSmall
                    Label {
                        Layout.fillWidth: true
                        Layout.maximumWidth: implicitWidth
                        text: "‘남김’은 보관, ‘사본’은 휴지통으로"
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        font.pixelSize: Theme.fontSmall
                        color: Theme.textMuted
                    }
                    HelpButton {
                        id: localDuplicateHelp
                        objectName: "localDuplicateHelp"
                        title: "중복 사진 정리"
                        cardWidth: 440
                        content: [
                            DuplicateHelpRow {
                                badge: "남김"; badgeColor: Theme.keepFill
                                text: "묶음에서 보관할 사진입니다."
                            },
                            DuplicateHelpRow {
                                badge: "사본"; badgeColor: Theme.accentLine
                                text: "같은 파일입니다. ‘사본 일괄 선택’으로 고를 수 있습니다."
                            },
                            DuplicateHelpRow {
                                badge: "비슷함"; badgeColor: Theme.lineStrong
                                text: "크기·화질만 다른 같은 사진입니다. ‘비교’에서 한 장씩 정리하세요."
                            },
                            DuplicateHelpRow {
                                badge: "중복 아님"; badgeColor: Theme.raised
                                text: "묶음 전체나 선택한 사진을 중복에서 뺍니다."
                            },
                            Label {
                                Layout.fillWidth: true
                                Layout.topMargin: Theme.gapTiny
                                text: "클릭 선택 · 더블 클릭 미리보기"
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textFaint
                            }
                        ]
                    }
                    Item { Layout.fillWidth: true }
                }
                GridLayout {
                    id: dupButtonsRow
                    columns: dupToolsLayout.width < 920 ? 3 : 5
                    columnSpacing: Theme.gapSmall
                    rowSpacing: Theme.gapSmall
                    Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                    FlatButton {
                        id: nearChip
                        objectName: "localFolderNear"
                        tone: "chip"
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && !view.folder.busy
                        checkable: true
                        checked: view.duplicateNear
                        text: "비슷한 사진도 검사"
                        Accessible.name: "크기나 화질만 다른 비슷한 사진도 찾기"
                        tip: "크기·화질이 다른 같은 사진도 찾기"
                        onClicked: {
                            view.duplicateNear = !view.duplicateNear
                            checked = Qt.binding(function() { return view.duplicateNear })
                        }
                    }
                    FlatButton {
                        objectName: "localFolderScanDuplicates"
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && !view.folder.busy
                        text: "중복 검사"
                        tip: view.duplicateNear ? "같은 사진과 비슷한 사진 찾기" : "완전히 같은 파일 찾기"
                        onClicked: {
                            if (view.folder && view.folder.scanDuplicates(view.duplicateNear))
                                view.flashNotice("중복 검사 중…")
                        }
                    }
                    FlatButton {
                        objectName: "localFolderSelectDuplicates"
                        text: "사본 일괄 선택"
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && !view.folder.copiesLocked && view.folder.folderModel.count > 0
                        tip: view.folder && view.folder.copiesLocked ? view.folder.holdReason : "불러온 사본 선택"
                        onClicked: {
                            if (!view.folder)
                                return
                            view.folder.selectDuplicateCopies()
                            view.flashNotice(view.folder.status)
                        }
                    }
                    FlatButton {
                        objectName: "localFolderUndoDistinct"
                        text: "중복 아님 되돌리기"
                        focusPolicy: Qt.TabFocus
                        visible: !!view.folder && view.folder.canUndoDistinct
                        enabled: !!view.folder && !view.folder.copiesLocked
                        tip: "마지막 ‘중복 아님’ 표시 취소"
                        Accessible.name: "중복 아님 표시 되돌리기"
                        onClicked: if (view.folder) view.folder.undoDistinct()
                    }
                    FlatButton {
                        objectName: "localFolderClearDistinct"
                        text: "중복 아님 " + (view.folder ? view.folder.distinctDuplicateCount.toLocaleString() : "0") + "개 초기화"
                        focusPolicy: Qt.TabFocus
                        visible: !!view.folder && view.folder.distinctDuplicateCount > 0
                        enabled: !!view.folder && !view.folder.copiesLocked
                        tip: "모든 ‘중복 아님’ 표시 취소"
                        Accessible.name: "중복 아님 표시 모두 초기화"
                        onClicked: if (view.folder) view.folder.clearDistinct()
                    }
                }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            DuplicateReview {
                id: duplicateReview
                objectName: "localDuplicateReview"
                anchors.fill: parent
                visible: view.duplicateMode
                app: view.app
                library: view.folder
                manager: view.manager
                workBusy: !!view.manager && view.manager.locked
                galleryModel: view.folder ? view.folder.folderModel : null
                tileWidth: view.tileWidth
                onPreviewRequested: key => view.openPreview(key)
            }

            GridView {
                id: grid
                objectName: "localFolderGrid"
                anchors.fill: parent
                visible: !view.duplicateMode
                clip: true
                focus: true
                activeFocusOnTab: true
                interactive: false
                reuseItems: true
                keyNavigationEnabled: !view.duplicateMode
                Keys.enabled: !view.duplicateMode
                keyNavigationWraps: false
                highlightMoveDuration: 0
                currentIndex: -1
                boundsBehavior: Flickable.StopAtBounds
                readonly property int columns: Math.max(1, Math.floor(width / Math.max(1, cellWidth)))
                cellWidth: Math.max(1, Math.floor(width / Math.max(1, Math.floor(width / view.tileWidth))))
                cellHeight: Math.round(view.tileWidth * 0.75) + 48
                cacheBuffer: cellHeight
                model: view.folder ? view.folder.folderModel : null
                ScrollBar.vertical: ScrollBar {
                    policy: grid.contentHeight > grid.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                    onPressedChanged: if (pressed && localWheel) localWheel.reset()
                }
                SmoothWheel {
                    id: localWheel
                    flickable: grid
                    notchPixels: Math.max(120, Math.round(grid.cellHeight * 0.6))
                }
                onContentYChanged: {
                    view.requestMore()
                    Qt.callLater(view.updateFocus)
                }
                onCountChanged: {
                    Qt.callLater(view.requestMore)
                    Qt.callLater(view.updateFocus)
                }
                onWidthChanged: {
                    if (localWheel) localWheel.reset()
                    Qt.callLater(view.updateFocus)
                }
                onHeightChanged: {
                    if (localWheel) localWheel.reset()
                    Qt.callLater(view.updateFocus)
                }
                onCellWidthChanged: {
                    if (localWheel) localWheel.reset()
                    Qt.callLater(view.updateFocus)
                }
                onCellHeightChanged: {
                    if (localWheel) localWheel.reset()
                    if (view.pendingAnchor >= 0)
                        view.placeAnchor()
                    Qt.callLater(view.updateFocus)
                }
                onOriginYChanged: {
                    if (localWheel) localWheel.reset()
                    Qt.callLater(view.updateFocus)
                }
                onContentHeightChanged: {
                    if (view.pendingAnchor >= 0)
                        view.placeAnchor()
                    Qt.callLater(view.updateFocus)
                }
                onVisibleChanged: if (visible) Qt.callLater(view.updateFocus)
                Connections {
                    target: view.folder ? view.folder.folderModel : null
                    function onModelReset() {
                        if (localWheel)
                            localWheel.reset()
                        grid.currentIndex = -1
                        Qt.callLater(view.updateFocus)
                    }
                }
                onCurrentItemChanged: {
                    const currentTile = currentItem as FolderTile
                    if (currentIndex >= 0 && view.folder && currentTile && currentTile.tileKey
                            && view.folder.selectedKey !== currentTile.tileKey) {
                        view.folder.selectItem(currentTile.tileKey)
                        positionViewAtIndex(currentIndex, GridView.Contain)
                    }
                }
                Keys.onPressed: event => {
                    if (localWheel)
                        localWheel.reset()
                    event.accepted = view.handleGridKey(event)
                }

                // Dragging draws a rectangle and picks the tiles it touches, as in the gallery
                // (Ctrl or Shift held adds to what is picked).
                GridBandSelection {
                    objectName: "localFolderBandDrag"
                    grid: grid
                    columns: grid.columns
                    enabled: !!view.folder && !view.modalOpen && !view.windowModalOpen
                    bandObjectName: "localFolderBand"
                    wheelReset: function() { if (localWheel) localWheel.reset() }
                    onBandStarted: additive => view.folder.beginBandSelection(additive)
                    onBandUpdated: (row0, row1, col0, col1, cols) => view.folder.bandSelect(row0, row1, col0, col1, cols)
                    onBandEnded: view.folder.endBandSelection()
                }

                component FolderTile: Item {
                    id: cell
                    required property int index
                    required property string key
                    required property string caption
                    required property string thumbUrl
                    required property bool thumbReady
                    required property bool thumbMissing
                    required property bool favorite
                    required property string detail
                    readonly property string tileKey: cell.key
                    property bool activeTile: true
                    readonly property bool chosen: !!view.folder && view.folder.selectionRevision >= 0
                        && view.folder.isSelected(cell.key)
                    readonly property bool imageReady: thumbImage.status === Image.Ready
                    readonly property bool imageMissing: !cell.imageReady
                        && (cell.thumbMissing || thumbImage.status === Image.Error)
                    width: grid.cellWidth
                    height: grid.cellHeight
                    GridView.onPooled: activeTile = false
                    GridView.onReused: activeTile = true
                    Accessible.role: Accessible.ListItem
                    Accessible.name: cell.caption
                    Accessible.selected: view.folder && view.folder.selectionMode ? cell.chosen : grid.currentIndex === cell.index
                    Accessible.onPressAction: {
                        grid.forceActiveFocus()
                        grid.currentIndex = cell.index
                        if (view.folder) {
                            if (view.folder.selectionMode)
                                view.folder.toggleSelection(cell.key)
                            else
                                view.folder.selectItem(cell.key)
                        }
                    }
                    Rectangle {
                        objectName: "localFolderTile"
                        readonly property string key: cell.key
                        anchors.fill: parent
                        anchors.margins: 6
                        radius: Theme.radiusCard
                        color: cell.chosen || grid.currentIndex === cell.index
                            ? Theme.accentSoft : (tileMouse.containsMouse ? Theme.card : "transparent")
                        border.width: cell.chosen || grid.currentIndex === cell.index ? 1 : 0
                        border.color: cell.chosen ? Theme.accent : Theme.accentLine
                        Rectangle {
                            anchors.fill: thumbImage
                            radius: Theme.radiusControl
                            color: Theme.card
                            visible: !cell.imageMissing
                        }
                        Image {
                            id: thumbImage
                            objectName: "localFolderThumb"
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: 8
                            height: Math.round(view.tileWidth * 0.75) - 16
                            source: cell.activeTile && cell.thumbReady && !cell.thumbMissing ? cell.thumbUrl : ""
                            asynchronous: true
                            cache: false
                            fillMode: Image.PreserveAspectFit
                            sourceSize.width: view.thumbDecodeW
                            sourceSize.height: view.thumbDecodeH
                        }
                        TileMissingPlaceholder {
                            anchors.fill: thumbImage
                            active: cell.imageMissing
                            missingFile: cell.thumbMissing
                            captionText: cell.caption
                        }
                        Label {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            anchors.margins: 10
                            text: cell.caption
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                            font.pixelSize: Theme.fontCaption
                            color: Theme.textMuted
                        }
                        MouseArea {
                            id: tileMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            onPressed: if (localWheel) localWheel.reset()
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            cursorShape: Qt.PointingHandCursor
                            onClicked: mouse => {
                                grid.forceActiveFocus()
                                if (!view.folder)
                                    return
                                if (mouse.button === Qt.RightButton) {
                                    grid.currentIndex = cell.index
                                    teachMenu.singleOnly = false
                                    teachMenu.targetKey = cell.key
                                    const pt = tileMouse.mapToItem(grid, mouse.x, mouse.y)
                                    teachMenu.popup(grid, pt.x, pt.y)
                                    return
                                }
                                if (mouse.modifiers & Qt.ShiftModifier) {
                                    view.folder.selectRangeTo(cell.key)
                                } else if (view.folder.selectionMode || (mouse.modifiers & Qt.ControlModifier)) {
                                    view.folder.toggleSelection(cell.key)
                                } else {
                                    view.folder.setSelectionAnchor(cell.key)
                                    view.folder.selectItem(cell.key)
                                }
                                grid.currentIndex = cell.index
                            }
                            onDoubleClicked: mouse => {
                                if (mouse.button === Qt.LeftButton && (!view.folder || !view.folder.selectionMode))
                                    view.openPreview(cell.key)
                            }
                        }
                        Rectangle {
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 12
                            z: 3
                            width: 28
                            height: 28
                            radius: height / 2
                            color: cell.favorite ? Theme.accent : (favMouse.containsMouse ? Theme.lineStrong : Theme.scrim)
                            visible: cell.favorite || tileMouse.containsMouse || favMouse.containsMouse
                            Text {
                                anchors.centerIn: parent
                                text: "★"
                                font.pixelSize: Theme.fontBody
                                color: cell.favorite || favMouse.containsMouse ? "white" : Theme.textMuted
                            }
                            MouseArea {
                                id: favMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                ToolTip.delay: 500
                                ToolTip.visible: containsMouse && !pressed
                                ToolTip.text: cell.favorite ? "즐겨찾기 해제 (F)" : "즐겨찾기 추가 (F)"
                                onClicked: {
                                    if (view.folder)
                                        view.folder.toggleFavorite(cell.key)
                                }
                            }
                        }
                        TileSelectionCheck {
                            id: tileCheck
                            objectName: "tileCheck"
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.margins: 6
                            z: 2
                            visible: !!view.folder && view.folder.selectionMode
                            enabled: visible && !cell.thumbMissing && !!view.folder && view.folder.canSelect(cell.key)
                            checkedState: cell.chosen
                            captionText: cell.caption
                            onSelectionToggled: {
                                grid.forceActiveFocus()
                                grid.currentIndex = cell.index
                                if (view.folder)
                                    view.folder.toggleSelection(cell.key)
                            }
                        }
                    }
                }
                delegate: FolderTile {}
            }

            Label {
                objectName: "localFolderEmptyList"
                anchors.centerIn: parent
                width: Math.max(0, parent.width - 32)
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: !!view.folder && !view.duplicateMode && !view.folder.folderModel.busy
                    && view.folder.folderModel.error.length === 0 && view.folder.folderModel.count === 0
                text: {
                    if (view.folder && view.folder.scanningFolder)
                        return "폴더를 읽는 중…"
                    if (view.folder && view.folder.filterFavoriteOnly && !searchField.displayText.length
                            && mediaTypeCombo.currentIndex === 0 && !view.folder.filterUserTag
                            && !view.folder.filterAiCategory)
                        return "아직 즐겨찾기가 없어요 · 사진의 ★이나 F 키로 담아 보세요"
                    if (view.folder && (view.folder.filterSummary.length > 0 || searchField.displayText.length > 0
                            || mediaTypeCombo.currentIndex > 0))
                        return "조건에 맞는 사진이 없어요"
                    return "이 폴더에 사진이 없어요"
                }
                color: Theme.textMuted
            }
        }

        // Picked photos: what can be done with all of them at once (same layout and order as Gallery).
        Card {
            objectName: "localFolderSelectionBar"
            Layout.fillWidth: true
            visible: !!view.folder && view.folder.selectionMode
            border.color: Theme.accentLine
            glow: 0.12
            implicitHeight: localSelectionLayout.implicitHeight + 2 * Theme.gapSmall
            GridLayout {
                id: localSelectionLayout
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Theme.gapLarge
                anchors.rightMargin: Theme.gapSmall
                columns: width >= localSelectionControls.implicitWidth + localSelectionActions.implicitWidth + columnSpacing ? 2 : 1
                columnSpacing: Theme.gap
                rowSpacing: Theme.gapTiny
                RowLayout {
                    id: localSelectionControls
                    Layout.fillWidth: true
                    spacing: Theme.gapTiny
                    Label {
                        Layout.fillWidth: true
                        text: view.folder ? Number(view.folder.selectedCount).toLocaleString(Qt.locale(), "f", 0) + "개 선택" : ""
                        font.bold: true
                        color: Theme.text
                        elide: Text.ElideRight
                    }
                    FlatButton {
                        objectName: "localFolderSelectLoaded"
                        text: "불러온 항목 선택"
                        tone: "ghost"
                        focusPolicy: Qt.TabFocus
                        tip: "지금 불러온 사진을 모두 선택 (Ctrl+A)"
                        onClicked: if (view.folder) view.folder.selectLoaded()
                    }
                    FlatButton {
                        objectName: "localFolderSelectionClear"
                        text: "선택 해제"
                        tone: "ghost"
                        focusPolicy: Qt.TabFocus
                        onClicked: if (view.folder) view.folder.clearSelection()
                    }
                }
                RowLayout {
                    id: localSelectionActions
                    Layout.alignment: Qt.AlignRight
                    spacing: Theme.gapSmall
                    FlatButton {
                        objectName: "localFolderTeachSelected"
                        text: "분류 가르치기"
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && view.folder.selectedCount > 0 && !view.folder.tagsLocked && !view.folder.filesLocked
                        tip: view.folder && view.folder.holdReason ? view.folder.holdReason : "AI 분류 지정 · 닮은 사진에도 적용"
                        onClicked: teachPopup.open()
                    }
                    FlatButton {
                        objectName: "localFolderTagSelected"
                        text: "태그 추가"
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && view.folder.selectedCount > 0 && !view.folder.tagsLocked && !view.folder.busy
                        tip: view.folder && view.folder.tagsLocked ? view.folder.holdReason : ""
                        onClicked: tagPopup.open()
                    }
                    FlatButton {
                        objectName: "localFolderRecycleSelected"
                        text: "휴지통으로"
                        tone: "danger"
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && !!view.manager && view.folder.selectedCount > 0 && view.lockReason.length === 0
                            && !view.manager.mutating
                        tip: view.lockReason
                        onClicked: view.manager.prepareDelete(view.folder.selectedKeys)
                    }
                }
            }
        }
    }

    Timer {
        id: searchDelay
        interval: 180
        onTriggered: if (view.folder) view.folder.search(searchField.displayText)
    }

    FlatMenu {
        id: recentMenu
        objectName: "localFolderRecentMenu"
        width: Math.min(420, Math.max(240, implicitWidth))
        Instantiator {
            model: view.folder ? view.folder.recentFolders.slice(0, 8) : []
            FlatMenuItem {
                id: recentMenuItem
                required property string modelData
                elideMode: Text.ElideMiddle
                text: modelData
                checkable: true
                checked: !!view.folder && view.folder.folderPath === modelData
                ToolTip.delay: 500
                ToolTip.visible: hovered && truncated
                ToolTip.text: modelData
                onTriggered: {
                    if (view.folder)
                        view.folder.openFolder(recentMenuItem.modelData)
                    checked = Qt.binding(function() { return !!view.folder && view.folder.folderPath === recentMenuItem.modelData })
                }
            }
            onObjectAdded: (index, object) => recentMenu.insertItem(index, object)
            onObjectRemoved: (index, object) => recentMenu.removeItem(object)
        }
        FlatMenuSeparator {
            visible: !!view.folder && view.folder.recentFolders.length > 0
        }
        FlatMenuItem {
            objectName: "localFolderCloseAction"
            text: "폴더 닫기 (최근 목록 보기)"
            enabled: !!view.folder && !view.folder.busy
            onTriggered: if (view.folder) view.folder.closeFolder()
        }
    }

    FlatMenu {
        id: aiMenu
        objectName: "localFolderAiMenu"
        readonly property var counts: view.folder ? view.folder.aiCategoryCounts : []
        readonly property string applied: view.folder ? view.folder.filterAiCategory : ""
        function amount(value) { return Number(value || 0).toLocaleString(Qt.locale(), "f", 0) }
        FlatMenuItem {
            text: "모든 사진"
            checkable: true
            checked: aiMenu.applied.length === 0
            onTriggered: {
                if (view.folder)
                    view.folder.setAiFilter("", "")
                checked = Qt.binding(function() { return aiMenu.applied.length === 0 })
            }
        }
        Instantiator {
            model: aiMenu.counts
            FlatMenuItem {
                id: localAiItem
                required property var modelData
                objectName: "localFolderAiCategory_" + (modelData.category || "")
                text: (modelData.category || "") + "  ·  " + aiMenu.amount(modelData.count) + "장"
                checkable: true
                checked: aiMenu.applied === modelData.category
                onTriggered: {
                    if (view.folder)
                        view.folder.setAiFilter(localAiItem.modelData.category, "")
                    checked = Qt.binding(function() { return aiMenu.applied === localAiItem.modelData.category })
                }
            }
            onObjectAdded: (index, object) => aiMenu.insertItem(index + 1, object)
            onObjectRemoved: (index, object) => aiMenu.removeItem(object)
        }
        FlatMenuSeparator {}
        FlatMenuItem {
            objectName: "localFolderAiManage"
            text: "AI 태그 관리…"
            onTriggered: aiManager.open()
        }
        FlatMenuItem {
            objectName: "localFolderAiClassify"
            text: view.folder && view.folder.aiScanning ? "분류 중단" : "AI 자동분류 실행"
            enabled: !!view.folder && (view.folder.aiScanning || !view.folder.busy)
            onTriggered: {
                if (!view.folder)
                    return
                if (view.folder.aiScanning)
                    view.folder.cancelAiScan()
                else if (view.folder.scanAiTags())
                    view.flashNotice("AI 자동분류 중…")
            }
        }
        FlatMenuItem {
            objectName: "localFolderAiClear"
            text: "AI 태그 초기화…"
            enabled: !!view.folder && !view.folder.busy
            onTriggered: clearConfirm.open()
        }
    }

    FlatMenu {
        id: teachMenu
        property string targetKey: ""
        property bool singleOnly: false
        readonly property bool batchTarget: !singleOnly && !!view.folder && view.folder.selectionMode
            && view.folder.selectedCount > 1 && view.folder.isSelected(targetKey)
        FlatMenuItem {
            visible: !view.folder || view.folder.aiCategories.length === 0
            enabled: false
            text: "켜져 있는 AI 태그가 없습니다"
        }
        Instantiator {
            model: view.folder ? view.folder.aiCategories : []
            FlatMenuItem {
                required property string modelData
                text: "#" + modelData
                enabled: !!view.folder && !view.folder.tagsLocked
                onTriggered: {
                    if (!view.folder || view.folder.tagsLocked)
                        return
                    if (teachMenu.batchTarget)
                        view.teachCategory(modelData, true)
                    else
                        view.folder.teachOne(teachMenu.targetKey, modelData)
                }
            }
            onObjectAdded: (index, object) => teachMenu.insertItem(index + 1, object)
            onObjectRemoved: (index, object) => teachMenu.removeItem(object)
        }
        FlatMenuSeparator {}
        FlatMenuItem {
            text: "AI 태그 모두 아님"
            enabled: !!view.folder && !view.folder.tagsLocked
            onTriggered: {
                if (!view.folder || view.folder.tagsLocked)
                    return
                if (teachMenu.batchTarget)
                    view.teachCategory("", true)
                else
                    view.folder.teachOne(teachMenu.targetKey, "")
            }
        }
        FlatMenuItem {
            text: "AI 태그 관리…"
            onTriggered: aiManager.open()
        }
    }

    AiTagManagerDialog {
        id: aiManager
        objectName: "localFolderAiManager"
        parent: view.popupParent || Overlay.overlay
        app: view.app
        library: view.folder
        shareLearning: view.folder ? view.folder.shareLearning : true
    }

    FlatDialog {
        id: clearConfirm
        objectName: "localFolderAiClearConfirm"
        parent: view.popupParent || Overlay.overlay
        title: "이 폴더의 AI 태그를 초기화할까요?"
        preferredWidth: 460
        onOpened: localClearCancel.forceActiveFocus()
        contentItem: Label {
            text: "자동분류로 붙은 AI 태그만 지웁니다. 직접 추가한 태그와 사진 파일은 그대로 둡니다."
            wrapMode: Text.Wrap
            color: Theme.textMuted
        }
        footer: DialogButtons {
            FlatButton {
                text: "AI 태그 초기화"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                id: localClearCancel
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onAccepted: if (view.folder) view.folder.clearAiTags()
            onRejected: clearConfirm.close()
        }
    }

    // A recent folder off the list. Its record (what LuvBox keeps about it in the app data folder)
    // goes only with the switch on; the folder and its photos are never touched.
    FlatDialog {
        id: forgetDialog
        objectName: "localFolderForgetDialog"
        parent: view.popupParent || Overlay.overlay
        property string path: ""
        title: "최근 폴더에서 뺄까요?"
        preferredWidth: 480
        windowMargin: 48
        readonly property int windowLimit: parent ? Math.max(200, parent.height - 48) : 520
        readonly property int chromeHeight: topPadding + bottomPadding
            + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
            + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
        height: Math.min(forgetColumn.implicitHeight + chromeHeight + 4, windowLimit)
        onOpened: {
            forgetErase.checked = false
            forgetCancel.forceActiveFocus()
        }
        onAccepted: if (view.folder) view.folder.forgetFolder(forgetDialog.path, forgetErase.checked)
        contentItem: FocusScrollView {
            id: forgetScroll
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                id: forgetColumn
                width: forgetScroll.availableWidth
                spacing: Theme.gap
                Label {
                    Layout.fillWidth: true
                    text: forgetDialog.path
                    textFormat: Text.PlainText
                    wrapMode: Text.WrapAnywhere
                    color: Theme.text
                }
                Label {
                    Layout.fillWidth: true
                    text: "폴더와 사진은 그대로 둡니다."
                    color: Theme.textMuted
                    wrapMode: Text.Wrap
                }
                FlatSwitch {
                    id: forgetErase
                    objectName: "localFolderForgetErase"
                    Layout.fillWidth: true
                    trailing: true
                    wrap: true
                    text: "이 폴더의 정리 기록도 지우기 (태그·즐겨찾기·중복 아님·AI 분류 정보)"
                    Accessible.name: text
                }
            }
        }
        footer: DialogButtons {
            FlatButton {
                objectName: "localFolderForgetConfirm"
                text: "빼기"
                tone: "primary"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            FlatButton {
                id: forgetCancel
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
    }

    BatchTagDialog {
        id: tagPopup
        objectName: "localFolderTagPopup"
        inputObjectName: "localFolderBatchTag"
        parent: view.popupParent || Overlay.overlay
        title: "선택한 사진에 태그 추가"
        locked: !view.folder || view.folder.tagsLocked
        lockTip: view.folder && view.folder.tagsLocked ? view.folder.holdReason : ""
        onSubmitRequested: tag => tagPopup.submitTo(view.folder, tag)
    }

    Popup {
        id: teachPopup
        objectName: "localFolderTeachPopup"
        parent: view.popupParent || Overlay.overlay
        modal: true
        focus: true
        anchors.centerIn: parent
        width: 340
        height: Math.min(420, parent ? Math.max(220, parent.height - 48) : 420)
        padding: 16
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property string notice: ""
        onOpened: {
            localTeachNewTagField.clearText()
            teachPopup.notice = ""
            if (localTeachNewTagField.enabled) {
                localTeachNewTagField.forceActiveFocus()
                return
            }
            for (let i = 0; i < localTeachCategories.count; i++) {
                const btn = localTeachCategories.itemAt(i)
                if (btn && btn.enabled) {
                    btn.forceActiveFocus()
                    return
                }
            }
            if (localTeachRefuse.enabled)
                localTeachRefuse.forceActiveFocus()
        }
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
                text: "선택한 사진에 이 AI 태그가 맞다고 알려 줍니다. 닮은 사진에도 적용됩니다."
                color: Theme.text
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.gapSmall
                FlatField {
                    id: localTeachNewTagField
                    objectName: "localFolderTeachNewField"
                    readonly property string cleanTag: displayText.trim().replace(/^#+/, "").trim()
                    Layout.fillWidth: true
                    implicitHeight: Theme.compactHeight
                    leftPadding: Theme.gap
                    rightPadding: Theme.gap
                    maximumLength: 50
                    placeholderText: "새 AI 태그 만들어 바로 지정"
                    font.pixelSize: Theme.fontSmall
                    enabled: !!view.folder && !view.folder.tagsLocked && view.folder.shareLearning
                    onTextChanged: teachPopup.notice = ""
                    onAccepted: if (localTeachNewTagAdd.enabled) localTeachNewTagAdd.clicked()
                }
                FlatButton {
                    id: localTeachNewTagAdd
                    objectName: "localFolderTeachNewAdd"
                    text: "추가·지정"
                    compact: true
                    enabled: !!view.folder && !view.folder.tagsLocked && view.folder.shareLearning
                        && localTeachNewTagField.cleanTag.length > 0
                    tip: view.folder && !view.folder.shareLearning
                        ? "AI 학습 공유가 꺼져 있어 새 AI 태그를 만들 수 없습니다"
                        : (view.folder && view.folder.tagsLocked ? view.folder.holdReason : "")
                    onClicked: {
                        const clean = localTeachNewTagField.cleanTag
                        if (!clean || !view.folder || !view.folder.shareLearning)
                            return
                        if (view.folder.addAiTag(clean)) {
                            view.teachCategory(clean, true)
                            localTeachNewTagField.clearText()
                            teachPopup.close()
                        } else {
                            teachPopup.notice = view.folder.status || "AI 태그를 추가하지 못했습니다."
                        }
                    }
                }
            }
            Label {
                Layout.fillWidth: true
                visible: !!view.folder && !view.folder.shareLearning
                text: "‘AI 학습 공유’가 꺼져 있어 새 AI 태그를 만들 수 없습니다. 기존 AI 태그만 적용할 수 있습니다."
                color: Theme.textMuted
                font.pixelSize: Theme.fontCaption
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            Label {
                Layout.fillWidth: true
                visible: teachPopup.notice.length > 0
                text: teachPopup.notice
                color: Theme.error
                font.pixelSize: Theme.fontSmall
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            FocusScrollView {
                id: teachScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ColumnLayout {
                    width: teachScroll.availableWidth
                    spacing: Theme.gapSmall
                    Label {
                        Layout.fillWidth: true
                        visible: !view.folder || view.folder.aiCategories.length === 0
                        text: "켜져 있는 AI 태그가 없습니다. AI 태그 관리에서 추가해 주세요."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                        wrapMode: Text.Wrap
                    }
                    Repeater {
                        id: localTeachCategories
                        model: view.folder ? view.folder.aiCategories : []
                        delegate: FlatButton {
                            required property string modelData
                            Layout.fillWidth: true
                            text: "#" + modelData
                            enabled: !!view.folder && !view.folder.tagsLocked
                            onClicked: {
                                view.teachCategory(modelData, true)
                                teachPopup.close()
                            }
                        }
                    }
                }
            }
            FlatButton {
                id: localTeachRefuse
                Layout.fillWidth: true
                text: "AI 태그 모두 아님"
                tip: "보이는 AI 태그를 모두 빼고 AI 학습에 반영합니다"
                enabled: !!view.folder && !view.folder.tagsLocked
                onClicked: {
                    view.teachCategory("", true)
                    teachPopup.close()
                }
            }
        }
    }

    // The photo large, matching Gallery's previewDialog.
    FlatDialog {
        id: previewPopup
        objectName: "localFolderPreview"
        parent: view.popupParent || Overlay.overlay
        title: "미리보기"
        light: 0.1
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        width: Math.max(0, parent ? parent.width - 24 : 1060)
        height: Math.max(0, parent ? parent.height - 24 : 780)
        x: parent ? Math.max(0, Math.round((parent.width - width) / 2)) : 0
        y: parent ? Math.max(0, Math.round((parent.height - height) / 2)) : 0
        padding: 16
        leftPadding: 16
        rightPadding: 16
        topPadding: 16
        bottomPadding: 16
        property string notice: ""
        property string noticeKey: ""
        property string _openedStatus: ""
        readonly property string openedKey: view.folder ? view.folder.selectedKey : ""
        readonly property bool shortcutActive: previewPopup.visible && !previewTagField.activeFocus
            && !deleteConfirmation.visible && !alikeConfirmation.visible && !teachMenu.visible
            && !aiManager.visible && !view.saveReviewOpen
        function toggleFavorite() {
            if (!view.folder || !view.folder.selectedKey)
                return
            const note = view.folder.favoriteNotice(view.folder.selectedKey)
            view.flashNotice(note)
            if (note && view.folder.status === note) {
                notice = note
                noticeKey = openedKey
            } else {
                notice = ""
                noticeKey = ""
            }
        }
        function resetScroll() {
            if (localPreviewMetaScroll.contentItem)
                localPreviewMetaScroll.contentItem.contentY = 0
        }
        onOpenedKeyChanged: {
            notice = ""
            noticeKey = ""
            _openedStatus = view.folder ? (view.folder.status || "") : ""
            previewTagField.clearText()
            resetScroll()
        }
        onAboutToShow: {
            notice = ""
            noticeKey = ""
            _openedStatus = view.folder ? (view.folder.status || "") : ""
            previewTagField.clearText()
            resetScroll()
            if (localWheel)
                localWheel.reset()
        }
        onOpened: localPreviewCloseButton.forceActiveFocus()
        onClosed: {
            previewTagField.clearText()
            localPreviewMedia.release()
        }
        Shortcut {
            sequence: "Left"
            enabled: previewPopup.shortcutActive && !!view.folder && view.folder.canPrevious
            onActivated: view.folder.previousItem()
        }
        Shortcut {
            sequence: "Right"
            enabled: previewPopup.shortcutActive && !!view.folder && view.folder.canNext
            onActivated: view.folder.nextItem()
        }
        Shortcut {
            sequence: "F"
            enabled: previewPopup.shortcutActive && !!view.folder && !!view.folder.selectedKey
            onActivated: previewPopup.toggleFavorite()
        }
        PreviewShortcuts {
            preview: localPreviewMedia
            shortcutsEnabled: previewPopup.shortcutActive
        }
        Shortcut {
            sequence: "Delete"
            enabled: previewPopup.shortcutActive && !!view.folder && !!view.folder.selectedKey && !!view.manager
            onActivated: {
                if (view.lockReason.length > 0) {
                    previewPopup.notice = view.lockReason
                    previewPopup.noticeKey = previewPopup.openedKey
                    view.flashNotice(view.lockReason)
                    return
                }
                if (view.manager.mutating || view.folder.selectedMissing)
                    return
                previewPopup.noticeKey = previewPopup.openedKey
                if (view.folder.selectedAlike)
                    view.manager.prepareAlikeDelete(view.folder.selectedKey)
                else
                    view.manager.prepareDelete([view.folder.selectedKey])
            }
        }
        header: RowLayout {
            spacing: 8
            Label {
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.topMargin: 12
                Layout.bottomMargin: 4
                text: previewPopup.title
                font.pixelSize: Theme.fontHeading
                font.bold: true
            }
            FlatButton {
                objectName: "localFolderPreviewMaximize"
                Layout.rightMargin: 12
                Layout.topMargin: 8
                tone: "ghost"
                readonly property bool isMax: !!view.hostWindow && (view.hostWindow.visibility === Window.Maximized
                    || view.hostWindow.visibility === Window.FullScreen)
                text: isMax ? "창 복원" : "창 최대화"
                onClicked: {
                    if (!view.hostWindow)
                        return
                    if (isMax)
                        view.hostWindow.showNormal()
                    else
                        view.hostWindow.showMaximized()
                }
            }
        }
        footer: ColumnLayout {
            width: parent ? parent.width : 0
            spacing: 8
            Label {
                objectName: "localFolderPreviewNotice"
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.rightMargin: 16
                visible: previewPopup.notice.length > 0
                text: previewPopup.notice
                color: Theme.error
                font.pixelSize: Theme.fontSmall
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
            Flow {
                visible: !!view.folder
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.rightMargin: 16
                spacing: 8
                FlatButton {
                    objectName: "localFolderPreviewOpen"
                    text: "파일 열기"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && !view.folder.selectedMissing
                    onClicked: {
                        previewPopup.notice = ""
                        previewPopup.noticeKey = previewPopup.openedKey
                        view.folder.openSelected()
                    }
                }
                FlatButton {
                    objectName: "localFolderPreviewReveal"
                    text: "폴더에서 보기"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && !view.folder.selectedMissing
                    onClicked: {
                        previewPopup.notice = ""
                        previewPopup.noticeKey = previewPopup.openedKey
                        view.folder.revealSelected()
                    }
                }
                FlatButton {
                    objectName: "localFolderPreviewRecycle"
                    readonly property bool alike: !!view.folder && view.folder.selectedAlike
                    text: alike ? "남김과 비교 후 휴지통으로" : "휴지통으로"
                    tone: "danger"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && !!view.manager && !view.folder.selectedMissing
                        && view.lockReason.length === 0 && !view.manager.mutating
                    tip: view.lockReason.length > 0 ? view.lockReason
                        : (alike ? "크기·화질만 다른 ‘비슷함’ 사진입니다. 묶음에 남길 사진과 나란히 비교한 뒤 이 사진만 휴지통으로 보냅니다 (Delete)" : "")
                    onClicked: {
                        previewPopup.noticeKey = previewPopup.openedKey
                        if (view.folder.selectedAlike)
                            view.manager.prepareAlikeDelete(view.folder.selectedKey)
                        else
                            view.manager.prepareDelete([view.folder.selectedKey])
                    }
                }
                FlatButton {
                    objectName: "localFolderPreviewFavorite"
                    text: view.folder && view.folder.selectedIsFavorite ? "★ 즐겨찾기 해제" : "☆ 즐겨찾기"
                    tone: "chip"
                    focusPolicy: Qt.TabFocus
                    chosen: !!view.folder && view.folder.selectedIsFavorite
                    tip: view.folder && view.folder.selectedIsFavorite ? "즐겨찾기 해제 (F)" : "즐겨찾기 추가 (F)"
                    onClicked: previewPopup.toggleFavorite()
                }
            }
            DialogButtons {
                Layout.fillWidth: true
                leftPadding: 16
                rightPadding: 16
                topPadding: 4
                bottomPadding: 14
                FlatButton {
                    id: localPreviewCloseButton
                    text: "닫기"
                    DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
                }
                onRejected: previewPopup.close()
            }
        }
        contentItem: ColumnLayout {
            id: previewBody
            spacing: 12
            clip: true
            Flow {
                id: localPreviewTopControls
                Layout.fillWidth: true
                spacing: 8
                FlatButton {
                    objectName: "localFolderPreviewPrevious"
                    text: "이전"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && view.folder.canPrevious
                    tip: "이전 사진 (←)"
                    onClicked: {
                        previewTagField.clearText()
                        view.folder.previousItem()
                    }
                }
                FlatButton {
                    objectName: "localFolderPreviewNext"
                    text: "다음"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && view.folder.canNext
                    tip: "다음 사진 (→)"
                    onClicked: {
                        previewTagField.clearText()
                        view.folder.nextItem()
                    }
                }
                PreviewZoomControls { preview: localPreviewMedia }
            }
            MediaPreview {
                id: localPreviewMedia
                objectName: "localFolderPreviewImage"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 24
                sourceUrl: previewPopup.visible && view.folder ? view.folder.selectedPreviewUrl : ""
                mediaType: view.folder ? view.folder.selectedMediaType : "photo"
                sourcePixelWidth: view.folder ? view.folder.selectedPixelWidth : 0
                sourcePixelHeight: view.folder ? view.folder.selectedPixelHeight : 0
                sourceRawWidth: view.folder ? view.folder.selectedRawWidth : 0
                sourceRawHeight: view.folder ? view.folder.selectedRawHeight : 0
                missingNote: view.folder ? view.folder.selectedMissingNote : ""
                externalBusy: view.folder ? view.folder.selectionBusy : false
                playing: previewPopup.visible && view.windowShown && !view.saveReviewOpen
            }
            FocusScrollView {
                id: localPreviewMetaScroll
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(localPreviewMetaColumn.implicitHeight,
                                                Math.max(36, previewBody.height - localPreviewTopControls.height - 48))
                Layout.maximumHeight: Math.max(36, previewBody.height - localPreviewTopControls.height - 48)
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: localPreviewMetaColumn.implicitHeight > height + 2
                    ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
                ColumnLayout {
                    id: localPreviewMetaColumn
                    width: localPreviewMetaScroll.availableWidth
                    spacing: 8
                    Label {
                        objectName: "localFolderPreviewInfo"
                        Layout.fillWidth: true
                        text: view.folder ? view.folder.selectedPublicInfo : ""
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                        elide: Text.ElideMiddle
                        textFormat: Text.PlainText
                    }
                    Flow {
                        id: localPreviewTags
                        Layout.fillWidth: true
                        spacing: 6
                        visible: !!view.folder && (view.folder.selectedTags.length > 0 || !!view.folder.selectedKey)
                        Repeater {
                            model: view.folder ? view.folder.selectedTags : []
                            delegate: PreviewTagChip {
                                objectName: "localFolderTagChip"
                                maxWidth: localPreviewTags.width
                                tagsLocked: !view.folder || view.folder.tagsLocked
                                teaches: !isAi && !!view.folder && view.folder.aiCategories.indexOf(tagName) >= 0
                                nameObjectName: "localFolderTagName"
                                confirmObjectName: "localFolderTagConfirm"
                                removeObjectName: "localFolderTagRemove"
                                confirmTip: view.folder && !view.folder.shareLearning
                                    ? qsTr("맞아요 · 학습 공유가 꺼져 있어 이 폴더에만 저장됩니다")
                                    : qsTr("맞아요 · AI 학습에 반영")
                                removeAiTip: view.folder && !view.folder.shareLearning
                                    ? qsTr("이 태그 아님 · 학습 공유가 꺼져 있어 이 폴더에만 저장됩니다")
                                    : qsTr("이 태그 아님 · AI 학습에 반영")
                                onTagActivated: (tagName, isAi) => {
                                    if (!view.folder)
                                        return
                                    if (isAi)
                                        view.folder.setAiFilter(tagName, "")
                                    else
                                        view.folder.setUserTag(tagName)
                                    previewPopup.close()
                                }
                                onConfirmRequested: tagName => {
                                    if (view.folder && view.folder.teachTag(view.folder.selectedKey, tagName, true)) {
                                        previewPopup.notice = ""
                                        previewPopup.noticeKey = previewPopup.openedKey
                                        view.flashNotice(qsTr("#%1 분류를 배우는 중…").arg(tagName))
                                    }
                                }
                                onRemoveRequested: tagName => {
                                    if (!view.folder)
                                        return
                                    previewPopup.notice = ""
                                    previewPopup.noticeKey = previewPopup.openedKey
                                    view.folder.removeSelectedTag(tagName)
                                }
                            }
                        }
                        RowLayout {
                            spacing: Theme.gapSmall
                            visible: !!view.folder && !!view.folder.selectedKey
                            FlatField {
                                id: previewTagField
                                objectName: "localFolderPreviewTagField"
                                readonly property string cleanTag: displayText.trim().replace(/^#+/, "").trim()
                                implicitHeight: Theme.compactHeight
                                Layout.preferredWidth: 132
                                leftPadding: Theme.gap
                                rightPadding: Theme.gap
                                maximumLength: 50
                                placeholderText: "새 태그"
                                font.pixelSize: Theme.fontSmall
                                enabled: !!view.folder && !view.folder.tagsLocked
                                Keys.onEscapePressed: event => {
                                    clearText()
                                    focus = false
                                    event.accepted = true
                                }
                                onAccepted: {
                                    if (cleanTag.length > 0 && view.folder && !view.folder.tagsLocked) {
                                        previewPopup.notice = ""
                                        previewPopup.noticeKey = previewPopup.openedKey
                                        if (view.folder.addSelectedTag(cleanTag))
                                            clearText()
                                    }
                                }
                            }
                            FlatButton {
                                objectName: "localFolderPreviewTagAdd"
                                text: "+ 태그"
                                compact: true
                                focusPolicy: Qt.TabFocus
                                enabled: previewTagField.cleanTag.length > 0 && !!view.folder && !view.folder.tagsLocked
                                tip: view.folder && view.folder.tagsLocked ? view.folder.holdReason : ""
                                onClicked: {
                                    if (view.folder && !view.folder.tagsLocked) {
                                        previewPopup.notice = ""
                                        previewPopup.noticeKey = previewPopup.openedKey
                                        if (view.folder.addSelectedTag(previewTagField.cleanTag))
                                            previewTagField.clearText()
                                    }
                                }
                            }
                            FlatButton {
                                id: previewAiButton
                                objectName: "localFolderPreviewAi"
                                text: "+ AI 태그"
                                compact: true
                                focusPolicy: Qt.TabFocus
                                visible: !!view.folder && view.folder.selectedMediaType === "photo"
                                enabled: !!view.folder && !view.folder.tagsLocked
                                tip: view.folder && !view.folder.shareLearning
                                    ? "AI 태그 지정 · 학습 공유가 꺼져 있어 이 폴더에만 저장됩니다"
                                    : "AI 태그 지정 · 닮은 사진에도 적용"
                                onClicked: {
                                    if (!view.folder)
                                        return
                                    previewPopup.notice = ""
                                    previewPopup.noticeKey = previewPopup.openedKey
                                    teachMenu.singleOnly = true
                                    teachMenu.targetKey = view.folder.selectedKey
                                    teachMenu.popup(previewAiButton, 0, previewAiButton.height + 4)
                                }
                            }
                        }
                    }
                    Label {
                        objectName: "localFolderPreviewStatus"
                        Layout.fillWidth: true
                        visible: text.length > 0 && previewPopup.notice.length === 0
                        text: view.folder ? view.folder.status : ""
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontCaption
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                }
            }
        }
    }

    DeleteConfirmation {
        id: deleteConfirmation
        objectName: "localDeleteConfirmation"
        parent: view.popupParent || Overlay.overlay
        app: view.app
        library: view.folder
        manager: view.manager
        showKeepDeleted: false
    }
    AlikeConfirmation {
        id: alikeConfirmation
        objectName: "localAlikeConfirmation"
        parent: view.popupParent || Overlay.overlay
        app: view.app
        manager: view.manager
        showKeepDeleted: false
    }
    Connections {
        target: view.folder
        function onChanged() {
            if (!view.folder)
                return
            const index = view.folder.selectedIndex
            if (index >= 0 && grid.currentIndex !== index) {
                grid.currentIndex = index
                if (previewPopup.visible)
                    grid.positionViewAtIndex(index, GridView.Contain)
            } else if (index < 0 && view.folder.selectedKey.length === 0 && grid.currentIndex !== -1) {
                grid.currentIndex = -1
            }
            if (previewPopup.visible && !view.folder.selectionBusy && view.folder.selectedKey.length === 0)
                previewPopup.close()
            if (previewPopup.visible) {
                const status = view.folder.status || ""
                if (status !== previewPopup._openedStatus) {
                    previewPopup._openedStatus = status
                    if (status.length > 0 && previewPopup.noticeKey === previewPopup.openedKey
                            && previewPopup.openedKey.length > 0
                            && (status.indexOf("못했습니다") >= 0 || status.indexOf("없습니다") >= 0
                                || status.indexOf("않습니다") >= 0 || status.indexOf("중단") >= 0
                                || status.indexOf("끝난 뒤") >= 0))
                        previewPopup.notice = status
                }
            }
        }
    }
    Connections {
        target: view.manager
        function onDeleteConfirmationRequested() {
            if (!view.manager || !view.app || view.app.closing)
                return
            view.resumePreview = previewPopup.visible
            localPreviewMedia.release()
            previewPopup.close()
            Qt.callLater(function() {
                if (!view.manager || !view.app || view.app.closing || !view.manager.deleteConfirmationPending)
                    return
                if (view.manager.deleteAlike && view.manager.deleteAlike.alike)
                    alikeConfirmation.open()
                else
                    deleteConfirmation.open()
            })
        }
        function onChanged() {
            if (previewPopup.visible && view.manager && view.manager.error.length > 0
                    && previewPopup.noticeKey.length > 0 && previewPopup.noticeKey === previewPopup.openedKey)
                previewPopup.notice = view.manager.error
            if (!view.resumePreview || !view.manager || view.manager.mutating
                    || view.manager.deleteConfirmationPending)
                return
            view.resumePreview = false
            if (view.currentScreen === 3 && view.folder && view.opened && (!view.app || !view.app.closing)
                    && !previewPopup.visible && (view.folder.selectionBusy || view.folder.selectedKey.length > 0))
                previewPopup.open()
        }
    }
}
