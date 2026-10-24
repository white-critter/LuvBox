import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "preferencesDialog"
    property var app: null
    property string scheduleAction: ""
    onOpened: if (app) app.schedule.refresh()
    onClosed: scheduleConfirmation.close()
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
    implicitHeight: contentColumn.implicitHeight + 128
    height: Math.min(implicitHeight, parent ? Math.max(180, parent.height - 32) : 760)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 24

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

    contentItem: ScrollView {
        id: scrollView
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > availableHeight ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff

        ColumnLayout {
            id: contentColumn
            width: scrollView.availableWidth
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                enabled: dialog.app && !dialog.app.busy
                spacing: 10

                Label {
                    text: "저장"
                    font.bold: true
                    font.pixelSize: 17
                    textFormat: Text.PlainText
                }
                Switch {
                    Layout.fillWidth: true
                    text: "동영상 저장"
                    checked: dialog.pref("include_videos", true) === true
                    onToggled: dialog.app.setPreference("include_videos", checked)
                }
                Switch {
                    Layout.fillWidth: true
                    text: "움직이는 사진 저장"
                    checked: dialog.pref("include_gifs", true) === true
                    onToggled: dialog.app.setPreference("include_gifs", checked)
                }
                Label { text: "폴더 구성"; textFormat: Text.PlainText }
                ComboBox {
                    Layout.fillWidth: true
                    model: dialog.folderModes
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: dialog.optionIndex(dialog.folderModes, dialog.pref("folder_mode", "flat"))
                    Accessible.name: "폴더 구성"
                    onActivated: index => dialog.setOption("folder_mode", dialog.folderModes, index)
                }
                Switch {
                    Layout.fillWidth: true
                    text: "연결 계정별로 폴더 나누기"
                    checked: dialog.pref("separate_accounts", false) === true
                    onToggled: dialog.app.setPreference("separate_accounts", checked)
                }
                Switch {
                    Layout.fillWidth: true
                    text: "저장 전 사진 확인"
                    checked: dialog.pref("confirm_each", false) === true
                    onToggled: dialog.app.setPreference("confirm_each", checked)
                }

                Label {
                    Layout.topMargin: 12
                    text: "움직이는 사진"
                    font.bold: true
                    font.pixelSize: 17
                    textFormat: Text.PlainText
                }
                ComboBox {
                    Layout.fillWidth: true
                    model: dialog.gifFormats
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: dialog.optionIndex(dialog.gifFormats, dialog.pref("gif_format", "gif"))
                    Accessible.name: "움직이는 사진 형식"
                    onActivated: index => dialog.setOption("gif_format", dialog.gifFormats, index)
                }
                ComboBox {
                    Layout.fillWidth: true
                    enabled: dialog.pref("gif_format", "gif") === "gif"
                    model: dialog.gifSizes
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: dialog.optionIndex(dialog.gifSizes, dialog.pref("gif_size", "original"))
                    Accessible.name: "GIF 크기"
                    onActivated: index => dialog.setOption("gif_size", dialog.gifSizes, index)
                }

                ToolButton {
                    id: advancedToggle
                    Layout.topMargin: 12
                    Layout.fillWidth: true
                    text: checked ? "고급 ▾" : "고급 ▸"
                    checkable: true
                    Accessible.name: "고급 설정"
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: advancedToggle.checked
                    spacing: 10

                    Label { text: "목록 확인 범위"; textFormat: Text.PlainText }
                    ComboBox {
                        Layout.fillWidth: true
                        model: dialog.scanModes
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: dialog.optionIndex(dialog.scanModes, dialog.pref("scan_mode", "quick"))
                        Accessible.name: "목록 확인 범위"
                        onActivated: index => dialog.setOption("scan_mode", dialog.scanModes, index)
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: dialog.pref("scan_mode", "quick") === "full"
                        text: "오래된 Like까지 확인하므로 시간이 더 걸립니다."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        opacity: 0.72
                    }
                    Label { text: "동영상·GIF 크기 제한 (MB)"; textFormat: Text.PlainText }
                    TextField {
                        Layout.fillWidth: true
                        text: String(dialog.pref("max_video_mb", 200))
                        validator: IntValidator { bottom: 0; top: 1000000 }
                        inputMethodHints: Qt.ImhDigitsOnly
                        selectByMouse: true
                        Accessible.name: "동영상 GIF 크기 제한 MB"
                        onEditingFinished: {
                            if (acceptableInput && text.trim().length > 0)
                                dialog.app.setPreference("max_video_mb", Number(text))
                        }
                    }
                    Label {
                        text: "0은 제한 없음"
                        textFormat: Text.PlainText
                        opacity: 0.72
                    }
                    Button {
                        text: "기록 폴더 열기"
                        onClicked: dialog.app.openRecordsFolder()
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: dialog.app && (!dialog.app.schedule.known || dialog.app.schedule.registered ||
                            dialog.app.schedule.error.length > 0 || dialog.app.schedule.status.length > 0)
                        spacing: 8
                        Label { text: "기존 자동 저장"; font.bold: true; textFormat: Text.PlainText }
                        Label {
                            Layout.fillWidth: true
                            text: dialog.app ? dialog.app.schedule.summary : ""
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: dialog.app && dialog.app.schedule.registered && !dialog.app.schedule.current
                            text: "이전 버전의 예약입니다. 이 버전으로 변경하거나 해제할 수 있습니다."
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Button {
                                text: "다시 확인"
                                enabled: dialog.app && !dialog.app.schedule.busy
                                onClicked: dialog.app.schedule.refresh()
                            }
                            Button {
                                text: "현재 버전으로 변경"
                                visible: dialog.app && dialog.app.schedule.registered && dialog.app.schedule.enabled && !dialog.app.schedule.current
                                enabled: dialog.app && !dialog.app.schedule.busy && !dialog.app.schedule.locked && !dialog.app.schedule.running
                                onClicked: {
                                    dialog.scheduleAction = "update"
                                    scheduleConfirmation.open()
                                }
                            }
                            Button {
                                text: "예약 해제"
                                visible: dialog.app && dialog.app.schedule.registered
                                enabled: dialog.app && !dialog.app.schedule.busy && !dialog.app.schedule.locked
                                onClicked: {
                                    dialog.scheduleAction = "remove"
                                    scheduleConfirmation.open()
                                }
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: dialog.app ? (dialog.app.schedule.error || dialog.app.schedule.status) : ""
                            visible: text.length > 0
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
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
        Button {
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }

    Dialog {
        id: scheduleConfirmation
        objectName: "scheduleConfirmation"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(440, parent ? parent.width - 32 : 440)
        implicitHeight: 220
        height: Math.min(implicitHeight, parent ? Math.max(180, parent.height - 32) : implicitHeight)
        title: dialog.scheduleAction === "remove" ? "자동 저장 예약을 해제할까요?" : "현재 버전으로 변경할까요?"
        closePolicy: Popup.CloseOnEscape
        onOpened: scheduleCancel.forceActiveFocus()
        contentItem: Label {
            text: dialog.scheduleAction === "remove"
                ? "다음 자동 저장부터 실행하지 않습니다. 이미 시작한 저장과 받은 파일은 그대로 둡니다."
                : "기존 예약 시간을 유지하고 실행 프로그램만 현재 버전으로 바꿉니다."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
        onAccepted: {
            if (dialog.scheduleAction === "remove")
                dialog.app.schedule.remove()
            else
                dialog.app.schedule.useCurrentVersion()
        }
        footer: DialogButtonBox {
            Button {
                text: dialog.scheduleAction === "remove" ? "예약 해제" : "현재 버전으로 변경"
                highlighted: true
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            Button {
                id: scheduleCancel
                text: "취소"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onAccepted: scheduleConfirmation.accept()
            onRejected: scheduleConfirmation.reject()
        }
    }
}
