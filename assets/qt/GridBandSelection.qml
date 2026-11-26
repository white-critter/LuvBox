import QtQuick

// Mouse drag-box selection inside a GridView, with edge auto-scrolling.
DragHandler {
    id: bandDrag
    required property GridView grid
    required property int columns
    property string bandObjectName: ""
    property var wheelReset: null
    target: null
    acceptedDevices: PointerDevice.Mouse
    acceptedButtons: Qt.LeftButton
    dragThreshold: 10
    property real startX: 0
    property real startY: 0
    property real currentX: 0
    property real currentY: 0
    // Ctrl or Shift held when the button went down adds to the selection,
    // even if the key is let go before the drag ends.
    property int pressModifiers: Qt.NoModifier

    signal bandStarted(bool additive)
    signal bandUpdated(int row0, int row1, int col0, int col1, int cols)
    signal bandEnded()

    onGrabChanged: (transition, point) => {
        if (transition === PointerDevice.GrabPassive)
            pressModifiers = centroid.modifiers
    }
    function update() {
        const top = Math.min(startY, currentY)
        const bottom = Math.max(startY, currentY)
        const left = Math.min(startX, currentX)
        const right = Math.max(startX, currentX)
        const cw = Math.max(1, bandDrag.grid.cellWidth)
        const ch = Math.max(1, bandDrag.grid.cellHeight)
        const cols = Math.max(1, bandDrag.columns)
        bandDrag.bandUpdated(
            Math.floor(Math.max(0, top) / ch),
            Math.floor(Math.max(0, bottom) / ch),
            Math.floor(Math.max(0, left) / cw),
            Math.floor(Math.max(0, right) / cw),
            cols
        )
    }
    function inView(scenePoint) {
        return bandDrag.grid.mapFromItem(null, scenePoint.x, scenePoint.y)
    }
    function follow() {
        const point = inView(centroid.scenePosition)
        currentX = Math.max(0, Math.min(bandDrag.grid.width, point.x))
        currentY = point.y + bandDrag.grid.contentY - bandDrag.grid.originY
        update()
    }
    onActiveChanged: {
        if (active) {
            bandDrag.grid.forceActiveFocus()
            if (bandDrag.wheelReset)
                bandDrag.wheelReset()
            const pressed = inView(centroid.scenePressPosition)
            startX = Math.max(0, Math.min(bandDrag.grid.width, pressed.x))
            startY = pressed.y + bandDrag.grid.contentY - bandDrag.grid.originY
            const held = pressModifiers | centroid.modifiers
            bandDrag.bandStarted((held & (Qt.ControlModifier | Qt.ShiftModifier)) !== 0)
            follow()
        } else {
            bandDrag.bandEnded()
        }
    }
    onCentroidChanged: if (active) follow()

    readonly property Timer _autoScroll: Timer {
        interval: 30
        repeat: true
        running: bandDrag.active
        onTriggered: {
            const y = bandDrag.inView(bandDrag.centroid.scenePosition).y
            let step = 0
            if (y < 28)
                step = -Math.min(48, 8 + (28 - y) * 0.6)
            else if (y > bandDrag.grid.height - 28)
                step = Math.min(48, 8 + (y - (bandDrag.grid.height - 28)) * 0.6)
            if (!step)
                return
            const minimum = bandDrag.grid.originY
            const maximum = minimum + Math.max(0, bandDrag.grid.contentHeight - bandDrag.grid.height)
            const next = Math.max(minimum, Math.min(maximum, bandDrag.grid.contentY + step))
            if (next !== bandDrag.grid.contentY) {
                bandDrag.grid.contentY = next
                bandDrag.follow()
            }
        }
    }

    readonly property Rectangle _band: Rectangle {
        parent: bandDrag.grid
        objectName: bandDrag.bandObjectName
        visible: bandDrag.active
        z: 5
        x: Math.min(bandDrag.startX, bandDrag.currentX)
        y: Math.min(bandDrag.startY, bandDrag.currentY) - (bandDrag.grid.contentY - bandDrag.grid.originY)
        width: Math.abs(bandDrag.currentX - bandDrag.startX)
        height: Math.abs(bandDrag.currentY - bandDrag.startY)
        color: Qt.alpha(Theme.accent, 0.2)
        border.width: 1
        border.color: Theme.accent
    }
}
