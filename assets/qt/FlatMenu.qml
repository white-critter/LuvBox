import QtQuick
import QtQuick.Controls

// A drop-down list of actions in the app's colors. Its rows are FlatMenuItem.
Menu {
    id: menu
    padding: 6
    overlap: 0
    delegate: FlatMenuItem {}
    background: Rectangle {
        implicitWidth: 216
        implicitHeight: Theme.controlHeight
        radius: Theme.radiusControl + 2
        color: Theme.field
        border.width: 1
        border.color: Theme.lineStrong
    }
}
