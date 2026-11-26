import QtQuick

// Shared zoom and audio keyboard shortcuts for MediaPreview dialogs.
Item {
    id: root
    required property MediaPreview preview
    property bool shortcutsEnabled: false
    visible: false

    Shortcut {
        sequence: "+"
        enabled: root.shortcutsEnabled && root.preview.canZoomIn
        onActivated: root.preview.zoomIn()
    }
    Shortcut {
        sequence: "="
        enabled: root.shortcutsEnabled && root.preview.canZoomIn
        onActivated: root.preview.zoomIn()
    }
    Shortcut {
        sequence: "-"
        enabled: root.shortcutsEnabled && root.preview.canZoomOut
        onActivated: root.preview.zoomOut()
    }
    Shortcut {
        sequence: "0"
        enabled: root.shortcutsEnabled && root.preview.ready
        onActivated: root.preview.fit()
    }
    Shortcut {
        sequence: "1"
        enabled: root.shortcutsEnabled && root.preview.canActualSize
        onActivated: root.preview.actualSize()
    }
    Shortcut {
        sequence: "m"
        enabled: root.shortcutsEnabled && root.preview.mediaType === "video"
            && root.preview.videoSource && root.preview.ready
        onActivated: root.preview.muted = !root.preview.muted
    }
    Shortcut {
        sequence: "Shift+M"
        enabled: root.shortcutsEnabled && root.preview.mediaType === "video"
            && root.preview.videoSource && root.preview.ready
        onActivated: root.preview.muted = !root.preview.muted
    }
}
