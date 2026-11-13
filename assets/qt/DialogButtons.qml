import QtQuick
import QtQuick.Controls

// The bottom row of a dialog: buttons keep their own width and sit at the
// right, the main action first and close or cancel last.
DialogButtonBox {
    background: null
    alignment: Qt.AlignRight
    spacing: Theme.gapSmall
    leftPadding: Theme.dialogPadding
    rightPadding: Theme.dialogPadding
    topPadding: Theme.gap
    bottomPadding: 20
}
