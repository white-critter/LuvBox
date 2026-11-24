pragma ComponentBehavior: Bound

import QtQuick

FlatButton {
    id: metadataButton
    property color foreground: Theme.text
    property color hoverForeground: foreground
    tone: "ghost"
    compact: true
    focusPolicy: Qt.TabFocus
    leftPadding: 6
    rightPadding: 6
    contentItem: Text {
        text: metadataButton.text
        textFormat: Text.PlainText
        font: metadataButton.font
        color: !metadataButton.enabled ? Theme.textDisabled
            : (metadataButton.hovered ? metadataButton.hoverForeground : metadataButton.foreground)
        verticalAlignment: Text.AlignVCenter
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
    }
}
