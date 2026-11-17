pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Window

// Thin edge targets leave the interior controls alone and let Windows do the resize.
Item {
    id: frame
    required property var targetWindow
    visible: targetWindow.visibility === Window.Windowed
    Repeater {
        model: [Qt.LeftEdge, Qt.RightEdge, Qt.TopEdge, Qt.BottomEdge,
            Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.TopEdge,
            Qt.LeftEdge | Qt.BottomEdge, Qt.RightEdge | Qt.BottomEdge]
        MouseArea {
            required property int modelData
            readonly property bool atLeft: !!(modelData & Qt.LeftEdge)
            readonly property bool atRight: !!(modelData & Qt.RightEdge)
            readonly property bool atTop: !!(modelData & Qt.TopEdge)
            readonly property bool atBottom: !!(modelData & Qt.BottomEdge)
            readonly property bool corner: (atLeft || atRight) && (atTop || atBottom)
            width: corner ? 10 : (atLeft || atRight) ? 5 : frame.width - 20
            height: corner ? 10 : (atTop || atBottom) ? 5 : frame.height - 20
            x: atLeft ? 0 : atRight ? frame.width - width : 10
            y: atTop ? 0 : atBottom ? frame.height - height : 10
            cursorShape: corner ? ((atLeft && atTop) || (atRight && atBottom) ? Qt.SizeFDiagCursor : Qt.SizeBDiagCursor)
                : (atLeft || atRight) ? Qt.SizeHorCursor : Qt.SizeVerCursor
            onPressed: frame.targetWindow.startSystemResize(modelData)
        }
    }
}
