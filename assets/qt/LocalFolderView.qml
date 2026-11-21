import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

// "폴더 정리": a folder on this PC looked at like the gallery. Its duplicates are
// cleared with the gallery's duplicate view and the same Recycle Bin checks, and
// its photos get AI tags from the AI tags every library shares. Nothing is
// written into the folder itself.
Item {
    id: view
    objectName: "localFolderView"
    property var app: null
    property int tileWidth: 168
    property Item popupParent: null
    property bool windowModalOpen: false
    property bool windowShown: true
    readonly property var folder: app ? app.localFolder : null
    readonly property var manager: app ? app.localManagement : null
    readonly property bool opened: !!folder && folder.hasFolder
    readonly property bool duplicateMode: !!folder && folder.filterDuplicateOnly
    readonly property bool modalOpen: folderPicker.visible || previewPopup.visible || tagPopup.visible
        || teachPopup.visible || aiManager.visible || deleteConfirmation.visible || alikeConfirmation.visible
        || clearConfirm.visible || forgetDialog.visible
    readonly property bool menuOpen: aiMenu.visible || teachMenu.visible || orderMenu.visible || recentMenu.visible
    readonly property real thumbPixelRatio: Math.max(1, Screen.devicePixelRatio)
    readonly property int thumbDecodeW: Math.min(384, Math.max(1, Math.round((tileWidth - 20) * thumbPixelRatio)))
    readonly property int thumbDecodeH: Math.min(384, Math.max(1, Math.round((Math.round(tileWidth * 0.75) - 8) * thumbPixelRatio)))
    property real focusContentY: 0
    property bool resumePreview: false
    property string clickedKey: ""
    property bool clickedWasChosen: false
    // The status line, as in the gallery: the folder's work and the Recycle Bin's results; the one
    // that changed last speaks, and a key refused just now says why.
    property string statusSource: "folder"
    property string seenFolderStatus: ""
    property string seenManagerStatus: ""
    property string keyNotice: ""
    readonly property string managerText: manager ? (manager.error || manager.status) : ""
    readonly property string statusText: keyNotice.length > 0 ? keyNotice
        : (statusSource === "manager" && managerText.length > 0 ? managerText : (folder ? folder.status : ""))
    readonly property string lockReason: !!manager && manager.locked ? "저장 중에는 X 저장 폴더와 겹치는 이 폴더의 사진을 휴지통으로 옮기지 않습니다. 저장이 끝난 뒤 다시 시도해 주세요."
        : (!!folder && folder.filesLocked ? folder.holdReason : "")

    // Made when the tab is first shown: the folder opened last opens again then.
    Component.onCompleted: if (folder) folder.openLastFolder()
    // The keys go to what is shown: the list once a folder is open, the groups in the duplicate view.
    onOpenedChanged: {
        Qt.callLater(takeFocus)
        Qt.callLater(updateFocus)
    }
    onDuplicateModeChanged: {
        Qt.callLater(takeFocus)
        Qt.callLater(updateFocus)
    }
    onTileWidthChanged: Qt.callLater(updateFocus)

    function takeFocus() {
        if (opened && !duplicateMode)
            grid.forceActiveFocus()
        else
            view.forceActiveFocus()
    }

    function updateFocus() {
        if (!folder || !opened || duplicateMode || grid.width <= 0 || grid.height <= 0
                || grid.cellWidth <= 0 || grid.cellHeight <= 0 || grid.contentHeight <= 0 || grid.count === 0)
            return
        const y = Math.max(0, grid.contentY - grid.originY)
        const direction = y >= view.focusContentY ? 1 : -1
        view.focusContentY = y
        const columns = Math.max(1, grid.columns)
        const first = Math.min(grid.count - 1, Math.floor(y / grid.cellHeight) * columns)
        const bottom = Math.min(grid.contentHeight, y + grid.height)
        const last = Math.min(grid.count - 1, Math.max(first, Math.ceil(bottom / grid.cellHeight) * columns - 1))
        folder.folderModel.focusViewport(first | 0, last | 0, (direction * columns * 2) | 0,
                                         view.thumbDecodeW | 0, view.thumbDecodeH | 0)
    }

    function closeMenus() {
        aiMenu.close()
        teachMenu.close()
        orderMenu.close()
        recentMenu.close()
    }

    // The application is closing: nothing of this tab stays open.
    function closeAll() {
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
            view.keyNotice = (category.length ? "#" + category : "AI 태그 모두 아님") + " 분류 저장 중…"
    }

    // Keys of this tab, as the gallery's: Ctrl+F search, F star, B favorite filter, D duplicates,
    // Space/Enter preview (or select in selectionMode), Delete Recycle Bin, Ctrl+A loaded, Esc step back, F5 refresh.
    function handleKey(event) {
        if (!folder || !opened || modalOpen || windowModalOpen || menuOpen || searchField.activeFocus
                || userTagCombo.activeFocus || userTagCombo.popup.visible)
            return false
        const ctrl = (event.modifiers & Qt.ControlModifier) !== 0
        const shift = (event.modifiers & Qt.ShiftModifier) !== 0
        const altMeta = (event.modifiers & (Qt.AltModifier | Qt.MetaModifier)) !== 0
        const plain = !ctrl && !shift && !altMeta
        const key = folder.selectedKey || (grid.currentItem ? grid.currentItem.tileKey : "")

        if (event.key === Qt.Key_F && ctrl && !shift && !altMeta) {
            searchField.forceActiveFocus()
            searchField.selectAll()
            return true
        }
        if (event.key === Qt.Key_A && ctrl && !altMeta) {
            folder.selectLoaded()
            return true
        }
        if (event.key === Qt.Key_F && plain && !duplicateMode && key) {
            keyNotice = folder.favoriteNotice(key)
            return true
        }
        if (event.key === Qt.Key_B && plain) {
            folder.setFavoriteOnly(!folder.filterFavoriteOnly)
            return true
        }
        if (event.key === Qt.Key_D && plain) {
            folder.setDuplicateOnly(!folder.filterDuplicateOnly)
            return true
        }
        if (event.key === Qt.Key_F5 && plain && !folder.busy) {
            folder.refreshFolder()
            return true
        }
        if ((event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                && plain && !duplicateMode && key) {
            if (folder.selectionMode)
                folder.toggleSelection(key)
            else
                previewPopup.open()
            return true
        }
        if (event.key === Qt.Key_Delete && plain && manager) {
            if (lockReason.length > 0) {
                keyNotice = lockReason
                return true
            }
            if (manager.mutating)
                return true
            if (folder.selectedCount > 0)
                manager.prepareDelete(folder.selectedKeys)
            else if (key) {
                if (folder.selectedAlike)
                    manager.prepareAlikeDelete(key)
                else
                    manager.prepareDelete([key])
            }
            return true
        }
        if (event.key === Qt.Key_Escape && plain) {
            if (folder.selectedCount > 0)
                folder.clearSelection()
            else if (folder.selectionMode)
                folder.setSelectionMode(false)
            else {
                grid.currentIndex = -1
                folder.forgetOpened()
            }
            return true
        }
        return false
    }

    Keys.onPressed: event => { event.accepted = view.handleKey(event) }

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
            height: emptyColumn.implicitHeight + Theme.cardPadding * 2
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
                    text: "중복 사진 찾기와 AI 자동분류를 바로 쓸 수 있어요. 폴더 안에는 아무것도 만들지 않습니다."
                    color: Theme.textMuted
                    wrapMode: Text.Wrap
                }
                FlatButton {
                    objectName: "localFolderChoose"
                    text: "폴더 선택"
                    tone: "primary"
                    // Hidden once a folder opens: clicked with the mouse it must not keep the keys.
                    focusPolicy: Qt.TabFocus
                    onClicked: folderPicker.open()
                }
                Label {
                    objectName: "localFolderError"
                    Layout.fillWidth: true
                    visible: !!view.folder && view.folder.folderError.length > 0
                    text: view.folder ? view.folder.folderError : ""
                    color: Theme.error
                    wrapMode: Text.Wrap
                }
                Label {
                    visible: !!view.folder && view.folder.recentFolders.length > 0
                    text: "최근 연 폴더"
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSmall
                }
                Repeater {
                    model: view.folder ? view.folder.recentFolders.slice(0, 8) : []
                    delegate: RowLayout {
                        id: recentRow
                        required property string modelData
                        Layout.fillWidth: true
                        spacing: Theme.gapSmall
                        FlatButton {
                            objectName: "localFolderRecent"
                            Layout.fillWidth: true
                            tone: "ghost"
                            compact: true
                            focusPolicy: Qt.TabFocus
                            text: recentRow.modelData
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

    // --- A folder is open.
    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.gapSmall
        visible: view.opened

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            Label {
                objectName: "localFolderPath"
                Layout.fillWidth: true
                text: view.folder ? view.folder.folderPath : ""
                elide: Text.ElideMiddle
                color: Theme.text
                font.bold: true
                textFormat: Text.PlainText
            }
            FlatButton {
                objectName: "localFolderChange"
                text: "폴더 변경"
                compact: true
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
                onClicked: folderPicker.open()
            }
            FlatButton {
                id: recentButton
                objectName: "localFolderRecentButton"
                text: "최근 폴더"
                compact: true
                dropdown: true
                dropdownOpen: recentMenu.visible
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
                onClicked: recentMenu.popup(recentButton, 0, recentButton.height + 4)
            }
            FlatButton {
                objectName: "localFolderSubfolders"
                text: "하위 폴더 포함"
                tone: "chip"
                compact: true
                checkable: true
                focusPolicy: Qt.TabFocus
                checked: !!view.folder && view.folder.includeSubfolders
                enabled: !!view.folder && !view.folder.busy
                onClicked: {
                    if (view.folder)
                        view.folder.setIncludeSubfolders(checked)
                    checked = Qt.binding(function() { return !!view.folder && view.folder.includeSubfolders })
                }
            }
            FlatField {
                id: searchField
                objectName: "localFolderSearch"
                Layout.preferredWidth: 200
                placeholderText: "파일명 검색 (Ctrl+F)"
                rightPadding: searchClearButton.visible ? 28 : 14
                onDisplayTextChanged: searchDelay.restart()
                Keys.onEscapePressed: event => {
                    searchDelay.stop()
                    text = ""
                    if (view.folder)
                        view.folder.search("")
                    view.takeFocus()
                    event.accepted = true
                }
                FlatButton {
                    id: searchClearButton
                    anchors.right: parent.right
                    anchors.rightMargin: 4
                    anchors.verticalCenter: parent.verticalCenter
                    visible: searchField.text.length > 0
                    tone: "ghost"
                    compact: true
                    leftPadding: 6
                    rightPadding: 6
                    text: "×"
                    Accessible.name: "검색어 지우기"
                    focusPolicy: Qt.TabFocus
                    onClicked: {
                        searchDelay.stop()
                        searchField.text = ""
                        if (view.folder)
                            view.folder.search("")
                        view.takeFocus()
                    }
                }
            }
            FlatButton {
                id: orderButton
                objectName: "localFolderOrder"
                compact: true
                dropdown: true
                dropdownOpen: orderMenu.visible
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.duplicateMode
                text: !view.folder ? "" : ({ "modified": "수정일순", "name": "이름순", "size": "크기순" })[view.folder.folderOrder] || "수정일순"
                onClicked: orderMenu.popup(orderButton, 0, orderButton.height + 4)
            }
        }

        WrapRow {
            Layout.fillWidth: true
            FlatButton {
                objectName: "localFolderFavorites"
                text: "★ 즐겨찾기"
                tone: "chip"
                compact: true
                checkable: true
                focusPolicy: Qt.TabFocus
                checked: !!view.folder && view.folder.filterFavoriteOnly
                onClicked: {
                    if (view.folder)
                        view.folder.setFavoriteOnly(checked)
                    checked = Qt.binding(function() { return !!view.folder && view.folder.filterFavoriteOnly })
                }
            }
            FlatButton {
                objectName: "localFolderDuplicates"
                text: "중복 사진" + (view.folder && view.folder.duplicateCount > 0 ? " " + view.folder.duplicateCount : "")
                tone: "chip"
                compact: true
                checkable: true
                focusPolicy: Qt.TabFocus
                checked: view.duplicateMode
                onClicked: {
                    if (view.folder)
                        view.folder.setDuplicateOnly(checked)
                    checked = Qt.binding(function() { return view.duplicateMode })
                }
            }
            FlatButton {
                objectName: "localFolderScanDuplicates"
                text: view.folder && view.folder.copiesLocked ? "중복 검사 중…" : "중복 검사"
                compact: true
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
                tip: "같은 사진 찾기 · 크기·화질만 다른 사진까지 찾으려면 오른쪽 ‘비슷한 사진도 검사’를 켜세요"
                onClicked: if (view.folder) view.folder.scanDuplicates(nearChip.checked)
            }
            FlatButton {
                id: nearChip
                objectName: "localFolderNear"
                text: "비슷한 사진도 검사"
                tone: "chip"
                compact: true
                checkable: true
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
            }
            FlatButton {
                id: aiButton
                objectName: "localFolderAiMenuButton"
                readonly property string applied: view.folder ? view.folder.filterAiCategory : ""
                text: view.folder && view.folder.aiScanning ? "AI 분류 중…"
                    : (applied.length ? "AI · #" + applied : "AI 분류")
                tone: "chip"
                compact: true
                chosen: applied.length > 0
                dropdown: true
                dropdownOpen: aiMenu.visible
                focusPolicy: Qt.TabFocus
                onClicked: aiMenu.popup(aiButton, 0, aiButton.height + 4)
            }
            FlatCombo {
                id: userTagCombo
                objectName: "localFolderUserTag"
                chip: true
                chosen: !!view.folder && view.folder.filterUserTag.length > 0
                implicitHeight: Theme.compactHeight
                minimumPopupWidth: 200
                typeAhead: true
                focusPolicy: Qt.TabFocus
                escapeFocus: grid
                readonly property var tags: view.folder ? view.folder.userTags : []
                model: ["내 태그 전체"].concat(tags.map(item => "#" + item.tag + " · " + Number(item.count || 0).toLocaleString(Qt.locale(), "f", 0)))
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
            FlatSwitch {
                objectName: "localFolderShareLearning"
                text: "AI 학습 공유"
                focusPolicy: Qt.TabFocus
                checked: !!view.folder && view.folder.shareLearning
                // A running classification or answer read the switch when it began.
                enabled: !!view.folder && !view.folder.tagsLocked
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
                objectName: "localFolderFilterReset"
                text: "× 필터 지우기"
                tone: "ghost"
                compact: true
                focusPolicy: Qt.TabFocus
                visible: !!view.folder && (view.folder.filterFavoriteOnly || view.folder.filterDuplicateOnly
                    || view.folder.filterAiCategory.length > 0 || view.folder.filterUserTag.length > 0
                    || searchField.text.length > 0)
                onClicked: {
                    searchDelay.stop()
                    searchField.text = ""
                    if (view.folder)
                        view.folder.resetFilters()
                }
            }
            FlatButton {
                objectName: "localFolderRefresh"
                text: "새로고침"
                compact: true
                focusPolicy: Qt.TabFocus
                enabled: !!view.folder && !view.folder.busy
                tip: "폴더를 다시 읽어 새로 생기거나 바뀐 파일을 반영합니다 (F5)"
                onClicked: if (view.folder) view.folder.refreshFolder()
            }
            FlatButton {
                objectName: "localFolderSelectionToggle"
                text: view.folder && view.folder.selectionMode ? "선택 끝내기" : "여러 개 선택"
                tone: "chip"
                compact: true
                focusPolicy: Qt.TabFocus
                checkable: true
                checked: !!view.folder && view.folder.selectionMode
                onClicked: {
                    if (view.folder)
                        view.folder.setSelectionMode(checked)
                    checked = Qt.binding(function() { return !!view.folder && view.folder.selectionMode })
                }
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
                    + "보이고, 다음 저장에서 다시 받을 수 있습니다. X에서 받은 사진은 갤러리에서 정리하세요."
                color: Theme.notice
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSmall
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            ProgressBar {
                objectName: "localFolderProgress"
                Layout.preferredWidth: 160
                Layout.preferredHeight: 4
                readonly property bool aiWork: !!view.folder && view.folder.aiProgressTotal > 0
                readonly property bool duplicateWork: !!view.folder && view.folder.duplicateProgressTotal > 0
                readonly property bool preparingAi: !!view.folder && view.folder.aiScanning && !aiWork
                readonly property bool preparingDup: !!view.folder && view.folder.copiesLocked
                    && (!duplicateWork || view.folder.duplicateProgressCurrent >= view.folder.duplicateProgressTotal)
                visible: !!view.folder && view.folder.busy && (aiWork || duplicateWork || preparingAi || preparingDup)
                indeterminate: view.windowShown && (preparingAi || preparingDup)
                from: 0
                to: aiWork ? view.folder.aiProgressTotal : (duplicateWork ? view.folder.duplicateProgressTotal : 1)
                value: aiWork ? view.folder.aiProgressCurrent : (duplicateWork ? view.folder.duplicateProgressCurrent : 0)
            }
            Label {
                id: noticeText
                objectName: "localFolderStatus"
                Layout.fillWidth: true
                text: view.statusText
                color: view.keyNotice.length === 0 && view.statusSource === "manager" && !!view.manager
                    && view.manager.error.length > 0 ? Theme.error : Theme.textMuted
                font.pixelSize: Theme.fontSmall
                elide: Text.ElideRight
                textFormat: Text.PlainText
                Connections {
                    target: view.folder
                    function onChanged() {
                        const text = view.folder ? view.folder.status : ""
                        if (text === view.seenFolderStatus)
                            return
                        view.seenFolderStatus = text
                        view.keyNotice = ""
                        if (text.length > 0)
                            view.statusSource = "folder"
                    }
                }
                Connections {
                    target: view.manager
                    function onChanged() {
                        const text = view.managerText
                        if (text === view.seenManagerStatus)
                            return
                        view.seenManagerStatus = text
                        view.keyNotice = ""
                        if (text.length > 0)
                            view.statusSource = "manager"
                    }
                }
            }
            Label {
                text: !view.folder ? "" : Number(view.folder.folderModel.total).toLocaleString(Qt.locale(), "f", 0) + "장"
                color: Theme.textFaint
                font.pixelSize: Theme.fontSmall
            }
            FlatButton {
                objectName: "localFolderCancel"
                text: "중단"
                compact: true
                tone: "ghost"
                focusPolicy: Qt.TabFocus
                visible: !!view.folder && (view.folder.aiScanning || view.folder.copiesLocked)
                onClicked: {
                    if (!view.folder)
                        return
                    if (view.folder.aiScanning)
                        view.folder.cancelAiScan()
                    else
                        view.folder.cancelDuplicateScan()
                }
            }
        }

        // The duplicate view's tools, as in the gallery: every copy picked at once, and the
        // "중복 아님" marks taken back or forgotten.
        Card {
            objectName: "localFolderDuplicateTools"
            Layout.fillWidth: true
            visible: view.duplicateMode
            implicitHeight: dupToolsLayout.implicitHeight + Theme.gap
            GridLayout {
                id: dupToolsLayout
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Theme.gap
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
                RowLayout {
                    id: dupButtonsRow
                    Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                    spacing: Theme.gapSmall
                    FlatButton {
                        objectName: "localFolderSelectDuplicates"
                        text: "사본 일괄 선택"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && !view.folder.copiesLocked && view.folder.folderModel.count > 0
                        tip: view.folder && view.folder.copiesLocked ? view.folder.holdReason : "불러온 사본 선택"
                        onClicked: view.folder.selectDuplicateCopies()
                    }
                    FlatButton {
                        objectName: "localFolderUndoDistinct"
                        text: "중복 아님 되돌리기"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        visible: !!view.folder && view.folder.canUndoDistinct
                        enabled: !!view.folder && !view.folder.copiesLocked
                        tip: "마지막 ‘중복 아님’ 표시 취소"
                        Accessible.name: "중복 아님 표시 되돌리기"
                        onClicked: view.folder.undoDistinct()
                    }
                    FlatButton {
                        objectName: "localFolderClearDistinct"
                        text: "중복 아님 " + (view.folder ? view.folder.distinctDuplicateCount.toLocaleString() : "0") + "개 초기화"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        visible: !!view.folder && view.folder.distinctDuplicateCount > 0
                        enabled: !!view.folder && !view.folder.copiesLocked
                        tip: "모든 ‘중복 아님’ 표시 취소"
                        Accessible.name: "중복 아님 표시 모두 초기화"
                        onClicked: view.folder.clearDistinct()
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
                interactive: false
                reuseItems: true
                keyNavigationEnabled: !view.duplicateMode
                Keys.enabled: !view.duplicateMode
                keyNavigationWraps: false
                highlightMoveDuration: 0
                currentIndex: -1
                boundsBehavior: Flickable.StopAtBounds
                readonly property int columns: Math.max(1, Math.floor(width / (view.tileWidth + 12)))
                cellWidth: Math.max(1, Math.floor(width / columns))
                cellHeight: Math.round(view.tileWidth * 0.75) + 50
                cacheBuffer: Math.max(400, Math.round(cellHeight * 3))
                model: view.folder ? view.folder.folderModel : null
                ScrollBar.vertical: ScrollBar {
                    policy: grid.contentHeight > grid.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                }
                SmoothWheel { flickable: grid; notchPixels: Math.max(120, Math.round(grid.cellHeight * 0.6)) }
                onContentYChanged: {
                    view.requestMore()
                    Qt.callLater(view.updateFocus)
                }
                onCountChanged: {
                    Qt.callLater(view.requestMore)
                    Qt.callLater(view.updateFocus)
                }
                onWidthChanged: Qt.callLater(view.updateFocus)
                onHeightChanged: Qt.callLater(view.updateFocus)
                onCellWidthChanged: Qt.callLater(view.updateFocus)
                onCellHeightChanged: Qt.callLater(view.updateFocus)
                onOriginYChanged: Qt.callLater(view.updateFocus)
                onContentHeightChanged: Qt.callLater(view.updateFocus)
                onVisibleChanged: if (visible) Qt.callLater(view.updateFocus)
                onCurrentItemChanged: {
                    if (currentIndex >= 0 && view.folder && currentItem && currentItem.tileKey
                            && view.folder.selectedKey !== currentItem.tileKey) {
                        view.folder.selectItem(currentItem.tileKey)
                        positionViewAtIndex(currentIndex, GridView.Contain)
                    }
                }
                Keys.onPressed: event => { event.accepted = view.handleKey(event) }

                // Dragging draws a rectangle and picks the tiles it touches, as in the gallery
                // (Ctrl or Shift held adds to what is picked).
                DragHandler {
                    id: bandDrag
                    objectName: "localFolderBandDrag"
                    target: null
                    acceptedDevices: PointerDevice.Mouse
                    acceptedButtons: Qt.LeftButton
                    enabled: !!view.folder && !view.modalOpen && !view.windowModalOpen
                    dragThreshold: 10
                    property real startX: 0
                    property real startY: 0
                    property real currentX: 0
                    property real currentY: 0
                    property int pressModifiers: Qt.NoModifier
                    onGrabChanged: (transition, point) => {
                        if (transition === PointerDevice.GrabPassive)
                            pressModifiers = centroid.modifiers
                    }
                    function update() {
                        // In content coordinates, so the rectangle grows while the grid scrolls under it.
                        view.folder.bandSelect(
                            Math.floor(Math.max(0, Math.min(startY, currentY)) / grid.cellHeight),
                            Math.floor(Math.max(0, Math.max(startY, currentY)) / grid.cellHeight),
                            Math.floor(Math.max(0, Math.min(startX, currentX)) / grid.cellWidth),
                            Math.floor(Math.max(0, Math.max(startX, currentX)) / grid.cellWidth), grid.columns)
                    }
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
                        if (!view.folder)
                            return
                        if (active) {
                            grid.forceActiveFocus()
                            const pressed = inView(centroid.scenePressPosition)
                            startX = Math.max(0, Math.min(grid.width, pressed.x))
                            startY = pressed.y + grid.contentY - grid.originY
                            const held = pressModifiers | centroid.modifiers
                            view.folder.beginBandSelection((held & (Qt.ControlModifier | Qt.ShiftModifier)) !== 0)
                            follow()
                        } else {
                            view.folder.endBandSelection()
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
                        const maximum = grid.originY + Math.max(0, grid.contentHeight - grid.height)
                        const next = Math.max(grid.originY, Math.min(maximum, grid.contentY + step))
                        if (next !== grid.contentY) {
                            grid.contentY = next
                            bandDrag.follow()
                        }
                    }
                }
                Rectangle {
                    objectName: "localFolderBand"
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

                delegate: Item {
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
                    Rectangle {
                        objectName: "localFolderTile"
                        readonly property string key: cell.key
                        anchors.fill: parent
                        anchors.margins: 4
                        radius: Theme.radiusCard
                        color: cell.chosen || grid.currentIndex === cell.index
                            ? Theme.accentSoft : (tileMouse.containsMouse ? Theme.raised : Theme.well)
                        border.width: view.folder && view.folder.selectedKey === cell.key ? 2 : (cell.chosen ? 1 : 0)
                        border.color: view.folder && view.folder.selectedKey === cell.key ? Theme.focus : Theme.accent
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
                            height: parent.height - 50
                            source: cell.activeTile && cell.thumbReady && !cell.thumbMissing ? cell.thumbUrl : ""
                            asynchronous: true
                            cache: false
                            fillMode: Image.PreserveAspectFit
                            sourceSize.width: view.thumbDecodeW
                            sourceSize.height: view.thumbDecodeH
                        }
                        Rectangle {
                            anchors.fill: thumbImage
                            radius: Theme.radiusControl
                            color: Theme.well
                            border.width: 1
                            border.color: Theme.line
                            visible: cell.imageMissing
                            Label {
                                anchors.centerIn: parent
                                width: parent.width - 12
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                                text: cell.thumbMissing ? "파일 없음" : "미리보기 없음"
                                font.pixelSize: Theme.fontTiny
                                color: Theme.textFaint
                            }
                        }
                        Column {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            anchors.margins: 8
                            Label {
                                width: parent.width
                                text: cell.caption
                                elide: Text.ElideMiddle
                                textFormat: Text.PlainText
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textMuted
                            }
                            Label {
                                width: parent.width
                                text: cell.detail
                                elide: Text.ElideRight
                                font.pixelSize: Theme.fontTiny
                                color: Theme.textFaint
                            }
                        }
                        MouseArea {
                            id: tileMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            cursorShape: Qt.PointingHandCursor
                            onClicked: mouse => {
                                grid.forceActiveFocus()
                                if (!view.folder)
                                    return
                                if (mouse.button === Qt.RightButton) {
                                    grid.currentIndex = cell.index
                                    teachMenu.targetKey = cell.key
                                    teachMenu.popup()
                                    return
                                }
                                grid.currentIndex = cell.index
                                if (mouse.modifiers & Qt.ShiftModifier) {
                                    view.folder.selectRangeTo(cell.key)
                                    return
                                }
                                if (view.folder.selectionMode || (mouse.modifiers & Qt.ControlModifier)) {
                                    view.clickedKey = cell.key
                                    view.clickedWasChosen = cell.chosen
                                    view.folder.toggleSelection(cell.key)
                                    view.folder.setSelectionAnchor(cell.key)
                                } else {
                                    view.folder.selectItem(cell.key)
                                    view.folder.setSelectionAnchor(cell.key)
                                }
                            }
                            onDoubleClicked: mouse => {
                                if (mouse.button !== Qt.LeftButton)
                                    return
                                if (view.folder && view.clickedKey === cell.key && cell.chosen !== view.clickedWasChosen)
                                    view.folder.toggleSelection(cell.key)
                                view.clickedKey = ""
                                view.openPreview(cell.key)
                            }
                        }
                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.margins: 10
                            z: 2
                            width: 22
                            height: 22
                            radius: Theme.radiusSmall
                            visible: !!view.folder && (view.folder.selectionMode || cell.chosen)
                            color: cell.chosen ? Theme.accent : Theme.scrim
                            border.width: cell.chosen ? 0 : 1
                            border.color: Theme.textMuted
                            Text {
                                anchors.centerIn: parent
                                visible: cell.chosen
                                text: "✓"
                                font.pixelSize: Theme.fontSmall
                                font.bold: true
                                color: "white"
                            }
                        }
                        Rectangle {
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            z: 3
                            width: 26
                            height: 26
                            radius: height / 2
                            color: cell.favorite ? Theme.accent : (favMouse.containsMouse ? Theme.lineStrong : Theme.scrim)
                            visible: cell.favorite || tileMouse.containsMouse || favMouse.containsMouse
                            Text {
                                anchors.centerIn: parent
                                text: "★"
                                font.pixelSize: Theme.fontSmall
                                color: cell.favorite || favMouse.containsMouse ? "white" : Theme.textMuted
                            }
                            MouseArea {
                                id: favMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                ToolTip.visible: containsMouse
                                ToolTip.text: cell.favorite ? "즐겨찾기 해제 (F)" : "즐겨찾기 추가 (F)"
                                onClicked: {
                                    if (view.folder)
                                        view.folder.toggleFavorite(cell.key)
                                }
                            }
                        }
                    }
                }
            }

            Label {
                objectName: "localFolderEmptyList"
                anchors.centerIn: parent
                visible: !!view.folder && !view.duplicateMode && !view.folder.folderModel.busy && view.folder.folderModel.count === 0
                text: {
                    if (view.folder && view.folder.scanningFolder)
                        return "폴더를 읽는 중…"
                    if (view.folder && (view.folder.filterFavoriteOnly || view.folder.filterAiCategory.length > 0
                            || view.folder.filterUserTag.length > 0 || searchField.text.length > 0))
                        return "조건에 맞는 사진이 없습니다"
                    return "이 폴더에 사진이 없습니다"
                }
                color: Theme.textMuted
            }
        }

        // Picked photos: what can be done with all of them at once.
        Card {
            objectName: "localFolderSelectionBar"
            Layout.fillWidth: true
            visible: !!view.folder && (view.folder.selectionMode || (view.duplicateMode && view.folder.selectedCount > 0))
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
                columns: width >= 760 ? 2 : 1
                columnSpacing: Theme.gap
                rowSpacing: Theme.gapTiny
                RowLayout {
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
                        text: "불러온 사진 모두"
                        tone: "ghost"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        tip: "지금 불러온 사진을 모두 선택 (Ctrl+A)"
                        onClicked: if (view.folder) view.folder.selectLoaded()
                    }
                    FlatButton {
                        objectName: "localFolderSelectionClear"
                        text: "선택 해제"
                        tone: "ghost"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        onClicked: if (view.folder) view.folder.clearSelection()
                    }
                }
                RowLayout {
                    Layout.alignment: Qt.AlignRight
                    spacing: Theme.gapSmall
                    FlatButton {
                        objectName: "localFolderTagSelected"
                        text: "태그 추가"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && view.folder.selectedCount > 0 && !view.folder.tagsLocked
                        tip: view.folder && view.folder.tagsLocked ? view.folder.holdReason : ""
                        onClicked: tagPopup.open()
                    }
                    FlatButton {
                        objectName: "localFolderTeachSelected"
                        text: "분류 가르치기"
                        compact: true
                        focusPolicy: Qt.TabFocus
                        enabled: !!view.folder && view.folder.selectedCount > 0 && !view.folder.tagsLocked
                        tip: view.folder && view.folder.tagsLocked ? view.folder.holdReason : ""
                        onClicked: teachPopup.open()
                    }
                    FlatButton {
                        objectName: "localFolderRecycleSelected"
                        text: "휴지통으로 이동"
                        tone: "danger"
                        compact: true
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
        Instantiator {
            model: view.folder ? view.folder.recentFolders.slice(0, 8) : []
            FlatMenuItem {
                required property string modelData
                text: modelData
                checkable: true
                checked: !!view.folder && view.folder.folderPath === modelData
                onTriggered: if (view.folder) view.folder.openFolder(modelData)
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
        id: orderMenu
        objectName: "localFolderOrderMenu"
        FlatMenuItem {
            text: "수정일순"
            checkable: true
            checked: !!view.folder && view.folder.folderOrder === "modified"
            onTriggered: if (view.folder) view.folder.setFolderOrder("modified")
        }
        FlatMenuItem {
            text: "이름순"
            checkable: true
            checked: !!view.folder && view.folder.folderOrder === "name"
            onTriggered: if (view.folder) view.folder.setFolderOrder("name")
        }
        FlatMenuItem {
            text: "크기순"
            checkable: true
            checked: !!view.folder && view.folder.folderOrder === "size"
            onTriggered: if (view.folder) view.folder.setFolderOrder("size")
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
            onTriggered: if (view.folder) view.folder.setAiFilter("", "")
        }
        Instantiator {
            model: aiMenu.counts
            FlatMenuItem {
                required property var modelData
                objectName: "localFolderAiCategory_" + (modelData.category || "")
                text: "#" + (modelData.category || "") + "  ·  " + aiMenu.amount(modelData.count) + "장"
                checkable: true
                checked: aiMenu.applied === modelData.category
                onTriggered: if (view.folder) view.folder.setAiFilter(modelData.category, "")
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
                else
                    view.folder.scanAiTags()
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
        Instantiator {
            model: view.folder ? view.folder.aiCategories : []
            FlatMenuItem {
                required property string modelData
                text: "#" + modelData
                enabled: !!view.folder && !view.folder.tagsLocked
                onTriggered: {
                    if (!view.folder)
                        return
                    if (view.folder.selectionMode && view.folder.selectedCount > 1 && view.folder.isSelected(teachMenu.targetKey))
                        view.teachCategory(modelData, true)
                    else
                        view.folder.teachOne(teachMenu.targetKey, modelData)
                }
            }
            onObjectAdded: (index, object) => teachMenu.insertItem(index, object)
            onObjectRemoved: (index, object) => teachMenu.removeItem(object)
        }
        FlatMenuSeparator {}
        FlatMenuItem {
            text: "AI 태그 모두 아님"
            enabled: !!view.folder && !view.folder.tagsLocked
            onTriggered: {
                if (!view.folder)
                    return
                if (view.folder.selectionMode && view.folder.selectedCount > 1 && view.folder.isSelected(teachMenu.targetKey))
                    view.teachCategory("", true)
                else
                    view.folder.teachOne(teachMenu.targetKey, "")
            }
        }
    }

    AiTagManagerDialog {
        id: aiManager
        objectName: "localFolderAiManager"
        parent: view.popupParent || Overlay.overlay
        app: view.app
        library: view.folder
    }

    FlatDialog {
        id: clearConfirm
        objectName: "localFolderAiClearConfirm"
        parent: view.popupParent || Overlay.overlay
        title: "이 폴더의 AI 태그를 초기화할까요?"
        preferredWidth: 460
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
        onOpened: {
            forgetErase.checked = false
            forgetCancel.forceActiveFocus()
        }
        onAccepted: if (view.folder) view.folder.forgetFolder(forgetDialog.path, forgetErase.checked)
        contentItem: ColumnLayout {
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
                text: "이 폴더의 정리 기록도 지우기 (태그·즐겨찾기·중복 아님·AI 분류 정보)"
                Accessible.name: text
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

    Popup {
        id: tagPopup
        objectName: "localFolderTagPopup"
        parent: view.popupParent || Overlay.overlay
        modal: true
        focus: true
        anchors.centerIn: parent
        width: 320
        padding: 16
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onOpened: {
            batchTag.text = ""
            batchTag.forceActiveFocus()
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
                text: "선택한 사진에 붙일 태그"
                font.bold: true
                font.pixelSize: Theme.fontSection
                color: Theme.text
            }
            FlatField {
                id: batchTag
                readonly property string cleanTag: text.trim().replace(/^#+/, "").trim()
                Layout.fillWidth: true
                maximumLength: 50
                placeholderText: "태그 이름"
                onAccepted: addBatch.click()
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: 8
                FlatButton {
                    text: "취소"
                    onClicked: tagPopup.close()
                }
                FlatButton {
                    id: addBatch
                    text: "추가"
                    tone: "primary"
                    enabled: batchTag.cleanTag.length > 0 && !!view.folder && !view.folder.tagsLocked
                    onClicked: {
                        if (view.folder && !view.folder.tagsLocked
                                && view.folder.addTagToSelection(batchTag.cleanTag) > 0) {
                            batchTag.text = ""
                            tagPopup.close()
                        }
                    }
                }
            }
        }
    }

    Popup {
        id: teachPopup
        objectName: "localFolderTeachPopup"
        parent: view.popupParent || Overlay.overlay
        modal: true
        focus: true
        anchors.centerIn: parent
        width: 320
        height: Math.min(380, parent ? Math.max(200, parent.height - 48) : 380)
        padding: 16
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
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
                text: "선택한 사진에 이 AI 태그가 맞다고 알려 줍니다."
                color: Theme.text
                wrapMode: Text.Wrap
                Layout.fillWidth: true
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
                Layout.fillWidth: true
                text: "AI 태그 모두 아님"
                enabled: !!view.folder && !view.folder.tagsLocked
                onClicked: {
                    view.teachCategory("", true)
                    teachPopup.close()
                }
            }
        }
    }

    // The photo large, with what can be done to it here.
    Popup {
        id: previewPopup
        objectName: "localFolderPreview"
        parent: view.popupParent || Overlay.overlay
        modal: true
        focus: true
        anchors.centerIn: parent
        width: parent ? Math.max(0, parent.width - 24) : 1060
        height: parent ? Math.max(0, parent.height - 24) : 780
        padding: 16
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property string notice: ""
        property string noticeKey: ""
        property string _openedStatus: ""
        readonly property string openedKey: view.folder ? view.folder.selectedKey : ""
        readonly property bool subOverlayOpen: deleteConfirmation.visible || alikeConfirmation.visible
            || teachMenu.visible
        function toggleFavorite() {
            if (!view.folder || !view.folder.selectedKey)
                return
            const note = view.folder.favoriteNotice(view.folder.selectedKey)
            view.keyNotice = note
            if (note && view.folder.status === note) {
                notice = note
                noticeKey = openedKey
            } else {
                notice = ""
                noticeKey = ""
            }
        }
        onOpenedKeyChanged: {
            notice = ""
            noticeKey = ""
            previewTagField.text = ""
        }
        onAboutToShow: {
            notice = ""
            noticeKey = ""
            _openedStatus = view.folder ? (view.folder.status || "") : ""
            previewTagField.text = ""
        }
        onClosed: {
            previewTagField.text = ""
            localPreviewMedia.release()
        }
        background: Card {
            topColor: Theme.dialogTop
            bottomColor: Theme.dialog
            border.color: Theme.lineStrong
            glow: 0.12
        }
        onOpened: previewBody.forceActiveFocus()
        Shortcut {
            sequence: "Left"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && !!view.folder && view.folder.canPrevious
            onActivated: view.folder.previousItem()
        }
        Shortcut {
            sequence: "Right"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && !!view.folder && view.folder.canNext
            onActivated: view.folder.nextItem()
        }
        Shortcut {
            sequence: "F"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && !!view.folder && !!view.folder.selectedKey
            onActivated: previewPopup.toggleFavorite()
        }
        Shortcut {
            sequence: "+"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.canZoomIn
            onActivated: localPreviewMedia.zoomIn()
        }
        Shortcut {
            sequence: "="
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.canZoomIn
            onActivated: localPreviewMedia.zoomIn()
        }
        Shortcut {
            sequence: "-"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.canZoomOut
            onActivated: localPreviewMedia.zoomOut()
        }
        Shortcut {
            sequence: "0"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.ready
            onActivated: localPreviewMedia.fit()
        }
        Shortcut {
            sequence: "1"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.canActualSize
            onActivated: localPreviewMedia.actualSize()
        }
        Shortcut {
            sequence: "m"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.mediaType === "video" && localPreviewMedia.videoSource && localPreviewMedia.ready
            onActivated: localPreviewMedia.muted = !localPreviewMedia.muted
        }
        Shortcut {
            sequence: "Shift+M"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && localPreviewMedia.mediaType === "video" && localPreviewMedia.videoSource && localPreviewMedia.ready
            onActivated: localPreviewMedia.muted = !localPreviewMedia.muted
        }
        Shortcut {
            sequence: "Delete"
            enabled: previewPopup.visible && !previewPopup.subOverlayOpen && !previewTagField.activeFocus
                && !!view.folder && !!view.folder.selectedKey && !!view.manager
            onActivated: {
                if (view.lockReason.length > 0) {
                    previewPopup.notice = view.lockReason
                    previewPopup.noticeKey = previewPopup.openedKey
                    view.keyNotice = view.lockReason
                    return
                }
                if (view.manager.mutating || view.folder.selectedMissing)
                    return
                if (view.folder.selectedAlike)
                    view.manager.prepareAlikeDelete(view.folder.selectedKey)
                else
                    view.manager.prepareDelete([view.folder.selectedKey])
            }
        }
        ColumnLayout {
            id: previewBody
            anchors.fill: parent
            spacing: Theme.gapSmall
            focus: true
            Flow {
                Layout.fillWidth: true
                spacing: 8
                FlatButton {
                    objectName: "localFolderPreviewPrevious"
                    text: "‹ 이전"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && view.folder.canPrevious
                    tip: "이전 사진 (←)"
                    onClicked: view.folder.previousItem()
                }
                FlatButton {
                    objectName: "localFolderPreviewNext"
                    text: "다음 ›"
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && view.folder.canNext
                    tip: "다음 사진 (→)"
                    onClicked: view.folder.nextItem()
                }
                PreviewZoomControls { preview: localPreviewMedia }
            }
            MediaPreview {
                id: localPreviewMedia
                objectName: "localFolderPreviewImage"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 40
                sourceUrl: previewPopup.visible && view.folder ? view.folder.selectedPreviewUrl : ""
                mediaType: view.folder ? view.folder.selectedMediaType : "photo"
                sourcePixelWidth: view.folder ? view.folder.selectedPixelWidth : 0
                sourcePixelHeight: view.folder ? view.folder.selectedPixelHeight : 0
                sourceRawWidth: view.folder ? view.folder.selectedRawWidth : 0
                sourceRawHeight: view.folder ? view.folder.selectedRawHeight : 0
                missingNote: view.folder ? view.folder.selectedMissingNote : ""
                externalBusy: view.folder ? view.folder.selectionBusy : false
                playing: previewPopup.visible && view.windowShown
            }
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
                Layout.fillWidth: true
                spacing: Theme.gapSmall
                Repeater {
                    model: view.folder ? view.folder.selectedTags : []
                    delegate: Rectangle {
                        id: chip
                        objectName: "localFolderTagChip"
                        required property var modelData
                        readonly property string tagName: modelData.tag || ""
                        readonly property bool isConfirmed: modelData.source === "confirmed"
                        readonly property bool isAi: modelData.source === "ai" || isConfirmed
                        radius: Theme.radiusSmall
                        border.width: 1
                        border.color: isAi ? Theme.aiTagLine : Theme.userTagLine
                        color: chipNameMouse.containsMouse ? (isAi ? Theme.aiTagHover : Theme.userTagHover)
                            : (isAi ? Theme.aiTagFill : Theme.userTagFill)
                        implicitWidth: chipRow.implicitWidth + 14
                        implicitHeight: Theme.compactHeight
                        RowLayout {
                            id: chipRow
                            anchors.centerIn: parent
                            spacing: 4
                            Label {
                                text: (chip.isAi ? "AI · #" : "#") + chip.tagName + (chip.isConfirmed ? " ✓" : "")
                                font.pixelSize: Theme.fontSmall
                                color: chip.isAi ? Theme.aiTagText : Theme.userTagText
                                ToolTip.visible: chipNameMouse.containsMouse
                                ToolTip.text: chip.isConfirmed ? "맞다고 알려 준 AI 태그 · 모아보기"
                                    : (chip.isAi ? "AI가 붙인 태그 · 모아보기" : "이 태그만 보기")
                                MouseArea {
                                    id: chipNameMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        if (!view.folder)
                                            return
                                        if (chip.isAi)
                                            view.folder.setAiFilter(chip.tagName, "")
                                        else
                                            view.folder.setUserTag(chip.tagName)
                                        previewPopup.close()
                                    }
                                }
                            }
                            Label {
                                objectName: "localFolderTagConfirm"
                                visible: chip.modelData.source === "ai"
                                text: "✓"
                                font.pixelSize: Theme.fontSmall
                                font.bold: true
                                leftPadding: 3
                                rightPadding: 1
                                color: confirmArea.containsMouse ? Theme.good : Theme.textMuted
                                ToolTip.visible: confirmArea.containsMouse
                                ToolTip.text: "맞아요 · AI 학습에 반영"
                                MouseArea {
                                    id: confirmArea
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    enabled: !!view.folder && !view.folder.tagsLocked
                                    onClicked: {
                                        if (view.folder && view.folder.teachTag(view.folder.selectedKey, chip.tagName, true))
                                            previewPopup.notice = "#" + chip.tagName + " 분류를 배우는 중…"
                                    }
                                }
                            }
                            Label {
                                objectName: "localFolderTagRemove"
                                text: "×"
                                font.pixelSize: Theme.fontSmall
                                font.bold: true
                                leftPadding: 3
                                rightPadding: 3
                                color: removeArea.containsMouse ? Theme.error : Theme.textMuted
                                ToolTip.visible: removeArea.containsMouse
                                ToolTip.text: chip.isAi ? "이 태그 아님 · AI 학습에 반영" : "태그 삭제"
                                MouseArea {
                                    id: removeArea
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    enabled: !!view.folder && !view.folder.tagsLocked
                                    onClicked: if (view.folder) view.folder.removeSelectedTag(chip.tagName)
                                }
                            }
                        }
                    }
                }
                FlatField {
                    id: previewTagField
                    objectName: "localFolderPreviewTagField"
                    readonly property string cleanTag: text.trim().replace(/^#+/, "").trim()
                    implicitHeight: Theme.compactHeight
                    width: 132
                    maximumLength: 50
                    placeholderText: "새 태그"
                    font.pixelSize: Theme.fontSmall
                    enabled: !!view.folder && !view.folder.tagsLocked
                    Keys.onEscapePressed: event => {
                        text = ""
                        previewBody.forceActiveFocus()
                        event.accepted = true
                    }
                    onAccepted: {
                        if (cleanTag.length > 0 && view.folder && !view.folder.tagsLocked
                                && view.folder.addSelectedTag(cleanTag))
                            text = ""
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
                        if (view.folder && !view.folder.tagsLocked
                                && view.folder.addSelectedTag(previewTagField.cleanTag))
                            previewTagField.text = ""
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
                    tip: "AI 태그 지정"
                    onClicked: {
                        if (!view.folder)
                            return
                        teachMenu.targetKey = view.folder.selectedKey
                        teachMenu.popup(previewAiButton, 0, previewAiButton.height + 4)
                    }
                }
            }
            Label {
                objectName: "localFolderPreviewNotice"
                Layout.fillWidth: true
                visible: previewPopup.notice.length > 0
                text: previewPopup.notice
                color: Theme.notice
                font.pixelSize: Theme.fontSmall
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
            Flow {
                Layout.fillWidth: true
                spacing: Theme.gapSmall
                FlatButton {
                    objectName: "localFolderPreviewFavorite"
                    text: view.folder && view.folder.selectedIsFavorite ? "★ 즐겨찾기 해제" : "☆ 즐겨찾기"
                    tone: "chip"
                    compact: true
                    focusPolicy: Qt.TabFocus
                    chosen: !!view.folder && view.folder.selectedIsFavorite
                    tip: view.folder && view.folder.selectedIsFavorite ? "즐겨찾기 해제 (F)" : "즐겨찾기 추가 (F)"
                    onClicked: previewPopup.toggleFavorite()
                }
                FlatButton {
                    objectName: "localFolderPreviewReveal"
                    text: "폴더에서 보기"
                    compact: true
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && !view.folder.selectedMissing
                    onClicked: view.folder.revealSelected()
                }
                FlatButton {
                    objectName: "localFolderPreviewOpen"
                    text: "기본 앱으로 열기"
                    compact: true
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && !view.folder.selectedMissing
                    onClicked: view.folder.openSelected()
                }
                FlatButton {
                    objectName: "localFolderPreviewRecycle"
                    readonly property bool alike: !!view.folder && view.folder.selectedAlike
                    text: alike ? "남김과 비교 후 휴지통으로" : "휴지통으로"
                    tone: "danger"
                    compact: true
                    focusPolicy: Qt.TabFocus
                    enabled: !!view.folder && !!view.manager && !view.folder.selectedMissing
                        && view.lockReason.length === 0 && !view.manager.mutating
                    tip: view.lockReason.length > 0 ? view.lockReason
                        : (alike ? "크기·화질만 다른 ‘비슷함’ 사진입니다. 남길 사진과 나란히 비교한 뒤 이 사진만 휴지통으로 보냅니다 (Delete)" : "")
                    onClicked: {
                        if (view.folder.selectedAlike)
                            view.manager.prepareAlikeDelete(view.folder.selectedKey)
                        else
                            view.manager.prepareDelete([view.folder.selectedKey])
                    }
                }
                FlatButton {
                    text: "닫기"
                    compact: true
                    tone: "ghost"
                    focusPolicy: Qt.TabFocus
                    onClicked: previewPopup.close()
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
        function onFolderChanged() {
            if (searchField.text.length > 0) {
                searchDelay.stop()
                searchField.text = ""
            }
            view.seenFolderStatus = ""
        }
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
                if (status.length > 0 && status !== previewPopup._openedStatus) {
                    previewPopup._openedStatus = status
                    previewPopup.notice = status
                    previewPopup.noticeKey = previewPopup.openedKey
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
            if (!view.resumePreview || !view.manager || view.manager.mutating
                    || view.manager.deleteConfirmationPending)
                return
            view.resumePreview = false
            if (view.folder && view.opened && (!view.app || !view.app.closing) && !previewPopup.visible
                    && (view.folder.selectionBusy || view.folder.selectedKey.length > 0))
                previewPopup.open()
        }
    }
}
