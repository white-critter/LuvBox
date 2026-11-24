pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: chip
    required property var modelData
    property real maxWidth: 240
    property bool tagsLocked: false
    property bool teaches: false
    property string nameObjectName: "previewTagName"
    property string confirmObjectName: "previewTagConfirm"
    property string removeObjectName: "previewTagRemove"
    property string confirmTip: qsTr("맞아요 · AI 학습에 반영")
    property string removeAiTip: qsTr("이 태그 아님 · AI 학습에 반영")

    signal tagActivated(string tagName, bool isAi)
    signal confirmRequested(string tagName)
    signal removeRequested(string tagName)

    readonly property string tagName: modelData.tag || ""
    readonly property bool isConfirmed: modelData.source === "confirmed"
    readonly property bool isAi: modelData.source === "ai" || isConfirmed

    color: tagNameButton.hovered
        ? (isAi ? Theme.aiTagHover : Theme.userTagHover)
        : (isAi ? Theme.aiTagFill : Theme.userTagFill)
    radius: Theme.radiusSmall
    border.width: 1
    border.color: isAi ? Theme.aiTagLine : Theme.userTagLine
    implicitWidth: tagRow.implicitWidth + 4
    implicitHeight: Theme.compactHeight
    width: Math.min(implicitWidth, chip.maxWidth)

    RowLayout {
        id: tagRow
        anchors.fill: parent
        anchors.leftMargin: 2
        anchors.rightMargin: 2
        spacing: 2

        PreviewMetadataButton {
            id: tagNameButton
            objectName: chip.nameObjectName
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: (chip.isAi ? "AI · #" : "#") + chip.tagName + (chip.isConfirmed ? " ✓" : "")
            foreground: chip.isAi ? Theme.aiTagText : Theme.userTagText
            tip: chip.isConfirmed ? qsTr("맞다고 알려 준 AI 태그 · 모아보기")
                : (chip.isAi ? qsTr("AI가 붙인 태그 · 모아보기")
                        : (chip.teaches ? qsTr("내 태그 (AI 학습 사용 중) · 모아보기") : qsTr("이 태그만 보기")))
            Accessible.name: qsTr("태그 %1 모아보기").arg(text)
            onClicked: chip.tagActivated(chip.tagName, chip.isAi)
        }

        PreviewMetadataButton {
            objectName: chip.confirmObjectName
            visible: chip.modelData.source === "ai"
            enabled: !chip.tagsLocked
            text: "✓"
            font.bold: true
            foreground: Theme.textMuted
            hoverForeground: Theme.good
            tip: chip.confirmTip
            Accessible.name: qsTr("AI 태그 %1 맞아요").arg(chip.tagName)
            onClicked: chip.confirmRequested(chip.tagName)
        }

        PreviewMetadataButton {
            objectName: chip.removeObjectName
            enabled: !chip.tagsLocked
            text: "×"
            font.bold: true
            foreground: Theme.textMuted
            hoverForeground: Theme.error
            tip: chip.isAi ? chip.removeAiTip : qsTr("태그 삭제")
            Accessible.name: chip.isAi ? qsTr("AI 태그 %1 아님").arg(chip.tagName)
                : qsTr("태그 %1 삭제").arg(chip.tagName)
            onClicked: chip.removeRequested(chip.tagName)
        }
    }
}
