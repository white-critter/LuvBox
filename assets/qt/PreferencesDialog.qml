import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
    id: dialog
    objectName: "preferencesDialog"
    property var app: null
    // Absent in previews of this dialog that carry no update controller.
    readonly property var updates: app && app.updates ? app.updates : null
    readonly property var folderModes: [
        {label: "한 폴더에 저장", value: "flat"},
        {label: "작성자별 폴더", value: "author"},
        {label: "저장일별 폴더", value: "date"}
    ]
    readonly property var gifFormats: [
        {label: "GIF", value: "gif"}, {label: "원본 MP4", value: "mp4"}
    ]
    readonly property var gifSizes: [
        {label: "원본 크기", value: "original"},
        {label: "최대 720px", value: "720"}, {label: "최대 480px", value: "480"}
    ]
    readonly property var scanModes: [
        {label: "새 Like만 확인", value: "quick"}, {label: "전체 목록 확인", value: "full"}
    ]
    // In a wide window the settings stand in two columns (saving on the left, updates and
    // the advanced part on the right) and fit without scrolling; a narrow window stacks them.
    readonly property bool wide: !!parent && parent.width - windowMargin >= 820
    readonly property real columnWidth: wide ? (availableWidth - 2 * Theme.gapSection - 1) / 2 : availableWidth
    // The width every list and field on the right side of a row shares.
    readonly property int controlWidth: Math.min(220, Math.max(140, Math.round(columnWidth * 0.5)))
    title: "설정"
    signal scheduleRequested()
    signal runHistoryRequested()
    signal unsavedRequested()
    preferredWidth: wide ? 880 : 540
    readonly property int windowLimit: parent ? Math.max(180, parent.height - 32) : 760
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    implicitHeight: contentColumn.implicitHeight + chromeHeight
    height: Math.min(implicitHeight, windowLimit)
    banner: [
        Label {
            objectName: "preferencesBusyNote"
            width: parent.width
            visible: dialog.app && dialog.app.busy
            text: "작업이 끝난 뒤 설정을 바꿀 수 있습니다. 기록은 지금도 볼 수 있습니다."
            topPadding: 2
            color: Theme.notice
            font.pixelSize: Theme.fontSmall
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        },
        Label {
            objectName: "preferencesError"
            width: parent.width
            visible: dialog.app && dialog.app.error.length > 0
            text: dialog.app ? dialog.app.error : ""
            topPadding: 2
            color: Theme.error
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Accessible.role: Accessible.StaticText
        }
    ]

    function pref(key, fallback) {
        return app && app.preferences[key] !== undefined ? app.preferences[key] : fallback
    }

    function optionIndex(options, value) {
        for (var index = 0; index < options.length; index++) {
            if (options[index].value === value)
                return index
        }
        return -1
    }

    function setOption(key, options, index) {
        if (app && index >= 0 && index < options.length)
            app.setPreference(key, options[index].value)
    }

    component SectionTitle: Label {
        Layout.fillWidth: true
        Layout.topMargin: Theme.gapSmall
        font.bold: true
        font.pixelSize: Theme.fontSection
        textFormat: Text.PlainText
    }
    component Divider: Rectangle {
        Layout.fillWidth: true
        Layout.topMargin: Theme.gapSmall
        implicitHeight: 1
        color: Theme.line
    }

    contentItem: FocusScrollView {
        id: scrollView
        objectName: "preferencesScroll"
        clip: true
        // The bar gets its own lane, so it never covers a control's right edge.
        rightPadding: ScrollBar.vertical.policy === ScrollBar.AlwaysOn ? 14 : 0
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff

        GridLayout {
            id: contentColumn
            width: scrollView.availableWidth
            height: implicitHeight
            columns: dialog.wide ? 3 : 1
            columnSpacing: Theme.gapSection
            rowSpacing: Theme.gapTiny

            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                enabled: dialog.app && !dialog.app.busy
                spacing: Theme.gapTiny

                SectionTitle { text: "저장"; Layout.topMargin: 0 }
                FlatSwitch {
                    Layout.fillWidth: true
                    trailing: true
                    text: "동영상 저장"
                    checked: dialog.pref("include_videos", true) === true
                    onToggled: {
                        dialog.app.setPreference("include_videos", checked)
                        checked = Qt.binding(function() { return dialog.pref("include_videos", true) === true })
                    }
                }
                FlatSwitch {
                    Layout.fillWidth: true
                    trailing: true
                    text: "움직이는 사진 저장"
                    checked: dialog.pref("include_gifs", true) === true
                    onToggled: {
                        dialog.app.setPreference("include_gifs", checked)
                        checked = Qt.binding(function() { return dialog.pref("include_gifs", true) === true })
                    }
                }
                FlatSwitch {
                    Layout.fillWidth: true
                    trailing: true
                    text: "저장 전 사진 확인"
                    checked: dialog.pref("confirm_each", false) === true
                    onToggled: {
                        dialog.app.setPreference("confirm_each", checked)
                        checked = Qt.binding(function() { return dialog.pref("confirm_each", false) === true })
                    }
                }
                FlatSwitch {
                    objectName: "autoClassifySwitch"
                    Layout.fillWidth: true
                    trailing: true
                    text: "저장한 사진 자동 AI 분류"
                    checked: dialog.pref("auto_classify", true) === true
                    onToggled: {
                        dialog.app.setPreference("auto_classify", checked)
                        checked = Qt.binding(function() { return dialog.pref("auto_classify", true) === true })
                    }
                }
                Label {
                    objectName: "autoClassifyHint"
                    Layout.fillWidth: true
                    text: "‘AI 분류’를 한 번 실행한 뒤부터, 저장이 끝나면 새 사진을 알아서 분류해요. 자동 저장도 같아요."
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontCaption
                    color: Theme.textFaint
                }
                SettingRow {
                    label: "폴더 구성"
                    Layout.topMargin: 2
                    FlatCombo {
                        Layout.preferredWidth: dialog.controlWidth
                        model: dialog.folderModes
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: dialog.optionIndex(dialog.folderModes, dialog.pref("folder_mode", "flat"))
                        Accessible.name: "폴더 구성"
                        onActivated: index => {
                            dialog.setOption("folder_mode", dialog.folderModes, index)
                            currentIndex = Qt.binding(function() {
                                return dialog.optionIndex(dialog.folderModes, dialog.pref("folder_mode", "flat"))
                            })
                        }
                    }
                }
                FlatSwitch {
                    Layout.fillWidth: true
                    trailing: true
                    text: "연결 계정별로 폴더 나누기"
                    checked: dialog.pref("separate_accounts", false) === true
                    onToggled: {
                        dialog.app.setPreference("separate_accounts", checked)
                        checked = Qt.binding(function() { return dialog.pref("separate_accounts", false) === true })
                    }
                }

                Divider {}
                SectionTitle { text: "움직이는 사진" }
                SettingRow {
                    label: "저장 형식"
                    Layout.topMargin: 2
                    FlatCombo {
                        Layout.preferredWidth: dialog.controlWidth
                        model: dialog.gifFormats
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: dialog.optionIndex(dialog.gifFormats, dialog.pref("gif_format", "gif"))
                        Accessible.name: "움직이는 사진 형식"
                        onActivated: index => {
                            dialog.setOption("gif_format", dialog.gifFormats, index)
                            currentIndex = Qt.binding(function() {
                                return dialog.optionIndex(dialog.gifFormats, dialog.pref("gif_format", "gif"))
                            })
                        }
                    }
                }
                SettingRow {
                    label: "GIF 크기"
                    Layout.topMargin: Theme.gapTiny
                    enabled: dialog.pref("gif_format", "gif") === "gif"
                    FlatCombo {
                        Layout.preferredWidth: dialog.controlWidth
                        model: dialog.gifSizes
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: dialog.optionIndex(dialog.gifSizes, dialog.pref("gif_size", "original"))
                        Accessible.name: "GIF 크기"
                        onActivated: index => {
                            dialog.setOption("gif_size", dialog.gifSizes, index)
                            currentIndex = Qt.binding(function() {
                                return dialog.optionIndex(dialog.gifSizes, dialog.pref("gif_size", "original"))
                            })
                        }
                    }
                }
            }
            // The line between the two columns.
            Rectangle {
                visible: dialog.wide
                Layout.fillHeight: true
                implicitWidth: 1
                color: Theme.line
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                spacing: Theme.gapTiny
                ColumnLayout {
                    objectName: "preferencesUpdates"
                    Layout.fillWidth: true
                    spacing: Theme.gapTiny
                    // Not locked while a save runs: checking for a release changes nothing a save reads.
                    visible: !!dialog.updates
                    enabled: !!dialog.updates && !dialog.app.closing
                    Divider { visible: !dialog.wide }
                    SectionTitle { text: "업데이트"; Layout.topMargin: dialog.wide ? 0 : Theme.gapSmall }
                    FlatSwitch {
                        objectName: "updateAutoCheckSwitch"
                        Layout.fillWidth: true
                        trailing: true
                        text: "시작할 때 새 버전 확인 (하루 한 번)"
                        checked: !!dialog.updates && dialog.updates.autoCheck
                        onToggled: {
                            dialog.updates.setAutoCheck(checked)
                            checked = Qt.binding(function() { return !!dialog.updates && dialog.updates.autoCheck })
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.gap
                        Label {
                            objectName: "updateStatusLabel"
                            Layout.fillWidth: true
                            text: dialog.updates
                                ? "지금 버전 " + dialog.updates.currentVersion + " · " + dialog.updates.status : ""
                            color: dialog.updates && dialog.updates.state === "failed" ? Theme.error : Theme.textMuted
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                        }
                        FlatButton {
                            objectName: "updateCheckButton"
                            text: dialog.updates && dialog.updates.state === "checking" ? "확인하는 중" : "지금 확인"
                            enabled: !!dialog.updates && !dialog.updates.busy && dialog.updates.state !== "ready"
                            Accessible.name: "새 버전 지금 확인"
                            onClicked: dialog.updates.checkNow()
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.topMargin: 2
                        text: "공개 릴리스 정보만 읽고, 이 PC나 계정에 관한 것은 보내지 않아요. 받기는 알림에서 직접 누를 때만 시작해요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textFaint
                    }
                }
                // Stacked, it follows the block above; side by side, only when updates sit above it.
                Divider { visible: !dialog.wide || !!dialog.updates }
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: !dialog.wide || !!dialog.updates ? Theme.gapSmall : 0
                    spacing: Theme.gapSmall
                    ToolButton {
                        id: advancedToggle
                        objectName: "advancedSettingsToggle"
                        Layout.fillWidth: true
                        implicitHeight: Theme.controlHeight
                        leftPadding: 12
                        rightPadding: 12
                        text: "고급"
                        checkable: true
                        Accessible.name: "고급 설정"
                        HoverHandler {
                            cursorShape: Qt.PointingHandCursor
                        }
                        contentItem: Item {
                            Text {
                                anchors.left: parent.left
                                anchors.verticalCenter: parent.verticalCenter
                                text: advancedToggle.text
                                font.pixelSize: Theme.fontSection
                                font.bold: true
                                color: Theme.text
                            }
                            Text {
                                anchors.right: caret.left
                                anchors.rightMargin: 8
                                anchors.verticalCenter: parent.verticalCenter
                                text: advancedToggle.checked ? "접기" : "펼치기"
                                font.pixelSize: Theme.fontSmall
                                color: Theme.textMuted
                            }
                            Canvas {
                                id: caret
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                width: 10
                                height: 6
                                rotation: advancedToggle.checked ? 180 : 0
                                onPaint: {
                                    const ctx = getContext("2d")
                                    ctx.reset()
                                    ctx.moveTo(0, 1)
                                    ctx.lineTo(width, 1)
                                    ctx.lineTo(width / 2, height)
                                    ctx.closePath()
                                    ctx.fillStyle = Theme.textMuted
                                    ctx.fill()
                                }
                            }
                        }
                        background: Rectangle {
                            radius: Theme.radiusControl
                            border.width: advancedToggle.visualFocus ? 2 : 0
                            border.color: Theme.focus
                            color: advancedToggle.hovered || advancedToggle.down ? Theme.raised : Theme.field
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: advancedToggle.checked
                        spacing: Theme.gapSmall

                        ColumnLayout {
                            Layout.fillWidth: true
                            enabled: dialog.app && !dialog.app.busy
                            spacing: Theme.gapSmall
                            SettingRow {
                                label: "목록 확인 범위"
                                note: dialog.pref("scan_mode", "quick") === "full"
                                    ? "오래된 Like까지 확인하므로 시간이 더 걸립니다." : ""
                                FlatCombo {
                                    Layout.preferredWidth: dialog.controlWidth
                                    model: dialog.scanModes
                                    textRole: "label"
                                    valueRole: "value"
                                    currentIndex: dialog.optionIndex(dialog.scanModes, dialog.pref("scan_mode", "quick"))
                                    Accessible.name: "목록 확인 범위"
                                    onActivated: index => {
                                        dialog.setOption("scan_mode", dialog.scanModes, index)
                                        currentIndex = Qt.binding(function() {
                                            return dialog.optionIndex(dialog.scanModes, dialog.pref("scan_mode", "quick"))
                                        })
                                    }
                                }
                            }
                            SettingRow {
                                label: "동영상·GIF 크기 제한 (MB)"
                                note: "0은 제한 없음. 넘는 파일은 ‘저장 못 한 미디어’에 남깁니다."
                                FlatField {
                                    id: videoLimit
                                    objectName: "videoLimitField"
                                    Layout.preferredWidth: dialog.controlWidth
                                    text: String(dialog.pref("max_video_mb", 200))
                                    validator: IntValidator { bottom: 0; top: 1000000 }
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    selectByMouse: true
                                    Accessible.name: "동영상 GIF 크기 제한 MB"
                                    onEditingFinished: {
                                        if (acceptableInput && text.trim().length > 0)
                                            dialog.app.setPreference("max_video_mb", Number(text))
                                    }
                                    onActiveFocusChanged: {
                                        if (!activeFocus && (!acceptableInput || text.trim().length === 0))
                                            text = Qt.binding(function() { return String(dialog.pref("max_video_mb", 200)) })
                                    }
                                }
                            }
                        }
                        SettingRow {
                            label: "자동 저장"
                            note: dialog.app ? dialog.app.schedule.summary : ""
                            noteName: "preferencesScheduleSummary"
                            FlatButton {
                                objectName: "preferencesScheduleButton"
                                text: "자동 저장 설정"
                                onClicked: dialog.scheduleRequested()
                            }
                        }
                        Label {
                            Layout.topMargin: Theme.gapTiny
                            text: "기록"
                            textFormat: Text.PlainText
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: Theme.gapSmall
                            FlatButton {
                                objectName: "preferencesRunHistoryButton"
                                text: "실행 기록"
                                onClicked: dialog.runHistoryRequested()
                            }
                            FlatButton {
                                objectName: "preferencesUnsavedButton"
                                text: "저장 못 한 미디어" + (dialog.app && dialog.app.records.unsavedCount > 0
                                    ? " (" + dialog.app.records.unsavedCount.toLocaleString() + ")" : "")
                                onClicked: dialog.unsavedRequested()
                            }
                            FlatButton {
                                text: "기록 폴더 열기"
                                onClicked: dialog.app.openRecordsFolder()
                            }
                        }
                    }
                }
            }
        }
    }

    footer: DialogButtons {
        FlatButton {
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }
}
