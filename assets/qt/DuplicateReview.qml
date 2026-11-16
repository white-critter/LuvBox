import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Duplicates shown one group per card: which photos are the same, which one
// stays ("남김") and which are the copies to clear out ("사본": a photo before
// it in the group has the very same file). A photo found as the same picture
// at another size or quality is only "비슷함": it is not offered as a copy,
// because the Recycle Bin check keeps it unless a file with its bytes stays.
// Its "비교" opens it beside the photo kept, and a person can send it alone.
// The cards come from a model that changes in place, so after a group is put
// away the list stays where it was.
Item {
    id: review
    property var app: null
    property var galleryModel: null
    property int tileWidth: 168
    signal previewRequested(string key)

    readonly property var library: app ? app.library : null
    // While the list is read again after a change, the cards on screen are the earlier ones.
    readonly property bool settling: !!library && library.duplicatesSettling
    readonly property int tileHeight: Math.round(tileWidth * 0.75) + 50
    // The photo the last single click was on, and whether it was picked before that click:
    // a double-click opens the photo large and takes that click's pick back.
    property string clickedKey: ""
    property bool clickedWasChosen: false

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
        model: review.library ? review.library.duplicateGroupModel : null
        ScrollBar.vertical: ScrollBar {
            policy: list.contentHeight > list.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        }
        SmoothWheel { flickable: list; notchPixels: 160 }
        onContentYChanged: review.requestMore()
        onCountChanged: Qt.callLater(review.requestMore)

        delegate: Card {
            id: card
            required property int index
            required property int group
            required property int number
            required property int size
            required property var items
            objectName: "duplicateGroup"
            readonly property var copies: {
                const keys = []
                for (let i = 0; i < items.length; i++) {
                    if (items[i].copy && !items[i].missing)
                        keys.push(items[i].key)
                }
                return keys
            }
            // The photos of this card the person picked.
            readonly property var chosenKeys: {
                const revision = review.library ? review.library.selectionRevision : -1
                const keys = []
                if (revision < 0)
                    return keys
                for (let i = 0; i < items.length; i++) {
                    if (review.library.isSelected(items[i].key))
                        keys.push(items[i].key)
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
                        text: "묶음 " + card.number + " · 같은 사진 " + card.size + "장"
                            + (card.items.length < card.size ? " (" + card.items.length + "장 표시)" : "")
                        font.bold: true
                        font.pixelSize: Theme.fontSection
                        color: Theme.text
                        elide: Text.ElideRight
                    }
                    FlatButton {
                        // Some photos of the group are other pictures: only they leave it, the rest stays a group.
                        objectName: "duplicateGroupPhotosDistinct"
                        compact: true
                        tone: "ghost"
                        visible: card.chosenKeys.length > 0 && card.chosenKeys.length < card.size
                        text: "선택한 " + card.chosenKeys.length + "장만 중복 아님"
                        enabled: !!review.library && !review.library.copiesLocked && !review.settling
                        tip: "고른 사진만 이 묶음에서 뺍니다. 나머지는 묶음으로 남고, 뺀 사진은 다음 검사부터 이 묶음과 묶지 않습니다. 위의 ‘되돌리기’나 ‘초기화’로 다시 볼 수 있습니다."
                        Accessible.name: "묶음 " + card.number + " " + text
                        onClicked: review.library.markPhotosDistinct(card.chosenKeys)
                    }
                    FlatButton {
                        // Not the same photo, or both are wanted: the group is remembered and not shown again.
                        objectName: "duplicateGroupDistinct"
                        compact: true
                        tone: "ghost"
                        text: "중복 아님"
                        enabled: !!review.library && !review.library.copiesLocked && !review.settling && card.items.length > 0
                        tip: "이 묶음을 중복이 아닌 것으로 기억합니다. 다음 검사부터 묶지 않고, 위의 ‘되돌리기’나 ‘초기화’로 다시 볼 수 있습니다."
                        Accessible.name: "묶음 " + card.number + " 중복 아님"
                        onClicked: review.library.markGroupDistinct(card.items[0].key)
                    }
                    FlatButton {
                        objectName: "duplicateGroupSelect"
                        compact: true
                        text: card.copiesSelected ? "선택 해제" : "사본 " + card.copies.length + "장 선택"
                        enabled: card.copies.length > 0 && !!review.library && !review.library.copiesLocked && !review.settling
                        Accessible.name: "묶음 " + card.number + " " + text
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
                        model: card.items
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
                            color: chosen ? Theme.accentSoft
                                : (tileMouse.containsMouse || roleHover.hovered ? Theme.raised : Theme.well)
                            border.width: chosen ? 1 : 0
                            border.color: Theme.accent
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: (tile.modelData.keeper ? "남길 사진 " : tile.modelData.copy ? "사본 " : "비슷한 사진 ")
                                + tile.modelData.caption
                            Accessible.description: roleBadge.tip
                            Accessible.checkable: true
                            Accessible.checked: chosen
                            // Picked without a mouse too: a screen reader toggles or presses the tile.
                            Accessible.onToggleAction: tileMouse.pick()
                            Accessible.onPressAction: tileMouse.pick()

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
                                id: roleBadge
                                objectName: "duplicateRole"
                                // Only a photo that is neither kept nor a copy says more: why it is not offered.
                                readonly property string tip: tile.modelData.keeper || tile.modelData.copy ? ""
                                    : "크기나 화질만 다른 비슷한 사진입니다. 남길 사진과 내용이 완전히 같지 않아 사본으로 고르지 않습니다. "
                                      + "필요 없으면 ‘비교’를 눌러 남길 사진과 나란히 본 뒤 이 사진만 휴지통으로 보낼 수 있습니다."
                                anchors.left: parent.left
                                anchors.top: parent.top
                                anchors.margins: 12
                                width: roleLabel.implicitWidth + 14
                                height: 22
                                radius: height / 2
                                color: tile.modelData.keeper ? Theme.keepFill
                                    : tile.modelData.copy ? Theme.accentLine : Theme.lineStrong
                                ToolTip.visible: roleHover.hovered && tip.length > 0
                                ToolTip.delay: 500
                                ToolTip.text: tip
                                Label {
                                    id: roleLabel
                                    objectName: "duplicateRoleLabel"
                                    anchors.centerIn: parent
                                    text: tile.modelData.keeper ? "남김" : tile.modelData.copy ? "사본" : "비슷함"
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
                                ToolTip.text: "누르면 선택 (휴지통으로 보내거나, 위의 ‘선택한 것만 중복 아님’) · 두 번 누르면 크게 보기"
                                function pick() {
                                    if (review.library && !review.settling)
                                        review.library.toggleSelection(tile.modelData.key)
                                }
                                onClicked: {
                                    review.clickedKey = tile.modelData.key
                                    review.clickedWasChosen = tile.chosen
                                    pick()
                                }
                                onDoubleClicked: {
                                    // The first click of the two picked or let go of the photo; looking
                                    // at it large leaves it as it was before.
                                    const key = tile.modelData.key
                                    if (review.clickedKey === key && review.library
                                            && review.library.isSelected(key) !== review.clickedWasChosen)
                                        pick()
                                    review.clickedKey = ""
                                    review.previewRequested(key)
                                }
                            }
                            // The pointer on the badge of a similar photo. The tile's mouse area takes the hover
                            // from what lies under it, so this sits above it. It takes no clicks: they still pick
                            // the tile.
                            Item {
                                anchors.fill: roleBadge
                                visible: roleBadge.tip.length > 0
                                HoverHandler {
                                    id: roleHover
                                    cursorShape: Qt.PointingHandCursor
                                }
                            }
                            // A similar photo is never a copy; this opens it beside the photo kept, to send it
                            // alone. Above the tile's mouse area, so its click does not pick the tile.
                            Rectangle {
                                id: compare
                                objectName: "duplicateCompare"
                                readonly property bool available: !!review.app && !review.settling && !review.app.busy
                                    && !review.library.filesLocked && !review.app.management.mutating
                                anchors.left: roleBadge.right
                                anchors.leftMargin: Theme.gapTiny
                                anchors.verticalCenter: roleBadge.verticalCenter
                                visible: !tile.modelData.keeper && !tile.modelData.copy && !tile.modelData.missing
                                width: compareLabel.implicitWidth + 16
                                height: 22
                                radius: height / 2
                                color: compareMouse.containsMouse && available ? Theme.accent : Theme.scrim
                                border.width: 1
                                border.color: Theme.accentLine
                                opacity: available ? 1 : 0.5
                                Accessible.role: Accessible.Button
                                Accessible.name: "묶음 " + card.number + " 비슷한 사진 " + tile.modelData.caption
                                    + " 남길 사진과 비교 후 휴지통으로"
                                Accessible.onPressAction: compareMouse.open()
                                Label {
                                    id: compareLabel
                                    anchors.centerIn: parent
                                    text: "비교"
                                    font.pixelSize: Theme.fontTiny
                                    font.bold: true
                                    color: compareMouse.containsMouse && compare.available ? "white" : Theme.accentText
                                }
                                MouseArea {
                                    id: compareMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: compare.available ? Qt.PointingHandCursor : Qt.ArrowCursor
                                    ToolTip.visible: containsMouse
                                    ToolTip.delay: 500
                                    ToolTip.text: compare.available
                                        ? "남길 사진과 나란히 비교한 뒤 이 사진만 휴지통으로 보낼 수 있습니다"
                                        : "작업이 끝난 뒤 비교할 수 있습니다"
                                    function open() {
                                        if (compare.available && compare.visible)
                                            review.app.management.prepareAlikeDelete(tile.modelData.key)
                                    }
                                    onClicked: open()
                                    // A double-click here is two presses of this button, not "look at it large".
                                    onDoubleClicked: {}
                                }
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
        visible: list.count === 0 && !review.settling && (!review.galleryModel || !review.galleryModel.busy)
        text: review.library && review.library.copiesLocked
            ? "같은 사진을 찾고 있습니다. 끝나면 묶음으로 보여 드립니다."
            : "찾은 중복 사진이 없습니다. 위의 ‘중복 검사’를 눌러 확인해 보세요."
              + (review.library && review.library.distinctDuplicateCount > 0
                 ? "\n‘중복 아님’ 표시 " + review.library.distinctDuplicateCount.toLocaleString()
                   + "개는 위의 ‘초기화’로 다시 볼 수 있습니다." : "")
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        color: Theme.textMuted
    }
}
