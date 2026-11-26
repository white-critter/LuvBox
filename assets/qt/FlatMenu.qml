import QtQuick
import QtQuick.Controls

// A drop-down list of actions in the app's colors. Its rows are FlatMenuItem.
Menu {
    id: menu
    margins: 8
    padding: 6
    overlap: 0
    delegate: FlatMenuItem {}
    contentItem: ListView {
        id: menuList
        implicitHeight: contentHeight
        model: menu.contentModel
        interactive: true
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {
            policy: menuList.contentHeight > menuList.height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        }
    }
    background: Rectangle {
        implicitWidth: 216
        implicitHeight: Theme.controlHeight
        radius: Theme.radiusControl + 2
        color: Theme.field
        border.width: 1
        border.color: Theme.lineStrong
    }
}
