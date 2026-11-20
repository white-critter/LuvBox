import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A small round "?" beside a heading that opens a help card for that screen.
// Resting the pointer on it shows the card while the pointer stays on the "?"
// or on the card; a click (or Enter/Space) keeps it open until a click
// elsewhere or Escape. The card's text goes in `content`.
AbstractButton {
    id: control
    property string title: ""
    property int cardWidth: 420
    // The card's contents: Labels, rows, … laid out one under another.
    property alias content: body.data
    readonly property bool showing: card.visible
    // Opened by a click: stays when the pointer leaves.
    property bool pinned: false

    implicitWidth: 22
    implicitHeight: 22
    focusPolicy: Qt.TabFocus
    Accessible.role: Accessible.Button
    Accessible.name: title.length > 0 ? title + " 도움말" : "도움말"
    Keys.onReturnPressed: control.click()
    Keys.onEnterPressed: control.click()

    function close() { card.close() }
    onVisibleChanged: if (!visible) card.close()
    Keys.onEscapePressed: event => {
        if (card.visible) {
            card.close()
            event.accepted = true
        } else {
            event.accepted = false
        }
    }

    onClicked: {
        pinned = !(pinned && card.visible)
        if (pinned)
            card.open()
        else
            card.close()
    }

    function settle() {
        if (control.pinned)
            return
        if (pointer.hovered || cardPointer.hovered)
            closeLater.stop()
        else if (card.visible)
            closeLater.restart()
        if (pointer.hovered && !card.visible)
            openLater.restart()
        else if (!pointer.hovered)
            openLater.stop()
    }

    Timer { id: openLater; interval: 350; onTriggered: if (pointer.hovered) card.open() }
    // A short grace so the pointer can travel from the "?" onto the card.
    Timer { id: closeLater; interval: 250; onTriggered: if (!control.pinned && !pointer.hovered && !cardPointer.hovered) card.close() }

    HoverHandler {
        id: pointer
        cursorShape: Qt.PointingHandCursor
        onHoveredChanged: control.settle()
    }

    background: Rectangle {
        radius: height / 2
        color: control.down ? Theme.raisedPressed
            : (control.hovered || card.visible ? Theme.raisedHover : Theme.raised)
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus ? Theme.focus : (card.visible ? Theme.accentLine : Theme.lineStrong)
    }
    contentItem: Text {
        text: "?"
        font.pixelSize: Theme.fontSmall
        font.bold: true
        color: card.visible ? Theme.accentText : Theme.textMuted
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }

    Popup {
        id: card
        objectName: "helpCard"
        // Under the "?", kept inside the window by the margins.
        y: control.height + Theme.gapSmall
        x: -Theme.gapLarge
        margins: Theme.gap
        width: control.cardWidth
        padding: Theme.cardPadding
        topPadding: Theme.gapLarge
        bottomPadding: Theme.gapLarge
        modal: false
        focus: false
        closePolicy: Popup.CloseOnPressOutsideParent
        onClosed: control.pinned = false
        enter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 120 } }
        exit: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 90 } }
        background: Card {
            radius: Theme.radiusCard
            topColor: Theme.dialogTop
            bottomColor: Theme.dialog
            border.color: Theme.lineStrong
            glow: 0.08
            glowAt: 0.15
            // The whole card, padding included, keeps it open under the pointer.
            HoverHandler {
                id: cardPointer
                onHoveredChanged: control.settle()
            }
        }
        contentItem: ColumnLayout {
            id: column
            spacing: Theme.gap
            Label {
                visible: control.title.length > 0
                text: control.title
                font.pixelSize: Theme.fontSection
                font.bold: true
                color: Theme.text
            }
            ColumnLayout {
                id: body
                Layout.fillWidth: true
                spacing: Theme.gap
            }
        }
    }
}
