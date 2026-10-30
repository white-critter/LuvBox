import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "preferencesDialog"
    property var app: null
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
    title: "설정"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(520, parent ? Math.max(240, parent.width - 32) : 520)
    readonly property int windowLimit: parent ? Math.max(180, parent.height - 32) : 760
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    implicitHeight: contentColumn.implicitHeight + chromeHeight
    height: Math.min(implicitHeight, windowLimit)
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
            objectName: "preferencesError"
            width: parent.width
            visible: dialog.app && dialog.app.error.length > 0
            text: dialog.app ? dialog.app.error : ""
            leftPadding: 20
            rightPadding: 20
            topPadding: 6
            color: "#ff8791"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Accessible.role: Accessible.StaticText
        }
    }

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

    contentItem: FocusScrollView {
        id: scrollView
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff

        ColumnLayout {
            id: contentColumn
            width: scrollView.availableWidth
            height: implicitHeight
            spacing: 10

            ColumnLayout {
                Layout.fillWidth: true
                enabled: dialog.app && !dialog.app.busy
                spacing: 8

                Label {
                    text: "저장"
                    font.bold: true
                    font.pixelSize: 16
                    textFormat: Text.PlainText
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    FlatSwitch {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        text: "동영상 저장"
                        checked: dialog.pref("include_videos", true) === true
                        onToggled: dialog.app.setPreference("include_videos", checked)
                    }
                    FlatSwitch {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        text: "움직이는 사진 저장"
                        checked: dialog.pref("include_gifs", true) === true
                        onToggled: dialog.app.setPreference("include_gifs", checked)
                    }
                }
                Label { text: "폴더 구성"; textFormat: Text.PlainText }
                FlatCombo {
                    Layout.fillWidth: true
                    model: dialog.folderModes
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: dialog.optionIndex(dialog.folderModes, dialog.pref("folder_mode", "flat"))
                    Accessible.name: "폴더 구성"
                    onActivated: index => dialog.setOption("folder_mode", dialog.folderModes, index)
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    FlatSwitch {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        text: "연결 계정별로 폴더 나누기"
                        checked: dialog.pref("separate_accounts", false) === true
                        onToggled: dialog.app.setPreference("separate_accounts", checked)
                    }
                    FlatSwitch {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        text: "저장 전 사진 확인"
                        checked: dialog.pref("confirm_each", false) === true
                        onToggled: dialog.app.setPreference("confirm_each", checked)
                    }
                }

                Label {
                    Layout.topMargin: 6
                    text: "움직이는 사진"
                    font.bold: true
                    font.pixelSize: 16
                    textFormat: Text.PlainText
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        spacing: 6
                        Label { text: "저장 형식"; textFormat: Text.PlainText }
                        FlatCombo {
                            Layout.fillWidth: true
                            model: dialog.gifFormats
                            textRole: "label"
                            valueRole: "value"
                            currentIndex: dialog.optionIndex(dialog.gifFormats, dialog.pref("gif_format", "gif"))
                            Accessible.name: "움직이는 사진 형식"
                            onActivated: index => dialog.setOption("gif_format", dialog.gifFormats, index)
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        spacing: 6
                        Label {
                            text: "GIF 크기"
                            textFormat: Text.PlainText
                            enabled: dialog.pref("gif_format", "gif") === "gif"
                        }
                        FlatCombo {
                            Layout.fillWidth: true
                            enabled: dialog.pref("gif_format", "gif") === "gif"
                            model: dialog.gifSizes
                            textRole: "label"
                            valueRole: "value"
                            currentIndex: dialog.optionIndex(dialog.gifSizes, dialog.pref("gif_size", "original"))
                            Accessible.name: "GIF 크기"
                            onActivated: index => dialog.setOption("gif_size", dialog.gifSizes, index)
                        }
                    }
                }

                ToolButton {
                    id: advancedToggle
                    objectName: "advancedSettingsToggle"
                    Layout.topMargin: 6
                    Layout.fillWidth: true
                    implicitHeight: 36
                    text: checked ? "고급 ▾" : "고급 ▸"
                    checkable: true
                    Accessible.name: "고급 설정"
                    HoverHandler {
                        cursorShape: Qt.PointingHandCursor
                    }
                    contentItem: Text {
                        text: advancedToggle.text
                        font.pixelSize: 14
                        color: "#f4f1f1"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 10
                        border.width: advancedToggle.visualFocus ? 2 : 0
                        border.color: "#ff8791"
                        color: advancedToggle.hovered || advancedToggle.checked ? "#242424" : "#1c1c1c"
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: advancedToggle.checked
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            spacing: 6
                            Label { text: "목록 확인 범위"; textFormat: Text.PlainText }
                            FlatCombo {
                                Layout.fillWidth: true
                                model: dialog.scanModes
                                textRole: "label"
                                valueRole: "value"
                                currentIndex: dialog.optionIndex(dialog.scanModes, dialog.pref("scan_mode", "quick"))
                                Accessible.name: "목록 확인 범위"
                                onActivated: index => dialog.setOption("scan_mode", dialog.scanModes, index)
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            spacing: 6
                            Label { text: "동영상·GIF 크기 제한 (MB)"; textFormat: Text.PlainText }
                            FlatField {
                                id: videoLimit
                                objectName: "videoLimitField"
                                Layout.fillWidth: true
                                implicitHeight: 42
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
                    Label {
                        Layout.fillWidth: true
                        visible: dialog.pref("scan_mode", "quick") === "full"
                        text: "오래된 Like까지 확인하므로 시간이 더 걸립니다."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        opacity: 0.72
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            Layout.fillWidth: true
                            text: "0은 제한 없음"
                            textFormat: Text.PlainText
                            opacity: 0.72
                        }
                        FlatButton {
                            text: "기록 폴더 열기"
                            onClicked: dialog.app.openRecordsFolder()
                        }
                    }
                }
            }
            Label {
                Layout.fillWidth: true
                visible: dialog.app && dialog.app.busy
                text: "작업이 끝난 뒤 설정을 바꿀 수 있습니다."
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
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
