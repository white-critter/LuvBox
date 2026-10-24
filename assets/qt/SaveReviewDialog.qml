import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    required property var app
    property bool _responded: false
    readonly property var item: app ? app.reviewItem : ({})
    title: "이 사진을 저장할까요?"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(880, parent ? Math.max(240, parent.width - 32) : 880)
    height: Math.min(720, parent ? Math.max(200, parent.height - 32) : 720)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 20

    function submit(action) {
        if (_responded)
            return
        _responded = true
        media.release()
        app.respondReview(action)
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
            Layout.leftMargin: 20
            Layout.topMargin: 12
            Layout.bottomMargin: 8
            text: dialog.title
            textFormat: Text.PlainText
            font.bold: true
        }
        ToolButton {
            Layout.rightMargin: 12
            text: "×"
            Accessible.name: "저장을 중단하고 닫기"
            onClicked: dialog.submit("stop")
        }
    }

    contentItem: ColumnLayout {
        spacing: 12

        MediaPreview {
            id: media
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 80
            sourceUrl: dialog.item ? (dialog.item.url || "") : ""
            mediaType: dialog.item ? (dialog.item.media_type || "photo") : "photo"
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

            ToolButton {
                id: moreButton
                text: "더보기"
                Accessible.name: "추가 저장 선택"
                onClicked: moreMenu.open()

                Menu {
                    id: moreMenu
                    y: -height
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
            Button {
                text: "건너뛰기"
                onClicked: dialog.submit("skip")
            }
            Button {
                id: saveButton
                text: "저장"
                highlighted: true
                onClicked: dialog.submit("save")
            }
        }
    }
}
