import QtQuick

Item {
    id: controller
    required property Flickable view
    required property var inputFilter
    parent: view
    width: 0
    height: 0
    property int wheelLines: Application.styleHints.wheelScrollLines
    property real pixelsPerLine: 20
    readonly property bool motionActive: motion.running
    readonly property real multiplier: gain
    property real gain: 1
    property int burst: 0
    property int direction: 0
    property double lastInput: 0

    function reset() {
        if (motion.running)
            motion.stop()
        idle.stop()
        burst = 0
        direction = 0
        lastInput = 0
        gain = 1
    }

    onEnabledChanged: if (!enabled) reset()

    Binding { target: controller.inputFilter; property: "view"; value: controller.view }
    Binding { target: controller.inputFilter; property: "enabled"; value: controller.enabled }
    Connections {
        target: controller.inputFilter
        function onWheelRequested(angleY) { controller.advance(angleY) }
        function onResetRequested() { controller.reset() }
    }

    Timer {
        id: idle
        interval: 200
        onTriggered: controller.reset()
    }

    NumberAnimation {
        id: motion
        target: controller.view
        property: "contentY"
        duration: 110
        easing.type: Easing.OutCubic
    }

    function advance(angleY) {
        if (controller.wheelLines === 0) {
            controller.reset()
            return
        }
        let now = Date.now()
        let sign = angleY > 0 ? -1 : 1
        if (sign !== controller.direction || now - controller.lastInput >= 200 || now < controller.lastInput)
            controller.reset()
        let previousTarget = motion.running ? motion.to : controller.view.contentY
        controller.direction = sign
        controller.lastInput = now
        controller.burst += 1
        let page = controller.wheelLines < 0 || controller.wheelLines >= 1000
        controller.gain = page ? 1 : Math.min(3, 1 + (controller.burst - 1) * 0.4)
        let distance = page ? sign * controller.view.height :
            -angleY / 120 * controller.wheelLines * controller.pixelsPerLine * controller.gain
        let minimum = controller.view.originY
        let maximum = minimum + Math.max(0, controller.view.contentHeight - controller.view.height)
        let destination = Math.max(minimum, Math.min(maximum, previousTarget + distance))
        if (Math.abs(destination - controller.view.contentY) < 0.01) {
            controller.reset()
            return
        }
        controller.view.cancelFlick()
        motion.stop()
        motion.to = destination
        motion.start()
        idle.restart()
    }
}
