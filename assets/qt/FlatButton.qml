import QtQuick
import QtQuick.Controls

// The app's button. `tone` sets how much it stands out:
//   primary  the single main action of a screen or dialog
//   quiet    ordinary actions (the default)
//   ghost    actions that should recede: close, cancel, a link beside text
//   danger   a destructive action that sits among other actions
//   nav      the screen tabs in the title bar
//   chip     a filter that is on (checked, or `chosen`) or off
//   soft     an action worth noticing on a screen whose main action is another one
Button {
    id: control
    property string tone: "quiet"
    // The full-width main action of a page is taller than buttons in a row.
    property bool large: false
    // A button inside a list row or a status line is smaller than one in a row of its own.
    property bool compact: false
    // A chip whose filter is applied without the button itself being checkable.
    property bool chosen: false
    // False for a chip that only opens something: being open is not a filter being on.
    property bool checkLights: true
    readonly property bool lit: tone === "soft" || (tone === "chip" && ((checked && checkLights) || chosen))
    // A button that opens a menu or a panel carries the same arrow as a list;
    // the arrow points up while what it opened is showing.
    property bool dropdown: false
    property bool dropdownOpen: false
    // A line shown after the pointer rests on the button. It leaves when the
    // button is pressed and stays away until the pointer comes back, so it never
    // describes the state from before the click.
    property string tip: ""
    property bool tipSpent: false
    flat: true
    implicitHeight: large ? Theme.largeHeight : (compact ? Theme.compactHeight : Theme.controlHeight)
    leftPadding: compact ? 12 : (tone === "chip" ? 14 : 16)
    rightPadding: dropdown ? leftPadding + 18 : leftPadding
    font.bold: tone === "primary" || (tone === "nav" && checked)
    font.pixelSize: large ? Theme.fontSection : (compact ? Theme.fontSmall : Theme.fontBody)
    // Only a press under the pointer spends the tip; a key press must not hide the next one.
    onPressed: if (pointer.hovered) tipSpent = true
    ToolTip.visible: tip.length > 0 && pointer.hovered && !tipSpent
    ToolTip.delay: 500
    ToolTip.text: tip
    HoverHandler {
        id: pointer
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onHoveredChanged: if (!hovered) control.tipSpent = false
    }
    background: Rectangle {
        id: face
        readonly property bool primary: control.tone === "primary"
        radius: control.tone === "chip" ? height / 2 : Theme.radiusControl
        border.width: control.visualFocus ? 2 : (control.tone === "chip" || control.tone === "soft" ? 1 : 0)
        border.color: control.visualFocus ? Theme.focus : (control.lit ? Theme.accentLine : Theme.line)
        // The main action is lit from above, like the heart on the startup screen.
        gradient: primary && control.enabled && !control.down ? primaryLight : null
        color: {
            if (control.tone === "primary") {
                if (!control.enabled)
                    return Theme.accentDisabled
                return control.down ? Theme.accentPressed : Theme.accent
            }
            if (control.tone === "chip" || control.tone === "soft") {
                if (!control.enabled)
                    return Theme.fieldDisabled
                if (control.lit)
                    return control.hovered ? Theme.accentSoftHover : Theme.accentSoft
                return control.down || control.hovered ? Theme.raised : Theme.field
            }
            if (control.tone === "nav") {
                if (control.down)
                    return Theme.raised
                return control.hovered && control.enabled && !control.checked ? Theme.hover : "transparent"
            }
            if (control.tone === "ghost" || control.tone === "danger") {
                if (!control.enabled)
                    return "transparent"
                if (control.down)
                    return Theme.raisedPressed
                return control.hovered ? Theme.hover : "transparent"
            }
            if (!control.enabled)
                return Theme.fieldDisabled
            if (control.down)
                return Theme.raisedPressed
            return control.hovered ? Theme.raisedHover : Theme.raised
        }
        Gradient {
            id: primaryLight
            GradientStop { position: 0; color: control.hovered ? Qt.lighter(Theme.accentTop, 1.08) : Theme.accentTop }
            GradientStop { position: 1; color: control.hovered ? Theme.accentHover : Theme.accent }
        }
        // Two soft rings of red light around the main action and around a filter that is on.
        Repeater {
            model: (face.primary || control.lit) && control.enabled ? 2 : 0
            Rectangle {
                required property int index
                z: -1
                anchors.fill: parent
                anchors.margins: -(index + 1) * 3
                radius: face.radius + (index + 1) * 3
                color: "transparent"
                border.width: 3
                border.color: Qt.alpha(Theme.glow, (face.primary ? 0.16 : 0.09) / (index + 1))
            }
        }
        // The lit edge along the top of the main action.
        Rectangle {
            visible: face.primary && control.enabled && !control.down
            x: face.radius
            y: 1
            width: face.width - 2 * face.radius
            height: 1
            color: Qt.rgba(1, 1, 1, 0.22)
        }
        // The tab of the screen being shown: a short red light under its name.
        Rectangle {
            visible: control.tone === "nav" && control.checked
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 2
            width: Math.max(16, parent.width - 28)
            height: 2
            radius: height / 2
            color: Theme.accent
        }
    }
    Canvas {
        id: arrow
        visible: control.dropdown
        width: 10
        height: 6
        x: control.width - width - 14
        y: (control.height - height) / 2
        rotation: control.dropdownOpen ? 180 : 0
        readonly property color stroke: !control.enabled ? Theme.textDisabled
            : (control.lit ? Theme.accentText : Theme.textMuted)
        onStrokeChanged: requestPaint()
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.moveTo(0, 1)
            ctx.lineTo(width, 1)
            ctx.lineTo(width / 2, height)
            ctx.closePath()
            ctx.fillStyle = stroke
            ctx.fill()
        }
    }
    contentItem: Text {
        text: control.text
        font: control.font
        color: {
            if (!control.enabled)
                return Theme.textDisabled
            if (control.tone === "primary")
                return "white"
            if (control.tone === "danger")
                return Theme.error
            if (control.lit)
                return Theme.accentText
            if ((control.tone === "nav" && !control.checked) || control.tone === "ghost")
                return Theme.textMuted
            return Theme.text
        }
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
