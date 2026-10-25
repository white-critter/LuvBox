import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Dialog {
    id: dialog
    required property var app
    property bool _responded: false
    readonly property var item: app ? app.reviewItem : ({})
    readonly property alias previewMedia: media
    readonly property var hostWindow: parent ? parent.Window.window : null
    title: "이 사진을 저장할까요?"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: parent ? Math.max(0, parent.width - 24) : 1040
    height: parent ? Math.max(0, parent.height - 24) : 736
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 16
    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }

    function submit(action) {
        if (_responded)
            return
        _responded = true
        const reviewId = item ? item.id : ""
        const targetApp = app
        media.release()
        // Qt closes animated-image devices through deferred deletion. Let
        // that finish before the worker can rename or remove the source.
        Qt.callLater(function() {
            if (targetApp && targetApp.reviewItem && targetApp.reviewItem.id === reviewId)
                targetApp.respondReview(action)
        })
        close()
    }

    onOpened: {
        _responded = false
        saveButton.forceActiveFocus()
    }
    onRejected: submit("stop")
    onClosed: {
        media.release()
        moreMenu.close()
        if (!_responded && item && item.id)
            submit("stop")
    }

    header: RowLayout {
        spacing: 8
        Label {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.leftMargin: 20
            Layout.topMargin: 12
            Layout.bottomMargin: 8
            text: dialog.title
            textFormat: Text.PlainText
            font.bold: true
            elide: Text.ElideRight
        }
        PreviewZoomControls {
            visible: dialog.width >= 900
            preview: media
        }
        FlatButton {
            objectName: "reviewMaximizeButton"
            text: dialog.hostWindow && (dialog.hostWindow.visibility === Window.Maximized
                || dialog.hostWindow.visibility === Window.FullScreen) ? "창 복원" : "창 최대화"
            enabled: !!dialog.hostWindow
            onClicked: {
                if (dialog.hostWindow.visibility === Window.Maximized
                        || dialog.hostWindow.visibility === Window.FullScreen)
                    dialog.hostWindow.showNormal()
                else
                    dialog.hostWindow.showMaximized()
            }
        }
        FlatButton {
            Layout.rightMargin: 12
            text: "닫기"
            Accessible.name: "저장을 중단하고 닫기"
            onClicked: dialog.submit("stop")
        }
    }

    contentItem: ColumnLayout {
        spacing: 12

        PreviewZoomControls {
            visible: dialog.width < 900
            preview: media
        }

        MediaPreview {
            id: media
            objectName: "reviewMedia"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 80
            sourceUrl: dialog.item ? (dialog.item.url || "") : ""
            mediaType: dialog.item ? (dialog.item.media_type || "photo") : "photo"
            sourcePixelWidth: dialog.item ? (dialog.item.pixel_width || 0) : 0
            sourcePixelHeight: dialog.item ? (dialog.item.pixel_height || 0) : 0
            sourceRawWidth: dialog.item ? (dialog.item.source_width || 0) : 0
            sourceRawHeight: dialog.item ? (dialog.item.source_height || 0) : 0
            playing: dialog.visible && !dialog._responded
        }

        Label {
            Layout.fillWidth: true
            text: dialog.item ? (dialog.item.filename || "사진") : "사진"
            textFormat: Text.PlainText
            elide: Text.ElideMiddle
            Accessible.name: text
        }
        Label {
            Layout.fillWidth: true
            visible: text.length > 0
            text: dialog.item && dialog.item.author ? "@" + String(dialog.item.author).replace(/^@/, "") : ""
            textFormat: Text.PlainText
            elide: Text.ElideRight
            opacity: 0.72
        }
    }

    footer: Item {
        implicitHeight: buttons.implicitHeight + 24

        RowLayout {
            id: buttons
            anchors.fill: parent
            anchors.margins: 12
            spacing: 8

            FlatButton {
                id: moreButton
                text: "더보기"
                Accessible.name: "추가 저장 선택"
                onClicked: moreMenu.open()

                Menu {
                    id: moreMenu
                    y: -height
                    background: Rectangle {
                        radius: 10
                        color: "#1c1c1c"
                        border.width: 1
                        border.color: "#2e2e2e"
                    }
                    MenuItem {
                        text: "이 사진 다시 받지 않기"
                        onTriggered: dialog.submit("never")
                    }
                    MenuItem {
                        text: "이후 모두 자동 저장"
                        onTriggered: dialog.submit("all")
                    }
                }
            }
            Item { Layout.fillWidth: true }
            FlatButton {
                text: "건너뛰기"
                onClicked: dialog.submit("skip")
            }
            FlatButton {
                id: saveButton
                text: "저장"
                tone: "primary"
                onClicked: dialog.submit("save")
            }
        }
    }
}
