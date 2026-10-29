import QtQuick
import QtQuick.Controls
import QtQuick.Window

ScrollView {
    id: control

    // ScrollView clips its contents but does not reveal controls reached by Tab.
    // Follow focus only when the focused item belongs to this scrolling form.
    function revealFocus() {
        const host = control.Window.window
        const item = host ? host.activeFocusItem : null
        const flick = control.contentItem
        if (!control.visible || !item || !flick || flick.contentY === undefined)
            return
        let ancestor = item
        while (ancestor && ancestor !== flick)
            ancestor = ancestor.parent
        if (ancestor !== flick)
            return
        const point = item.mapToItem(flick.contentItem, 0, 0)
        const top = point.y - 8
        const bottom = point.y + Math.min(item.height, flick.height - 16) + 8
        let target = flick.contentY
        if (top < target)
            target = top
        else if (bottom > target + flick.height)
            target = bottom - flick.height
        flick.contentY = Math.max(0, Math.min(target, Math.max(0, flick.contentHeight - flick.height)))
    }

    Connections {
        target: control.Window.window
        function onActiveFocusItemChanged() { Qt.callLater(control.revealFocus) }
    }
    onHeightChanged: Qt.callLater(revealFocus)
}
