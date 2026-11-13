import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Duplicates shown one group per card: which photos are the same, which one
// stays ("남김") and which are the copies to clear out ("사본").
Item {
    id: review
    property var app: null
    property var galleryModel: null
    property int tileWidth: 168
    signal previewRequested(string key)

    readonly property var library: app ? app.library : null
    readonly property var groups: library ? library.duplicateGroups : []
    readonly property int tileHeight: Math.round(tileWidth * 0.75) + 50

    function requestMore() {
        if (galleryModel && list.contentY - list.originY + list.height + 600 >= list.contentHeight)
            galleryModel.loadMore()
    }

    ListView {
        id: list
        objectName: "duplicateGroupList"
        anchors.fill: parent
        clip: true
        spacing: 10
        boundsBehavior: Flickable.StopAtBounds
        model: review.groups
        ScrollBar.vertical: ScrollBar {
            policy: list.contentHeight > list.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        }
        SmoothWheel { flickable: list; notchPixels: 160 }
        onContentYChanged: review.requestMore()
        onCountChanged: Qt.callLater(review.requestMore)

        delegate: Card {
            id: card
            required property var modelData
            required property int index
            objectName: "duplicateGroup"
            readonly property var copies: {
                const keys = []
                for (let i = 0; i < modelData.items.length; i++) {
                    if (!modelData.items[i].keeper && !modelData.items[i].missing)
                        keys.push(modelData.items[i].key)
                }
                return keys
            }
            readonly property bool copiesSelected: {
                const revision = review.library ? review.library.selectionRevision : -1
                if (revision < 0 || copies.length === 0)
                    return false
                for (let i = 0; i < copies.length; i++) {
                    if (!review.library.isSelected(copies[i]))
                        return false
                }
                return true
            }
            width: list.width - 14
            implicitHeight: body.implicitHeight + 24
            height: implicitHeight

            ColumnLayout {
                id: body
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 12
                spacing: 10

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Label {
                        objectName: "duplicateGroupTitle"
                        Layout.fillWidth: true
                        text: "묶음 " + card.modelData.number + " · 같은 사진 " + card.modelData.size + "장"
                            + (card.modelData.items.length < card.modelData.size
                               ? " (" + card.modelData.items.length + "장 표시)" : "")
                        font.bold: true
                        font.pixelSize: Theme.fontSection
                        color: Theme.text
                        elide: Text.ElideRight
                    }
                    FlatButton {
                        objectName: "duplicateGroupSelect"
                        compact: true
                        text: card.copiesSelected ? "선택 해제" : "사본 " + card.copies.length + "장 선택"
                        enabled: card.copies.length > 0 && !!review.library && !review.library.copiesLocked
                        Accessible.name: "묶음 " + card.modelData.number + " " + text
                        onClicked: {
                            if (card.copiesSelected)
                                review.library.removeFromSelection(card.copies)
                            else
                                review.library.addToSelection(card.copies)
                        }
                    }
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Repeater {
                        model: card.modelData.items
                        delegate: Rectangle {
                            id: tile
                            required property var modelData
                            objectName: "duplicateTile"
                            readonly property bool chosen: {
                                const revision = review.library ? review.library.selectionRevision : -1
                                return revision >= 0 && review.library.isSelected(tile.modelData.key)
                            }
                            width: review.tileWidth
                            height: review.tileHeight
                            radius: Theme.radiusCard
                            color: chosen ? Theme.accentSoft : (tileMouse.containsMouse ? Theme.raised : Theme.well)
                            border.width: chosen ? 1 : 0
                            border.color: Theme.accent
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: (tile.modelData.keeper ? "남길 사진 " : "사본 ") + tile.modelData.caption
                            Accessible.checked: chosen

                            Image {
                                id: picture
                                anchors.top: parent.top
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.margins: 8
                                height: Math.round(review.tileWidth * 0.75) - 16
                                source: tile.modelData.missing ? "" : tile.modelData.thumbUrl
                                sourceSize.width: Math.min(384, Math.round(width * Math.max(1, Screen.devicePixelRatio)))
                                sourceSize.height: Math.min(384, Math.round(height * Math.max(1, Screen.devicePixelRatio)))
                                asynchronous: true
                                cache: false
                                fillMode: Image.PreserveAspectFit
                            }
                            Label {
                                anchors.centerIn: picture
                                visible: tile.modelData.missing || picture.status === Image.Error
                                text: tile.modelData.missing ? "파일 없음" : "미리보기 없음"
                                color: Theme.textFaint
                                font.pixelSize: Theme.fontTiny
                            }
                            // What to do with this one, in words and colour.
                            Rectangle {
                                objectName: "duplicateRole"
                                anchors.left: parent.left
                                anchors.top: parent.top
                                anchors.margins: 12
                                width: roleLabel.implicitWidth + 14
                                height: 22
                                radius: height / 2
                                color: tile.modelData.keeper ? Theme.keepFill : Theme.accentLine
                                Label {
                                    id: roleLabel
                                    anchors.centerIn: parent
                                    text: tile.modelData.keeper ? "남김" : "사본"
                                    font.pixelSize: Theme.fontTiny
                                    font.bold: true
                                    color: "white"
                                }
                            }
                            Label {
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 12
                                visible: tile.modelData.favorite
                                text: "★"
                                color: Theme.notice
                                font.pixelSize: Theme.fontSection
                            }
                            Rectangle {
                                anchors.right: parent.right
                                anchors.bottom: picture.bottom
                                anchors.margins: 4
                                width: 22
                                height: 22
                                radius: Theme.radiusSmall
                                visible: tile.chosen
                                color: Theme.accent
                                Label {
                                    anchors.centerIn: parent
                                    text: "✓"
                                    color: "white"
                                    font.bold: true
                                    font.pixelSize: Theme.fontSmall
                                }
                            }
                            Column {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.margins: 8
                                spacing: 1
                                Label {
                                    width: parent.width
                                    text: tile.modelData.caption
                                    textFormat: Text.PlainText
                                    elide: Text.ElideRight
                                    font.pixelSize: Theme.fontCaption
                                    color: Theme.textMuted
                                }
                                Label {
                                    width: parent.width
                                    text: tile.modelData.detail
                                    textFormat: Text.PlainText
                                    elide: Text.ElideRight
                                    font.pixelSize: Theme.fontTiny
                                    color: Theme.textFaint
                                }
                            }
                            MouseArea {
                                id: tileMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                ToolTip.visible: containsMouse
                                ToolTip.delay: 600
                                ToolTip.text: "누르면 휴지통으로 보낼 사진으로 선택 · 두 번 누르면 크게 보기"
                                onClicked: if (review.library) review.library.toggleSelection(tile.modelData.key)
                                onDoubleClicked: review.previewRequested(tile.modelData.key)
                            }
                        }
                    }
                }
            }
        }
    }

    Label {
        objectName: "duplicateEmptyState"
        anchors.centerIn: parent
        width: Math.max(0, parent.width - 32)
        visible: review.groups.length === 0 && (!review.galleryModel || !review.galleryModel.busy)
        text: review.library && review.library.copiesLocked
            ? "같은 사진을 찾고 있습니다. 끝나면 묶음으로 보여 드립니다."
            : "찾은 중복 사진이 없습니다. 위의 ‘중복 검사’를 눌러 확인해 보세요."
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        color: Theme.textMuted
    }
}
