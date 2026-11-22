import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
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
    // In a wide window the opened help stands beside the connection instead of under it. In one
    // column the dialog was taller than a window of the default size and had to be scrolled,
    // and the help opens by itself the first time someone connects.
    readonly property bool sideBySide: methodIndex === 0 && setupToggle.checked && !!parent && parent.width >= 1040
    preferredWidth: sideBySide ? 980 : 560
    readonly property int windowLimit: parent ? Math.max(180, parent.height - 32) : 720
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    implicitHeight: contentColumn.implicitHeight + chromeHeight
    height: Math.min(implicitHeight, windowLimit)

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
        objectName: "connectionScroll"
        clip: true
        contentWidth: availableWidth
        // The bar gets its own lane, so it never covers a control's right edge.
        rightPadding: ScrollBar.vertical.policy === ScrollBar.AlwaysOn ? 14 : 0
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
                    ? "브라우저에서 쓰는 X 계정을 연결합니다."
                    : "X의 로그인 토큰을 입력하세요."
                color: Theme.textMuted
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }

            GridLayout {
                objectName: "connectionBrowserMethod"
                Layout.fillWidth: true
                visible: dialog.methodIndex === 0
                columns: dialog.sideBySide ? 2 : 1
                uniformCellWidths: true
                rowSpacing: 8
                columnSpacing: Theme.gapLarge

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    spacing: 8

                    Label { text: "연결할 브라우저"; color: Theme.textMuted; font.pixelSize: Theme.fontSmall }
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
                        onActivated: {
                            dialog.browserSetup.setBrowser(currentValue)
                            currentIndex = Qt.binding(function() {
                                if (!dialog.browserSetup)
                                    return -1
                                var choices = dialog.browserSetup.browsers
                                for (var i = 0; i < choices.length; ++i)
                                    if (choices[i].id === dialog.browserSetup.selectedBrowser)
                                        return i
                                return -1
                            })
                        }
                    }
                    Label {
                        objectName: "noBrowserMessage"
                        Layout.fillWidth: true
                        visible: dialog.browserSetup && !dialog.browserSetup.busy
                            && !dialog.browserSetup.selectedBrowser.length
                        text: dialog.browserSetup && dialog.browserSetup.error
                            ? dialog.browserSetup.error
                            : "Chrome·Edge·Firefox 중 하나를 설치하고 앱을 다시 여세요."
                        color: Theme.error
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "X에 로그인하고 확장을 설치한 일반 프로필을 사용하세요."
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        color: Theme.textMuted
                    }
                    FlatButton {
                        objectName: "connectBrowserButton"
                        Layout.fillWidth: true
                        text: dialog.accounts && dialog.accounts.cancelling ? "연결을 취소하는 중…"
                              : dialog.accounts && dialog.accounts.busy ? "연결 진행 중…"
                              : dialog.accounts && dialog.accounts.connected ? "다시 연결" : "X 계정 연결"
                        tone: dialog.accounts && dialog.accounts.busy ? "quiet" : "primary"
                        enabled: !dialog.setupUnavailable && !dialog.accounts.busy
                            && dialog.browserSetup.selectedBrowser.length > 0
                        onClicked: dialog.accounts.connectBrowser(dialog.browserSetup.selectedBrowser)
                    }
                    Frame {
                        Layout.fillWidth: true
                        visible: dialog.accounts && dialog.accounts.browserConnecting
                        padding: Theme.gapLarge
                        background: Card { glow: 0.14 }
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
                                color: Theme.textMuted
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: dialog.accounts && dialog.accounts.pairingCode.length > 0
                                text: "브라우저 확장의 코드와 비교하세요."
                                wrapMode: Text.Wrap
                            }
                            Label {
                                text: dialog.accounts ? dialog.accounts.pairingCode : ""
                                font.pixelSize: Theme.fontDisplay
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
                                font.pixelSize: Theme.fontHeading
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
                        objectName: "connectionHelpToggle"
                        Layout.fillWidth: true
                        implicitHeight: 36
                        checkable: true
                        text: checked ? "확장 설치·연결 도움말 ▾" : "확장 설치·연결 도움말 ▸"
                        Keys.onReturnPressed: event => { toggle(); event.accepted = true }
                        Keys.onEnterPressed: event => { toggle(); event.accepted = true }
                        HoverHandler {
                            cursorShape: Qt.PointingHandCursor
                        }
                        contentItem: Text {
                            text: setupToggle.text
                            font.pixelSize: Theme.fontBody
                            color: Theme.text
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: Theme.radiusControl
                            border.width: setupToggle.visualFocus ? 2 : 0
                            border.color: Theme.focus
                            color: setupToggle.hovered || setupToggle.checked ? Theme.raised : Theme.field
                        }
                    }
                }
                ColumnLayout {
                    objectName: "connectionHelp"
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    visible: setupToggle.checked
                    spacing: 8
                    Label {
                        Layout.fillWidth: true
                        text: "X에 로그인한 프로필에 확장을 설치하세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        text: dialog.browserSetup && dialog.browserSetup.extensionKind === "firefox"
                            ? "1. ‘임시 부가 기능 로드’를 누르세요. Firefox 재시작 후 다시 로드하세요."
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
                            ? "2. 아래 폴더에서 manifest.json 파일을 선택하세요."
                            : "2. ‘압축해제된 확장 프로그램 로드’에서 아래 폴더를 선택하세요."
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
                        text: "3. 같은 프로필에서 X에 로그인하고 ‘X 계정 연결’을 누르세요."
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
                        text: "응답이 없으면 확장 관리에서 새로고침한 뒤 다시 연결하세요."
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Theme.textMuted
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.gapTiny
                        implicitHeight: 1
                        color: Theme.line
                        visible: !dialog.sideBySide
                    }
                }
            }

            ToolButton {
                id: advancedMethodToggle
                Layout.fillWidth: true
                implicitHeight: 36
                text: dialog.methodIndex === 1 ? "고급 연결 · 토큰 입력 ▾" : "고급 연결 · 토큰 입력 ▸"
                enabled: !dialog.unavailable && !dialog.accounts.busy
                Keys.onReturnPressed: event => {
                    if (enabled) {
                        click()
                        event.accepted = true
                    }
                }
                Keys.onEnterPressed: event => {
                    if (enabled) {
                        click()
                        event.accepted = true
                    }
                }
                HoverHandler {
                    cursorShape: advancedMethodToggle.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                }
                contentItem: Text {
                    text: advancedMethodToggle.text
                    font.pixelSize: Theme.fontBody
                    color: advancedMethodToggle.enabled ? Theme.text : Theme.textDisabled
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    border.width: advancedMethodToggle.visualFocus ? 2 : 0
                    border.color: Theme.focus
                    color: advancedMethodToggle.hovered || dialog.methodIndex === 1 ? Theme.raised : Theme.field
                }
                onClicked: dialog.methodIndex = dialog.methodIndex === 1 ? 0 : 1
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: dialog.methodIndex === 1
                spacing: 10

                Label {
                    Layout.fillWidth: true
                    text: "Chrome에서 F12 › 애플리케이션(Application) › 쿠키(Cookies) › https://x.com의 auth_token 값을 복사하세요."
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
                    text: "토큰은 다른 사람에게 보내지 마세요."
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: Theme.textMuted
                }
                FlatButton {
                    Layout.fillWidth: true
                    text: "토큰 저장"
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
                color: dialog.browserSetup && dialog.browserSetup.error.length > 0 ? Theme.error : Theme.textMuted
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                Accessible.role: Accessible.StaticText
            }
            Label {
                Layout.fillWidth: true
                visible: dialog.accounts && dialog.accounts.connected && !dialog.accounts.busy
                text: dialog.accounts ? dialog.accounts.authenticationLabel : ""
                color: dialog.accounts && dialog.accounts.authenticationState === "reconnect_required" ? Theme.error : Theme.textMuted
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            Label {
                Layout.fillWidth: true
                visible: dialog.accounts && (dialog.accounts.error.length > 0
                    || (!dialog.accounts.browserConnecting && dialog.accounts.status.length > 0))
                text: dialog.accounts ? (dialog.accounts.error || dialog.accounts.status) : ""
                color: dialog.accounts && dialog.accounts.error.length > 0 ? Theme.error : Theme.textMuted
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                Accessible.role: Accessible.StaticText
            }
        }
    }

    footer: DialogButtons {
        FlatButton {
            text: "현재 계정 연결 해제"
            tone: "danger"
            visible: dialog.accounts && dialog.accounts.connected
            enabled: !dialog.unavailable && !dialog.accounts.busy
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: dialog.accounts.disconnectAccount()
        }
        FlatButton {
            text: dialog.accounts && dialog.accounts.cancelling ? "취소하는 중…"
                  : dialog.accounts && dialog.accounts.browserConnecting ? "연결 취소" : "닫기"
            enabled: !dialog.accounts || !dialog.accounts.cancelling
            DialogButtonBox.buttonRole: dialog.accounts && dialog.accounts.browserConnecting
                ? DialogButtonBox.ActionRole : DialogButtonBox.RejectRole
            onClicked: {
                if (dialog.accounts && dialog.accounts.browserConnecting)
                    dialog.accounts.cancelBrowser()
            }
        }
        onRejected: {
            if (dialog.accounts && dialog.accounts.browserConnecting)
                dialog.accounts.cancelBrowser()
            else
                dialog.close()
        }
    }
}
