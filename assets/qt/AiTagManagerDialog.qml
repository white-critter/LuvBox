pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// The AI tags: add one, switch one off for a while, delete one (the five defaults
// included), and turn one of the person's own tags into an AI tag. What a tag has
// learned so far shows under its name. "지금 다시 분류" gives every photo its AI
// tags again from the stored image features, without reading the photos.
FlatDialog {
    id: dialog
    objectName: "aiTagManagerDialog"
    property var app: null
    // The gallery's by default; the 폴더 정리 tab gives its own controller (the tags are the same).
    property var library: app ? app.library : null
    readonly property var rules: library ? library.aiTagRules : []
    readonly property bool locked: !library || library.tagsLocked || library.busy
    readonly property string typed: newTagField.text.trim().replace(/^#+/, "").trim()
    property string feedbackText: ""
    property string _openedStatus: ""
    // The person's own tags that are not AI tags yet, most used first.
    readonly property var candidates: {
        if (!library)
            return []
        const known = {}
        for (const rule of dialog.rules)
            known[rule.tag] = true
        const found = []
        for (const item of library.userTags) {
            if (item && item.tag && !known[item.tag])
                found.push(item)
        }
        return found.slice(0, 16)
    }
    title: "AI 태그 관리"
    subtitle: "AI가 사진에 자동으로 붙일 태그를 정합니다. 예시 사진 1~4장은 닮은 사진을 찾아 주고, 5장부터는 특징을 배워 더 정확하게 분류합니다."
    preferredWidth: 620
    windowMargin: 48
    height: Math.min(implicitHeight, parent ? parent.height - 48 : implicitHeight)

    function addTyped() {
        if (!dialog.library || dialog.locked || dialog.typed.length === 0)
            return
        if (dialog.library.addAiTag(dialog.typed))
            newTagField.text = ""
    }

    onOpened: {
        dialog._openedStatus = dialog.library ? (dialog.library.status || "") : ""
        dialog.feedbackText = ""
        if (!dialog.locked)
            newTagField.forceActiveFocus()
        else
            closeButton.forceActiveFocus()
    }

    Connections {
        target: dialog.library
        function onChanged() {
            if (!dialog.visible || !dialog.library)
                return
            const status = dialog.library.status || ""
            if (status !== dialog._openedStatus) {
                dialog._openedStatus = status
                if (status.length > 0)
                    dialog.feedbackText = status
            }
        }
    }

    contentItem: FocusScrollView {
        id: dialogScroll
        clip: true
        contentWidth: availableWidth
        rightPadding: ScrollBar.vertical.policy === ScrollBar.AlwaysOn ? 14 : 0
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff

        ColumnLayout {
            id: managerColumn
            width: dialogScroll.availableWidth
            spacing: Theme.gap

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            FlatField {
                id: newTagField
                objectName: "aiTagNewField"
                Layout.fillWidth: true
                maximumLength: 50
                placeholderText: "새 AI 태그 이름 (예: 고양이)"
                enabled: !dialog.locked
                onAccepted: dialog.addTyped()
            }
            FlatButton {
                objectName: "aiTagAdd"
                text: "+ 추가"
                focusPolicy: Qt.TabFocus
                enabled: dialog.typed.length > 0 && !dialog.locked
                tip: dialog.library && dialog.library.tagsLocked ? dialog.library.holdReason
                    : (dialog.library && dialog.library.busy
                        ? (dialog.library.holdReason || "진행 중인 작업이 끝난 뒤 변경할 수 있습니다.") : "")
                onClicked: dialog.addTyped()
            }
        }

        Label {
            Layout.fillWidth: true
            visible: dialog.candidates.length > 0
            text: "내 태그를 AI 태그로 쓰기 · 그 태그를 붙인 사진이 첫 예시가 됩니다"
            color: Theme.textMuted
            font.pixelSize: Theme.fontSmall
            wrapMode: Text.Wrap
        }
        Flow {
            objectName: "aiTagCandidates"
            Layout.fillWidth: true
            visible: dialog.candidates.length > 0
            spacing: Theme.gapSmall
            Repeater {
                model: dialog.candidates
                delegate: FlatButton {
                    required property var modelData
                    objectName: "aiTagCandidate_" + (modelData.tag || "")
                    tone: "chip"
                    compact: true
                    checkLights: false
                    focusPolicy: Qt.TabFocus
                    text: "#" + modelData.tag + " · " + Number(modelData.count || 0).toLocaleString(Qt.locale(), "f", 0)
                    enabled: !dialog.locked
                    tip: "AI 태그로 추가하고 이 태그의 사진으로 배웁니다"
                    onClicked: if (dialog.library) dialog.library.enableUserTagAsAi(modelData.tag)
                }
            }
        }

        FocusScrollView {
            id: ruleScroll
            objectName: "aiTagRuleList"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(ruleColumn.implicitHeight, 340)
            Layout.minimumHeight: Math.min(ruleColumn.implicitHeight, 48)
            clip: true
            contentWidth: availableWidth
            rightPadding: ScrollBar.vertical.policy === ScrollBar.AlwaysOn ? 14 : 0
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            ColumnLayout {
                id: ruleColumn
                width: ruleScroll.availableWidth
                spacing: Theme.gapSmall
                Label {
                    Layout.fillWidth: true
                    visible: dialog.rules.length === 0
                    text: "AI 태그가 없습니다. 위에서 새로 추가하거나 기본 태그를 복원해 주세요."
                    color: Theme.textMuted
                    wrapMode: Text.Wrap
                }
                Repeater {
                    model: dialog.rules
                    delegate: Rectangle {
                        id: row
                        required property var modelData
                        objectName: "aiTagRow_" + (modelData.tag || "")
                        Layout.fillWidth: true
                        implicitHeight: rowLayout.implicitHeight + Theme.gapSmall * 2
                        radius: Theme.radiusControl
                        color: Theme.well
                        border.width: 1
                        border.color: Theme.line
                        RowLayout {
                            id: rowLayout
                            anchors.fill: parent
                            anchors.leftMargin: Theme.gap
                            anchors.rightMargin: Theme.gapSmall
                            spacing: Theme.gapSmall
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                spacing: Theme.gapTiny
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.gapSmall
                                    Label {
                                        objectName: "aiTagName"
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        text: "#" + row.modelData.tag
                                        color: row.modelData.enabled ? Theme.text : Theme.textMuted
                                        font.bold: true
                                        elide: Text.ElideRight
                                        textFormat: Text.PlainText
                                    }
                                    Label {
                                        text: row.modelData.built_in ? "기본" : "직접 추가"
                                        color: Theme.textFaint
                                        font.pixelSize: Theme.fontCaption
                                    }
                                }
                                Label {
                                    objectName: "aiTagStatus"
                                    Layout.fillWidth: true
                                    text: row.modelData.enabled ? row.modelData.status_text
                                                                : "꺼짐 · " + row.modelData.status_text
                                    color: !row.modelData.built_in && row.modelData.pos_count === 0
                                        ? Theme.notice : Theme.textMuted
                                    font.pixelSize: Theme.fontSmall
                                    elide: Text.ElideRight
                                }
                            }
                            FlatSwitch {
                                objectName: "aiTagSwitch_" + (row.modelData.tag || "")
                                focusPolicy: Qt.TabFocus
                                checked: row.modelData.enabled
                                enabled: !dialog.locked
                                Accessible.name: "#" + row.modelData.tag + " 자동분류"
                                onToggled: {
                                    if (dialog.library)
                                        dialog.library.setAiTagEnabled(row.modelData.tag, checked)
                                    checked = Qt.binding(function() { return row.modelData.enabled })
                                }
                            }
                            FlatButton {
                                objectName: "aiTagDelete_" + (row.modelData.tag || "")
                                text: "삭제"
                                tone: "ghost"
                                compact: true
                                focusPolicy: Qt.TabFocus
                                enabled: !dialog.locked
                                tip: "AI 태그에서 뺍니다. 직접 붙인 태그와 사진은 그대로 둡니다"
                                onClicked: if (dialog.library) dialog.library.deleteAiTag(row.modelData.tag)
                            }
                        }
                    }
                }
            }
        }

        Label {
            objectName: "aiTagFeedback"
            Layout.fillWidth: true
            visible: dialog.feedbackText.length > 0
            text: dialog.feedbackText
            color: Theme.notice
            font.pixelSize: Theme.fontSmall
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
        }

        Label {
            objectName: "aiTagHint"
            Layout.fillWidth: true
            text: dialog.library && dialog.library.tagsLocked
                ? dialog.library.holdReason
                : (dialog.library && dialog.library.busy
                    ? (dialog.library.holdReason || "진행 중인 작업이 끝난 뒤 변경할 수 있습니다.")
                    : "태그를 바꾼 뒤 ‘지금 다시 분류’를 누르면 저장해 둔 사진 정보로 바로 다시 분류합니다.")
            color: Theme.textMuted
            font.pixelSize: Theme.fontSmall
            wrapMode: Text.Wrap
        }
        }
    }

    footer: DialogButtons {
        FlatButton {
            objectName: "aiTagRestore"
            text: "기본 태그 복원"
            tone: "ghost"
            focusPolicy: Qt.TabFocus
            enabled: !!dialog.library && dialog.library.aiDeletedBuiltinCount > 0 && !dialog.locked
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: if (dialog.library) dialog.library.restoreBuiltinAiTags()
        }
        FlatButton {
            objectName: "aiTagReclassify"
            text: "지금 다시 분류"
            tone: "primary"
            enabled: !dialog.locked
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: {
                if (dialog.library && dialog.library.reclassifyNow())
                    dialog.close()
            }
        }
        FlatButton {
            id: closeButton
            objectName: "aiTagClose"
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }
}
