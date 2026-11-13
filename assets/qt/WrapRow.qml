import QtQuick

// Places its visible children left to right and starts a new line when the
// next one does not fit, like words in a paragraph. Its height follows the
// lines it needs, so the content under it moves up when a line empties.
Item {
    id: host
    property int spacing: Theme.gapSmall
    property real laidHeight: 0
    // Children end at the right edge instead of starting at the left.
    property bool alignRight: false
    implicitWidth: 0
    implicitHeight: laidHeight

    function layout() {
        const rows = []
        let row = []
        let x = 0
        const limit = width
        for (let index = 0; index < children.length; index++) {
            const child = children[index]
            if (!child || !child.visible || child.width < 1 || child.height < 1)
                continue
            if (row.length > 0 && x + child.width > limit) {
                rows.push(row)
                row = []
                x = 0
            }
            row.push(child)
            x += child.width + spacing
        }
        if (row.length > 0)
            rows.push(row)
        let y = 0
        for (let line = 0; line < rows.length; line++) {
            let rowHeight = 0
            let used = -spacing
            for (const child of rows[line]) {
                rowHeight = Math.max(rowHeight, child.height)
                used += child.width + spacing
            }
            let left = alignRight ? Math.max(0, limit - used) : 0
            for (const child of rows[line]) {
                child.x = left
                child.y = y + (rowHeight - child.height) / 2
                left += child.width + spacing
            }
            y += rowHeight + (line < rows.length - 1 ? spacing : 0)
        }
        if (Math.abs(laidHeight - y) > 0.5)
            laidHeight = y
    }

    function watch() {
        // A child that appears, disappears or changes size after the first
        // layout would otherwise stay where it was and cover its neighbour.
        for (let index = 0; index < children.length; index++) {
            const child = children[index]
            child.visibleChanged.connect(later)
            child.widthChanged.connect(later)
            child.heightChanged.connect(later)
        }
    }

    function later() { Qt.callLater(layout) }

    onWidthChanged: layout()
    onVisibleChanged: later()
    onAlignRightChanged: later()
    Component.onCompleted: {
        watch()
        later()
    }
}
