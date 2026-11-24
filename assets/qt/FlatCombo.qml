pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Window

ComboBox {
    id: control
    readonly property var hostWindow: Window.window
    readonly property Item windowFocusItem: Window.activeFocusItem
    property int minimumPopupWidth: 0
    // A dim word in front of the choice ("정렬  최근 저장"), for lists whose
    // choices do not say what they are a choice of.
    property string label: ""
    // A filter chip: pill-shaped, and lit while `chosen`.
    property bool chip: false
    property bool chosen: false
    readonly property real labelWidth: label.length ? prefix.implicitWidth + 8 : 0
    // Esc on the closed list hands the keys to this item (the gallery's lists: to the grid).
    property Item escapeFocus: null
    // Letters typed quickly one after another find a row together ("bo" finds "@bob"), and
    // the "@" or "#" in front of a row's name does not count. A closed list opens on the row
    // it found: only Enter or a click applies a choice, typing alone never does.
    property bool typeAhead: false
    property string typedText: ""
    property double typedAt: 0
    // The item that had the keys when the mouse opened the list (see the popup).
    property bool keysBorrowed: false
    property Item keysBefore: null
    // While the highlight is stepped to a typed row, the open list waits for the last step.
    property bool stepping: false
    implicitHeight: chip ? Theme.controlHeight : Theme.inputHeight
    leftPadding: 14
    rightPadding: 30
    font.pixelSize: Theme.fontBody
    hoverEnabled: true
    // A closed list must keep its choice when the pointer wheel passes over it.
    wheelEnabled: false
    // Measures the widest choice, so the list fits its labels and no more.
    implicitContentWidthPolicy: ComboBox.ContentItemImplicitWidth
    HoverHandler {
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    // A row's text for type-ahead: without the "@" or "#" in front, in lower case.
    function bareText(index) {
        return String(textAt(index)).replace(/^[@#]+/, "").toLowerCase()
    }
    // The first row from `from` on (wrapping around) whose text starts with `typed`.
    function typedRow(typed, from) {
        for (let step = 0; step < count; step++) {
            const index = (from + step) % count
            if (bareText(index).startsWith(typed))
                return index
        }
        return -1
    }
    function inside(item, ancestor) {
        for (let node = item; node; node = node.parent) {
            if (node === ancestor)
                return true
        }
        return false
    }
    // Moves the open list's highlight, which only the list's own steps can do.
    function highlightRow(index) {
        if (!popup.visible || index < 0 || index >= count)
            return
        let steps = count
        stepping = true
        while (highlightedIndex < index && steps-- > 0)
            incrementCurrentIndex()
        while (highlightedIndex > index && steps-- > 0)
            decrementCurrentIndex()
        stepping = false
    }
    Keys.onPressed: event => {
        if (!control.typeAhead || control.editable)
            return
        const letter = event.text
        if (letter.length !== 1 || letter <= " " || letter === "\u007f")
            return
        // Ctrl, Alt or Meta alone make a shortcut, not a letter (Ctrl with Alt is AltGr, which
        // types "@" on some keyboards).
        const held = event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
        if (held && held !== (Qt.ControlModifier | Qt.AltModifier))
            return
        // Taken here, so the list's own one-letter search never applies a row of a closed list.
        event.accepted = true
        const now = Date.now()
        const fresh = now - control.typedAt > 1000
        control.typedAt = now
        control.typedText = (fresh ? "" : control.typedText) + letter.toLowerCase()
        const typed = control.typedText.replace(/^[@#]+/, "")
        if (!typed.length)
            return
        const start = control.popup.visible ? control.highlightedIndex : control.currentIndex
        // The first letter of a word looks past the row it is on; more letters may keep that row.
        let found = control.typedRow(typed, typed.length === 1 ? start + 1 : Math.max(0, start))
        // The same letter again and again steps through the rows that start with it.
        if (found < 0 && typed.length > 1 && typed.split("").every(c => c === typed[0]))
            found = control.typedRow(typed[0], start + 1)
        if (found < 0)
            return
        if (!control.popup.visible)
            control.popup.open()
        control.highlightRow(found)
    }
    Keys.onEscapePressed: event => {
        if (control.escapeFocus && !control.popup.visible) {
            control.escapeFocus.forceActiveFocus()
            event.accepted = true
        } else {
            event.accepted = false
        }
    }

    background: Rectangle {
        radius: control.chip ? height / 2 : Theme.radiusControl
        color: {
            if (!control.enabled)
                return Theme.fieldDisabled
            if (control.chip && control.chosen)
                return control.hovered ? Theme.accentSoftHover : Theme.accentSoft
            return control.chip && (control.hovered || control.popup.visible) ? Theme.raised : Theme.field
        }
        border.width: control.visualFocus ? 2 : 1
        border.color: {
            if (control.visualFocus)
                return Theme.focus
            if (control.chip && control.chosen)
                return Theme.accentLine
            return control.hovered && control.enabled && !control.chip ? Theme.lineStrong : Theme.line
        }
    }

    contentItem: Item {
        implicitWidth: control.labelWidth + value.implicitWidth
        Text {
            id: prefix
            visible: control.label.length > 0
            anchors.verticalCenter: parent.verticalCenter
            text: control.label
            textFormat: Text.PlainText
            font: control.font
            color: control.enabled ? Theme.textFaint : Theme.textDisabled
        }
        Text {
            id: value
            anchors.left: parent.left
            anchors.leftMargin: control.labelWidth
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: control.displayText
            textFormat: Text.PlainText
            font: control.font
            color: {
                if (!control.enabled)
                    return Theme.textDisabled
                return control.chip && control.chosen ? Theme.accentText : Theme.text
            }
            elide: Text.ElideRight
        }
    }

    indicator: Canvas {
        id: canvasIndicator
        width: 10
        height: 6
        x: control.width - width - 14
        y: (control.height - height) / 2
        readonly property color stroke: !control.enabled ? Theme.textDisabled
            : (control.chip && control.chosen ? Theme.accentText : Theme.textMuted)
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

    delegate: ItemDelegate {
        id: row
        required property var model
        required property int index
        width: ListView.view ? ListView.view.width : control.popup.availableWidth
        height: Theme.controlHeight
        text: {
            const value = model.modelData !== undefined ? model.modelData : model
            if (!control.textRole)
                return value
            return value && value[control.textRole] !== undefined ? value[control.textRole] : value
        }
        highlighted: control.highlightedIndex === index
        font.pixelSize: Theme.fontBody
        leftPadding: 12
        rightPadding: 12
        HoverHandler {
            cursorShape: Qt.PointingHandCursor
        }
        contentItem: Text {
            text: row.text
            textFormat: Text.PlainText
            font: row.font
            color: control.currentIndex === row.index ? Theme.accentText : Theme.text
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: Theme.radiusSmall
            color: row.highlighted || row.hovered ? Theme.raised : "transparent"
        }
    }

    popup: Popup {
        y: control.height + 6
        width: Math.min(control.hostWindow ? Math.max(160, control.hostWindow.width - 16) : 640,
            Math.max(control.width, control.minimumPopupWidth, control.implicitContentWidth + 32))
        // Stay inside the window, e.g. for a combo at its right edge.
        margins: 8
        padding: 4
        implicitHeight: Math.min(280, contentItem.implicitHeight + 8)
        // A list that does not take the keys on a click still takes them while the mouse has it
        // open, for the arrows, letters and Enter, and gives them back when it closes. A list
        // the keyboard reached keeps them.
        onAboutToShow: {
            if (!control.activeFocus) {
                let before = control.windowFocusItem
                // Keys on a picture of the grid go back to the grid itself: the choice may read
                // the list again and put that picture away.
                if (before && control.escapeFocus && control.inside(before, control.escapeFocus))
                    before = control.escapeFocus
                control.keysBefore = before
                control.keysBorrowed = true
                control.forceActiveFocus(Qt.PopupFocusReason)
            }
        }
        onClosed: {
            let back = control.keysBefore
            const borrowed = control.keysBorrowed
            control.keysBefore = null
            control.keysBorrowed = false
            // Typing after the list closed starts a new word.
            control.typedText = ""
            control.typedAt = 0
            if (!borrowed || !control.activeFocus)
                return
            while (back && !back.visible)
                back = back.parent
            if (back)
                back.forceActiveFocus(Qt.PopupFocusReason)
            if (control.activeFocus)
                control.focus = false
        }
        background: Rectangle {
            radius: Theme.radiusControl + 2
            color: Theme.field
            border.width: 1
            border.color: Theme.lineStrong
        }
        contentItem: ListView {
            id: popupList
            width: control.popup.availableWidth
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.stepping ? -1 : control.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds
            // The bar can be dragged. The wheel is accepted here, including at
            // the first and last row, so it does not fall through to a list behind.
            ScrollBar.vertical: ScrollBar {
                policy: popupList.contentHeight > popupList.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }
            SmoothWheel {
                flickable: popupList
                notchPixels: 108
                passAtEnds: false
            }
            WheelHandler {
                acceptedDevices: PointerDevice.TouchPad
                blocking: true
                onWheel: function(event) {
                    const step = event.pixelDelta.y
                        || (event.angleDelta.y ? Math.round(event.angleDelta.y / 120 * 108) : 0)
                    if (!step) {
                        event.accepted = false
                        return
                    }
                    const minimum = popupList.originY
                    const maximum = minimum + Math.max(0, popupList.contentHeight - popupList.height)
                    popupList.contentY = Math.max(minimum, Math.min(maximum, popupList.contentY - step))
                }
            }
        }
    }
}
