import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    property var accounts: null
    property var app: null
    property int methodIndex: 0
    property bool lastSetupNeeded: false
    property bool lastExtensionReady: false
    readonly property var browserSetup: app ? app.browserSetup : null
    readonly property bool unavailable: !accounts || accounts.locked || (app && app.closing)
    readonly property bool setupUnavailable: unavailable || !browserSetup || browserSetup.busy
        || (accounts.busy && !accounts.browserConnecting)
    title: "X 로그인 연결"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(560, parent ? Math.max(240, parent.width - 32) : 560)
    readonly property int windowLimit: parent ? Math.max(180, parent.height - 32) : 720
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
    header: Label {
        text: dialog.title
        leftPadding: 20
        rightPadding: 20
        topPadding: 16
        bottomPadding: 2
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
        methodIndex = 0
        lastSetupNeeded = !!(accounts && accounts.setupNeeded)
        lastExtensionReady = !!(accounts && accounts.extensionReady)
        setupToggle.checked = lastSetupNeeded
        if (browserSetup)
            browserSetup.prepareExtension()
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
        function onChanged() {
            if (!dialog.accounts || !dialog.visible)
                return
            if (dialog.accounts.setupNeeded && !dialog.lastSetupNeeded)
                setupToggle.checked = true
            else if (dialog.accounts.extensionReady && !dialog.lastExtensionReady)
                setupToggle.checked = false
            dialog.lastSetupNeeded = dialog.accounts.setupNeeded
            dialog.lastExtensionReady = dialog.accounts.extensionReady
        }
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

            Label {
                Layout.fillWidth: true
                text: dialog.methodIndex === 0
                    ? "사용 중인 브라우저의 X 계정을 연결합니다. 이미 연결했다면 다시 설치할 필요가 없습니다."
                    : "브라우저에서 확인한 토큰을 직접 입력해 이 PC에 보관합니다."
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }

            ColumnLayout {
                Layout.fillWidth: true
                visible: dialog.methodIndex === 0
                spacing: 8

                Label { text: "연결할 브라우저" }
                FlatCombo {
                    objectName: "connectionBrowser"
                    Layout.fillWidth: true
                    model: dialog.browserSetup ? dialog.browserSetup.browsers : []
                    textRole: "label"
                    valueRole: "id"
                    currentIndex: {
                        if (!dialog.browserSetup)
                            return -1
                        var choices = dialog.browserSetup.browsers
                        for (var i = 0; i < choices.length; ++i)
                            if (choices[i].id === dialog.browserSetup.selectedBrowser)
                                return i
                        return -1
                    }
                    enabled: !dialog.setupUnavailable && !dialog.accounts.busy
                    displayText: currentIndex >= 0 ? currentText
                        : !dialog.browserSetup || dialog.browserSetup.busy ? "브라우저를 확인하는 중…" : "지원하는 브라우저 없음"
                    Accessible.name: "X 계정을 연결할 브라우저"
                    onActivated: dialog.browserSetup.setBrowser(currentValue)
                }
                Label {
                    objectName: "noBrowserMessage"
                    Layout.fillWidth: true
                    visible: dialog.browserSetup && !dialog.browserSetup.busy
                        && !dialog.browserSetup.selectedBrowser.length
                    text: dialog.browserSetup && dialog.browserSetup.error
                        ? dialog.browserSetup.error
                        : "지원하는 브라우저를 찾지 못했습니다. Chrome·Edge·Firefox를 설치한 뒤 앱을 다시 열어 주세요."
                    color: "#ff8791"
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
                Label {
                    Layout.fillWidth: true
                    text: "X에 로그인하고 확장을 설치한 일반 프로필을 사용하세요. 여러 프로필을 쓴다면 열린 창의 프로필을 확인해 주세요."
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                    opacity: 0.72
                }
                FlatButton {
                    objectName: "connectBrowserButton"
                    Layout.fillWidth: true
                    text: dialog.accounts && dialog.accounts.cancelling ? "연결을 취소하는 중…"
                          : dialog.accounts && dialog.accounts.busy ? "연결 진행 중…"
                          : dialog.accounts && dialog.accounts.connected ? "다시 연결" : "X 계정 연결"
                    tone: "primary"
                    enabled: !dialog.setupUnavailable && !dialog.accounts.busy
                        && dialog.browserSetup.selectedBrowser.length > 0
                    onClicked: dialog.accounts.connectBrowser(dialog.browserSetup.selectedBrowser)
                }
                Frame {
                    Layout.fillWidth: true
                    visible: dialog.accounts && dialog.accounts.browserConnecting
                    background: Rectangle { color: "#1c1c1c"; radius: 12 }
                    ColumnLayout {
                        width: parent.width
                        spacing: 8
                        Label {
                            Layout.fillWidth: true
                            text: dialog.accounts && dialog.accounts.approvalPending ? "3 / 3 · 앱에서 최종 승인"
                                : dialog.accounts && dialog.accounts.extensionReady ? "2 / 3 · 브라우저에서 계정 확인"
                                : "1 / 3 · 연결 확장 확인"
                            font.bold: true
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            text: dialog.accounts ? dialog.accounts.status : ""
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                        }
                        Label {
                            visible: dialog.accounts && dialog.accounts.secondsRemaining > 0
                            text: dialog.accounts ? "남은 시간 " + dialog.accounts.secondsRemaining + "초" : ""
                            opacity: 0.72
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: dialog.accounts && dialog.accounts.pairingCode.length > 0
                            text: "브라우저 확장의 코드와 비교하세요."
                            wrapMode: Text.Wrap
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
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                        }
                        FlatButton {
                            objectName: "approveConnectionButton"
                            Layout.fillWidth: true
                            visible: dialog.accounts && dialog.accounts.approvalPending
                            enabled: !dialog.unavailable && !dialog.accounts.cancelling
                            text: "이 계정 연결 승인"
                            tone: "primary"
                            onClicked: dialog.accounts.approveBrowser()
                        }
                        FlatButton {
                            visible: dialog.accounts && dialog.accounts.connectionStage === "login_required"
                            text: "선택한 브라우저에서 X 열기"
                            enabled: !dialog.setupUnavailable
                            onClicked: dialog.browserSetup.openX()
                        }
                    }
                }
                ToolButton {
                    id: setupToggle
                    Layout.fillWidth: true
                    implicitHeight: 36
                    checkable: true
                    text: checked ? "확장 설치·연결 도움말 ▾" : "처음 연결하거나 확장 응답이 없나요? ▸"
                    HoverHandler {
                        cursorShape: Qt.PointingHandCursor
                    }
                    contentItem: Text {
                        text: setupToggle.text
                        font.pixelSize: 14
                        color: "#f4f1f1"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 10
                        border.width: setupToggle.visualFocus ? 2 : 0
                        border.color: "#ff8791"
                        color: setupToggle.hovered || setupToggle.checked ? "#242424" : "#1c1c1c"
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: setupToggle.checked
                    spacing: 8
                    Label {
                        Layout.fillWidth: true
                        text: "선택한 브라우저의 같은 프로필에 확장을 한 번 설치합니다. 설치 후에는 이 안내를 건너뛰어도 됩니다."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        text: dialog.browserSetup && dialog.browserSetup.extensionKind === "firefox"
                            ? "1. 부가 기능 디버깅 화면에서 ‘임시 부가 기능 로드’를 누르세요. Firefox를 다시 시작하면 확장을 다시 불러와야 합니다."
                            : "1. 확장 관리 화면에서 개발자 모드를 켜세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    FlatButton {
                        text: "확장 관리 주소 복사·브라우저 열기"
                        enabled: !dialog.setupUnavailable
                        onClicked: dialog.browserSetup.openManager()
                    }
                    FlatField {
                        Layout.fillWidth: true
                        readOnly: true
                        selectByMouse: true
                        text: dialog.browserSetup ? dialog.browserSetup.managerAddress : ""
                        Accessible.name: "확장 관리 주소"
                    }
                    Label {
                        Layout.fillWidth: true
                        text: dialog.browserSetup && dialog.browserSetup.extensionKind === "firefox"
                            ? "2. 아래 폴더의 manifest.json 파일을 선택하세요."
                            : "2. ‘압축해제된 확장 프로그램 로드’를 누르세요. 폴더 창 주소창에 아래 경로를 붙여 넣고 Enter를 누르세요. ‘문서’ 폴더가 아니라 manifest.json이 있는 폴더를 선택합니다."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    FlatField {
                        Layout.fillWidth: true
                        readOnly: true
                        selectByMouse: true
                        text: dialog.browserSetup ? dialog.browserSetup.extensionPath : ""
                        placeholderText: "확장 폴더를 준비하는 중…"
                        Accessible.name: "확장 폴더 경로"
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
                        text: "3. 같은 프로필에서 X에 로그인한 뒤, 위의 연결 버튼을 누르세요. 연결 중이라면 열린 연결 탭을 새로고침하지 말고 취소 후 다시 연결하세요."
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
                        text: "앱을 업데이트한 뒤 확장이 응답하지 않으면 확장 관리에서 ‘새로고침’을 누르고 다시 연결하세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        opacity: 0.72
                    }
                    MenuSeparator { Layout.fillWidth: true }
                }
            }

            ToolButton {
                id: advancedMethodToggle
                Layout.fillWidth: true
                implicitHeight: 36
                text: dialog.methodIndex === 1 ? "고급 연결 · 토큰 입력 ▾" : "고급 연결 · 토큰 입력 ▸"
                enabled: !dialog.unavailable && !dialog.accounts.busy
                HoverHandler {
                    cursorShape: Qt.PointingHandCursor
                }
                contentItem: Text {
                    text: advancedMethodToggle.text
                    font.pixelSize: 14
                    color: "#f4f1f1"
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: 10
                    border.width: advancedMethodToggle.visualFocus ? 2 : 0
                    border.color: "#ff8791"
                    color: advancedMethodToggle.hovered || dialog.methodIndex === 1 ? "#242424" : "#1c1c1c"
                }
                onClicked: dialog.methodIndex = dialog.methodIndex === 1 ? 0 : 1
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: dialog.methodIndex === 1
                spacing: 10

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
                    text: "이 Windows 계정에 암호화해 보관합니다. 저장만으로 실제 X 인증이 확인되지는 않습니다. 값은 다른 사람에게 보내지 마세요."
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    opacity: 0.72
                }
                FlatButton {
                    Layout.fillWidth: true
                    text: "토큰 보관"
                    tone: "primary"
                    enabled: !dialog.unavailable && !dialog.accounts.busy && tokenField.text.trim().length > 0
                    onClicked: dialog.submitToken()
                }
            }

            Label {
                Layout.fillWidth: true
                visible: dialog.methodIndex === 0 && dialog.browserSetup
                    && dialog.browserSetup.selectedBrowser.length > 0
                    && (dialog.browserSetup.error.length > 0
                        || (setupToggle.checked && dialog.browserSetup.status.length > 0))
                text: dialog.browserSetup ? (dialog.browserSetup.error || dialog.browserSetup.status) : ""
                color: dialog.browserSetup && dialog.browserSetup.error.length > 0 ? "#ff8791" : "#b9afb1"
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                Accessible.role: Accessible.StaticText
            }
            Label {
                Layout.fillWidth: true
                visible: dialog.accounts && dialog.accounts.connected && !dialog.accounts.busy
                text: dialog.accounts ? dialog.accounts.authenticationLabel : ""
                color: dialog.accounts && dialog.accounts.authenticationState === "reconnect_required" ? "#ff8791" : "#b9afb1"
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            Label {
                Layout.fillWidth: true
                visible: dialog.accounts && (dialog.accounts.error.length > 0
                    || (!dialog.accounts.browserConnecting && dialog.accounts.status.length > 0))
                text: dialog.accounts ? (dialog.accounts.error || dialog.accounts.status) : ""
                color: dialog.accounts && dialog.accounts.error.length > 0 ? "#ff8791" : "#b9afb1"
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
