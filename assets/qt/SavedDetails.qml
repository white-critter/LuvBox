import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Frame {
    id: details
    required property var app
    readonly property var download: app ? app.download : null
    readonly property var item: download ? download.currentSavedItem : ({})
    readonly property bool hasItem: !!(item && item.filename)
    padding: 14
    background: Rectangle { color: "#191919"; radius: 12 }

    ColumnLayout {
        width: parent.width
        spacing: 10
        Label { text: "최근 저장한 파일"; font.bold: true }
        Label {
            Layout.fillWidth: true
            visible: !details.hasItem
            text: "파일을 저장하면 미리보기와 게시물 정보가 여기에 표시됩니다."
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: "#b9afb1"
        }
        RowLayout {
            Layout.fillWidth: true
            visible: details.hasItem
            spacing: 14
            Item {
                Layout.preferredWidth: Math.min(188, details.availableWidth * 0.36)
                Layout.preferredHeight: 132
                Image {
                    objectName: "savedMediaThumbnail"
                    anchors.fill: parent
                    source: details.item.preview_url || ""
                    sourceSize: Qt.size(384, 288)
                    asynchronous: true
                    cache: false
                    fillMode: Image.PreserveAspectFit
                    autoTransform: true
                    Accessible.name: "최근 저장한 파일의 첫 장면"
                }
                BusyIndicator {
                    anchors.centerIn: parent
                    running: !!details.item.preview_loading
                    visible: running
                }
                Column {
                    anchors.centerIn: parent
                    width: parent.width
                    visible: !details.item.preview_loading && !details.item.preview_url
                    spacing: 6
                    Canvas {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 36
                        height: 30
                        onPaint: {
                            const ctx = getContext("2d")
                            ctx.reset()
                            ctx.strokeStyle = "#8a8284"
                            ctx.lineWidth = 1.8
                            ctx.lineJoin = "round"
                            ctx.lineCap = "round"
                            ctx.strokeRect(3, 3, 30, 24)
                            ctx.beginPath()
                            ctx.arc(11, 11, 2.5, 0, Math.PI * 2)
                            ctx.stroke()
                            ctx.beginPath()
                            ctx.moveTo(6, 23)
                            ctx.lineTo(14, 15)
                            ctx.lineTo(20, 20)
                            ctx.lineTo(25, 16)
                            ctx.lineTo(30, 23)
                            ctx.stroke()
                            ctx.strokeStyle = "#d93645"
                            ctx.lineWidth = 2
                            ctx.beginPath()
                            ctx.moveTo(5, 5)
                            ctx.lineTo(31, 25)
                            ctx.stroke()
                        }
                    }
                    Label {
                        width: parent.width
                        text: "미리보기 없음"
                        color: "#b9afb1"
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                    }
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                spacing: 6
                Label {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: details.item.author ? "@" + details.item.author : ""
                    font.bold: true
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                }
                Label {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: details.item.display_name || ""
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                Label {
                    Layout.fillWidth: true
                    text: details.item.filename || ""
                    textFormat: Text.PlainText
                    elide: Text.ElideMiddle
                }
                Label {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: details.item.posted_at
                        ? details.item.posted_at.replace("T", " ").replace(/\+09:00$/, "").slice(0, 16) : ""
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#b9afb1"
                }
                Label {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: details.item.preview_note || ""
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#b9afb1"
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    FlatButton {
                        objectName: "savedPreviewRetry"
                        text: "미리보기 다시 불러오기"
                        visible: details.hasItem && !details.item.preview_loading && !details.item.preview_url
                        onClicked: if (details.download) details.download.retrySavedPreview()
                    }
                    FlatButton {
                        text: "원문 보기"
                        visible: !!details.item.source_url
                        onClicked: if (details.app) details.app.openSavedSource()
                    }
                }
            }
        }
        ScrollView {
            id: bodyScroll
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(120, savedBody.implicitHeight)
            visible: details.hasItem && !!details.item.body
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: savedBody.implicitHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            TextArea {
                id: savedBody
                objectName: "savedTweetBody"
                width: bodyScroll.availableWidth
                text: details.item.body || ""
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: "#f4f1f1"
                selectionColor: "#d93645"
                selectedTextColor: "white"
                font.pixelSize: 13
                background: null
            }
        }
    }
}
