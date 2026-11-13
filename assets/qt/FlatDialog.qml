import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// The frame every dialog shares: the title at the top left with an optional
// line under it, one padding, and the buttons at the bottom right
// (DialogButtons). `banner` takes notes that belong under the title.
Dialog {
    id: dialog
    property string subtitle: ""
    property int preferredWidth: 520
    // Space kept between the dialog and the window edge, both sides together.
    property int windowMargin: 32
    // Strength of the red light at the top of the dialog; 0 turns it off.
    property real light: 0.16
    property alias banner: bannerColumn.data
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(preferredWidth, parent ? Math.max(240, parent.width - windowMargin) : preferredWidth)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    leftPadding: Theme.dialogPadding
    rightPadding: Theme.dialogPadding
    topPadding: Theme.gapLarge
    bottomPadding: Theme.gapSmall
    // Drawn like the startup screen's card, with a little of its red light at the top.
    background: Card {
        radius: Theme.radiusDialog
        topColor: Theme.dialogTop
        bottomColor: Theme.dialog
        border.color: Theme.lineStrong
        glow: dialog.light
    }
    header: Column {
        width: dialog.width
        topPadding: 20
        leftPadding: Theme.dialogPadding
        rightPadding: Theme.dialogPadding
        spacing: Theme.gapTiny
        Label {
            width: parent.width - parent.leftPadding - parent.rightPadding
            text: dialog.title
            font.pixelSize: Theme.fontHeading
            font.bold: true
            color: Theme.text
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        Label {
            width: parent.width - parent.leftPadding - parent.rightPadding
            visible: dialog.subtitle.length > 0
            text: dialog.subtitle
            font.pixelSize: Theme.fontSmall
            color: Theme.textMuted
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        Column {
            id: bannerColumn
            width: parent.width - parent.leftPadding - parent.rightPadding
            spacing: Theme.gapTiny
        }
    }
}
