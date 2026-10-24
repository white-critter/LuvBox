import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    property var accounts: null
    property var app: null
    property int methodIndex: 0
    readonly property var browserSetup: app ? app.browserSetup : null
    readonly property bool unavailable: !accounts || accounts.locked || (app && app.closing)
    readonly property bool setupUnavailable: unavailable || !browserSetup || browserSetup.busy || accounts.busy
    title: "X 로그인 연결"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(560, parent ? Math.max(240, parent.width - 32) : 560)
    implicitHeight: contentColumn.implicitHeight + 128
    height: Math.min(implicitHeight, parent ? Math.max(180, parent.height - 32) : 720)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 24
    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }
    header: Label {
        text: dialog.title
        leftPadding: 24
        rightPadding: 24
        topPadding: 20
        bottomPadding: 4
        font.pixelSize: 18
        background: null
    }

    function submitToken() {
        if (unavailable || accounts.busy || !tokenField.text.trim())
            return
        var value = tokenField.text
        if (accounts.connectToken(value))
            tokenField.clear()
        value = ""
    }

    onOpened: {
        tokenField.clear()
        setupToggle.checked = !accounts || !accounts.connected
    }
    onClosed: {
        tokenField.clear()
        if (accounts)
            accounts.cancelBrowser()
    }

    Connections {
        target: dialog.accounts
        function onConnectionCompleted() {
            if (dialog.visible)
                dialog.close()
        }
    }

    contentItem: ScrollView {
        id: scrollView
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > availableHeight ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff

        ColumnLayout {
            id: contentColumn
            width: scrollView.availableWidth
            spacing: 16

            RowLayout {
                Layout.fillWidth: true
                enabled: !dialog.unavailable && !dialog.accounts.busy
                spacing: 8
                FlatButton {
                    text: "브라우저 연결"
                    tone: "nav"
                    checkable: true
                    checked: dialog.methodIndex === 0
                    onClicked: dialog.methodIndex = 0
                }
                FlatButton {
                    text: "토큰 입력"
                    tone: "nav"
                    checkable: true
                    checked: dialog.methodIndex === 1
                    onClicked: dialog.methodIndex = 1
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                visible: dialog.methodIndex === 0
                spacing: 12

                ToolButton {
                    id: setupToggle
                    Layout.fillWidth: true
                    implicitHeight: 40
                    visible: !dialog.accounts || !dialog.accounts.browserConnecting
                    checkable: true
                    text: checked ? "처음 설치 안내 ▾" : "처음 설치 안내 ▸"
                    background: Rectangle {
                        radius: 10
                        color: setupToggle.hovered || setupToggle.checked ? "#242424" : "#1c1c1c"
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: setupToggle.checked && (!dialog.accounts || !dialog.accounts.browserConnecting)
                    spacing: 8
                    Label {
                        Layout.fillWidth: true
                        text: "처음 한 번 확장을 설치한 뒤, 같은 브라우저 프로필의 X 계정을 연결합니다."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    FlatButton {
                        text: "브라우저에 확장 불러오기"
                        enabled: !dialog.setupUnavailable
                        onClicked: dialog.browserSetup.installExtension()
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "1. 확장 관리 화면에서 개발자 모드를 켜세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    FlatButton {
                        text: "확장 관리 열기"
                        enabled: !dialog.setupUnavailable
                        onClicked: dialog.browserSetup.openManager()
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "2. ‘압축해제된 확장 로드’를 누르고 아래 폴더를 선택하세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: 8
                        FlatButton {
                            text: "폴더 경로 복사"
                            enabled: !dialog.setupUnavailable
                            onClicked: dialog.browserSetup.copyExtensionPath()
                        }
                        FlatButton {
                            text: "폴더 열기"
                            enabled: !dialog.setupUnavailable
                            onClicked: dialog.browserSetup.openExtensionFolder()
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "3. 확장을 설치한 프로필에서 X에 로그인하세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    FlatButton {
                        text: "X 열기"
                        enabled: !dialog.setupUnavailable
                        onClicked: dialog.browserSetup.openX()
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "Chrome·Edge용 안내입니다. Firefox 설치는 확장 폴더 상위의 README를 확인하세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        opacity: 0.72
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: dialog.browserSetup && (dialog.browserSetup.error.length > 0 || dialog.browserSetup.status.length > 0)
                        text: dialog.browserSetup ? (dialog.browserSetup.error || dialog.browserSetup.status) : ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        Accessible.role: Accessible.StaticText
                    }
                    MenuSeparator { Layout.fillWidth: true }
                }
                Label {
                    Layout.fillWidth: true
                    visible: !dialog.accounts || !dialog.accounts.browserConnecting
                    text: "설치와 로그인이 끝나면 연결을 시작하세요. 확장의 코드와 계정을 확인한 뒤 이 창에서 승인합니다."
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                FlatButton {
                    Layout.fillWidth: true
                    text: dialog.accounts && dialog.accounts.cancelling ? "연결을 취소하는 중…"
                          : dialog.accounts && dialog.accounts.busy ? "연결 기다리는 중…" : "브라우저에서 연결"
                    tone: "primary"
                    enabled: !dialog.unavailable && !dialog.accounts.busy && (!dialog.browserSetup || !dialog.browserSetup.busy)
                    onClicked: dialog.accounts.connectBrowser()
                }

                Frame {
                    Layout.fillWidth: true
                    visible: dialog.accounts && dialog.accounts.pairingCode.length > 0
                    background: Rectangle { color: "#1c1c1c"; radius: 12 }

                    ColumnLayout {
                        width: parent.width
                        spacing: 10
                        Label {
                            text: "확장에 표시된 코드와 같은지 확인하세요."
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                        }
                        Label {
                            text: dialog.accounts ? dialog.accounts.pairingCode : ""
                            font.pixelSize: 28
                            font.bold: true
                            font.letterSpacing: 3
                            textFormat: Text.PlainText
                            Accessible.name: "연결 코드 " + text
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: dialog.accounts && dialog.accounts.approvalPending
                            text: dialog.accounts && dialog.accounts.pendingIdentity.handle
                                  ? "@" + dialog.accounts.pendingIdentity.handle : ""
                            font.bold: true
                            font.pixelSize: 18
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: dialog.accounts && dialog.accounts.approvalPending
                            text: dialog.accounts ? (dialog.accounts.pendingIdentity.display_name || "") : ""
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                        }
                        FlatButton {
                            Layout.fillWidth: true
                            visible: dialog.accounts && dialog.accounts.approvalPending
                            enabled: !dialog.unavailable && !dialog.accounts.cancelling
                            text: "이 계정 연결 승인"
                            tone: "primary"
                            onClicked: dialog.accounts.approveBrowser()
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                visible: dialog.methodIndex === 1
                spacing: 12

                Label {
                    Layout.fillWidth: true
                    text: "X에 로그인한 Chrome에서 F12 → Application → Cookies → https://x.com을 엽니다. auth_token의 Value만 복사하세요."
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                FlatField {
                    id: tokenField
                    Layout.fillWidth: true
                    enabled: !dialog.unavailable && !dialog.accounts.busy
                    placeholderText: "auth_token 값"
                    echoMode: TextInput.Password
                    maximumLength: 4096
                    inputMethodHints: Qt.ImhHiddenText | Qt.ImhNoPredictiveText
                    selectByMouse: true
                    Accessible.name: "auth_token 값"
                    onAccepted: dialog.submitToken()
                }
                Label {
                    Layout.fillWidth: true
                    text: "이 Windows 계정에 암호화해 저장합니다. 값은 다른 사람에게 보내지 마세요."
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    opacity: 0.72
                }
                FlatButton {
                    Layout.fillWidth: true
                    text: "연결 저장"
                    tone: "primary"
                    enabled: !dialog.unavailable && !dialog.accounts.busy && tokenField.text.trim().length > 0
                    onClicked: dialog.submitToken()
                }
            }

            Label {
                Layout.fillWidth: true
                visible: dialog.accounts && (dialog.accounts.error.length > 0 || dialog.accounts.status.length > 0)
                text: dialog.accounts ? (dialog.accounts.error || dialog.accounts.status) : ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                Accessible.role: Accessible.StaticText
            }
        }
    }

    footer: DialogButtonBox {
        background: null
        FlatButton {
            text: "현재 계정 연결 해제"
            visible: dialog.accounts && dialog.accounts.connected
            enabled: !dialog.unavailable && !dialog.accounts.busy
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: dialog.accounts.disconnectAccount()
        }
        FlatButton {
            text: dialog.accounts && dialog.accounts.cancelling ? "취소하는 중…"
                  : dialog.accounts && dialog.accounts.browserConnecting ? "연결 취소" : "닫기"
            enabled: !dialog.accounts || !dialog.accounts.cancelling
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: {
            if (dialog.accounts && dialog.accounts.browserConnecting)
                dialog.accounts.cancelBrowser()
            else
                dialog.close()
        }
    }
}
