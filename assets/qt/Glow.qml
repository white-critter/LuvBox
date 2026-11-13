import QtQuick

// The soft red light of the startup screen. Brightest at one point and gone at
// the rim, with the same falloff the startup picture uses. Painted once at a
// small size and stretched over the item: light has no detail to lose.
Item {
    id: glow
    // Opacity at the brightest point.
    property real strength: 0.22
    property color tint: Theme.glow
    // The brightest point, as fractions of the item's width and height.
    property real focusX: 0.5
    property real focusY: 0
    // 1 lets the light reach the item's edges; less keeps it closer to the focus.
    property real reach: 1
    readonly property int grain: 192

    onStrengthChanged: canvas.requestPaint()
    onTintChanged: canvas.requestPaint()
    onFocusXChanged: canvas.requestPaint()
    onFocusYChanged: canvas.requestPaint()
    onReachChanged: canvas.requestPaint()

    Canvas {
        id: canvas
        width: glow.grain
        height: glow.grain
        smooth: true
        transform: Scale {
            xScale: glow.width / glow.grain
            yScale: glow.height / glow.grain
        }
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            // An ellipse around the focus that ends exactly at the item's nearest
            // edges, so the light never stops at a visible straight line. A focus
            // on an edge (the top of a window) spreads across the whole item.
            const fx = glow.focusX
            const fy = glow.focusY
            const spanX = fx <= 0 || fx >= 1 ? 1 : Math.min(fx, 1 - fx)
            const spanY = fy <= 0 || fy >= 1 ? 1 : Math.min(fy, 1 - fy)
            const unit = 100
            ctx.save()
            ctx.translate(fx * width, fy * height)
            ctx.scale(Math.max(1, spanX * width * glow.reach) / unit, Math.max(1, spanY * height * glow.reach) / unit)
            const light = ctx.createRadialGradient(0, 0, 0, 0, 0, unit)
            const steps = 14
            for (let step = 0; step <= steps; step++) {
                const t = step / steps
                light.addColorStop(t, Qt.rgba(glow.tint.r, glow.tint.g, glow.tint.b,
                                              glow.strength * Math.pow(1 - t, 2.2)))
            }
            ctx.fillStyle = light
            ctx.fillRect(-unit, -unit, 2 * unit, 2 * unit)
            ctx.restore()
        }
    }
}
