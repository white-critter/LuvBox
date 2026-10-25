import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Frame {
    id: details
    required property var app
    readonly property var download: app ? app.download : null
    readonly property var item: download ? download.currentSavedItem : ({})
    readonly property bool hasItem: !!(item && item.filename)
    padding: 16
    background: Rectangle { color: "#191919"; radius: 12 }

    ColumnLayout {
        width: parent.width
        spacing: 12
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
            spacing: 16
            Item {
                Layout.preferredWidth: Math.min(240, details.availableWidth * 0.42)
                Layout.preferredHeight: 180
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
                Label {
                    anchors.centerIn: parent
                    width: parent.width
                    visible: !details.item.preview_loading && !details.item.preview_url
                    text: "미리보기를 표시하지 못했습니다."
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                spacing: 8
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
                    text: details.item.preview_note || ""
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#b9afb1"
                }
                FlatButton {
                    text: "원문 보기"
                    visible: !!details.item.source_url
                    onClicked: if (details.app) details.app.openSavedSource()
                }
            }
        }
        ScrollView {
            id: bodyScroll
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(160, savedBody.implicitHeight)
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
                background: null
            }
        }
    }
}
