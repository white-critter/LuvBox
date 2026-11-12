import QtQuick

// Mouse-wheel scrolling for one list or scrolling page. Qt's own step moves a
// few dozen pixels a notch, which felt slow everywhere; this moves a fixed,
// larger step and speeds up while the wheel keeps turning the same way.
WheelHandler {
    id: handler
    required property Flickable flickable
    // One notch at Windows' default of three lines.
    property real notchPixels: 120
    property real maximumGain: 4
    // A popup list keeps the wheel at its ends, so the page behind does not move.
    property bool passAtEnds: true
    readonly property int wheelLines: Application.styleHints.wheelScrollLines
    property int burst: 0
    property int direction: 0
    property double lastInput: 0
    readonly property bool motionActive: motion.running

    acceptedDevices: PointerDevice.Mouse
    target: null

    property NumberAnimation motion: NumberAnimation {
        target: handler.flickable
        property: "contentY"
        duration: 130
        easing.type: Easing.OutCubic
    }

    function reset() {
        if (motion.running)
            motion.stop()
        burst = 0
        direction = 0
        lastInput = 0
    }

    onWheel: event => {
        const view = handler.flickable
        const angle = event.angleDelta.y
        if (!view || !angle || event.angleDelta.x !== 0 || event.modifiers !== Qt.NoModifier
                || handler.wheelLines === 0) {
            event.accepted = false
            return
        }
        const minimum = view.originY
        const maximum = minimum + Math.max(0, view.contentHeight - view.height)
        const from = motion.running ? motion.to : view.contentY
        const sign = angle > 0 ? -1 : 1
        // At an end, or with nothing to scroll, the wheel belongs to the page behind.
        if (maximum - minimum < 1 || (sign < 0 && from <= minimum + 0.5) || (sign > 0 && from >= maximum - 0.5)) {
            handler.reset()
            event.accepted = !handler.passAtEnds
            return
        }
        const now = Date.now()
        if (sign !== handler.direction || now - handler.lastInput >= 200 || now < handler.lastInput)
            handler.burst = 0
        handler.direction = sign
        handler.lastInput = now
        handler.burst += 1
        const page = handler.wheelLines < 0 || handler.wheelLines >= 1000
        const gain = page ? 1 : Math.min(handler.maximumGain, 1 + (handler.burst - 1) * 0.5)
        const distance = page ? sign * view.height
            : -angle / 120 * handler.notchPixels * (handler.wheelLines / 3) * gain
        const destination = Math.max(minimum, Math.min(maximum, from + distance))
        view.cancelFlick()
        motion.stop()
        motion.to = destination
        motion.start()
        event.accepted = true
    }
}
