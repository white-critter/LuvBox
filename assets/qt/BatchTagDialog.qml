import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
    id: dialog
    property alias inputObjectName: tagInput.objectName
    property alias inputField: tagInput
    property bool locked: false
    property string lockTip: ""
    property string notice: ""
    signal submitRequested(string tag)

    title: "선택한 항목에 태그 추가"
    preferredWidth: 340
    light: 0.12
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    function clearInput() {
        tagInput.clearText()
        notice = ""
    }

    function submitTag() {
        if (tagInput.cleanTag.length > 0 && !dialog.locked)
            dialog.submitRequested(tagInput.cleanTag)
    }

    function submitTo(library, tag) {
        const previousStatus = library ? library.status : ""
        if (library && !library.tagsLocked && library.addTagToSelection(tag) > 0)
            acceptSubmitted()
        else
            showNotice(library && library.status !== previousStatus ? library.status : "")
    }

    function acceptSubmitted() {
        clearInput()
        dialog.close()
    }

    function showNotice(message) {
        dialog.notice = message || "태그를 추가하지 못했습니다."
    }

    onOpened: {
        clearInput()
        tagInput.forceActiveFocus()
    }
    onClosed: clearInput()

    contentItem: ColumnLayout {
        spacing: Theme.gapSmall
        FlatField {
            id: tagInput
            readonly property string cleanTag: displayText.trim().replace(/^#+/, "").trim()
            Layout.fillWidth: true
            maximumLength: 50
            placeholderText: "태그 이름 입력"
            enabled: !dialog.locked
            onTextChanged: dialog.notice = ""
            onPreeditTextChanged: if (activeFocus) dialog.notice = ""
            onAccepted: dialog.submitTag()
        }
        Label {
            Layout.fillWidth: true
            visible: dialog.notice.length > 0
            text: dialog.notice
            color: Theme.error
            font.pixelSize: Theme.fontSmall
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
        }
    }

    footer: DialogButtons {
        FlatButton {
            text: "추가"
            tone: "primary"
            enabled: tagInput.cleanTag.length > 0 && !dialog.locked
            tip: dialog.locked ? dialog.lockTip : ""
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: dialog.submitTag()
        }
        FlatButton {
            text: "취소"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
    }
}
