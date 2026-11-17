import QtQuick
import QtQuick.Controls

// Draw caption symbols at a fixed size instead of depending on a symbol font.
Button {
    id: control
    property string windowAction: "minimize"
    implicitWidth: 44
    implicitHeight: Theme.controlHeight
    hoverEnabled: true
    focusPolicy: Qt.TabFocus
    Accessible.name: windowAction === "close" ? "트레이로 숨기기" : windowAction === "minimize" ? "최소화"
        : windowAction === "restore" ? "창 복원" : "최대화"
    ToolTip.visible: hovered
    ToolTip.delay: 600
    ToolTip.text: Accessible.name
    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.down ? Theme.raisedPressed : control.hovered
            ? (control.windowAction === "close" ? Theme.accent : Theme.hover) : "transparent"
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.focus
    }
    contentItem: Canvas {
        id: symbol
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Connections {
            target: control
            function onWindowActionChanged() { symbol.requestPaint() }
            function onEnabledChanged() { symbol.requestPaint() }
        }
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.strokeStyle = control.enabled ? Theme.text : Theme.textDisabled
            ctx.lineWidth = 1
            const x = Math.floor(width / 2) - 5 + 0.5
            const y = Math.floor(height / 2) - 5 + 0.5
            if (control.windowAction === "close") {
                ctx.moveTo(x, y); ctx.lineTo(x + 10, y + 10)
                ctx.moveTo(x + 10, y); ctx.lineTo(x, y + 10)
            } else if (control.windowAction === "minimize") {
                ctx.moveTo(x, y + 5); ctx.lineTo(x + 10, y + 5)
            } else if (control.windowAction === "restore") {
                ctx.moveTo(x + 3, y + 3); ctx.lineTo(x + 3, y)
                ctx.lineTo(x + 11, y); ctx.lineTo(x + 11, y + 8); ctx.lineTo(x + 8, y + 8)
                ctx.rect(x, y + 3, 8, 8)
            } else {
                ctx.rect(x, y, 10, 10)
            }
            ctx.stroke()
        }
    }
}
