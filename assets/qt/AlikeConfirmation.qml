import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One photo the duplicate view shows only as alike ('비슷함': the same picture at
// another size or quality, other bytes), beside the photo its group keeps.
// No copy check can let such a photo go, so a person looks at the two and
// sends this one alone; the move checks again that the photo kept is still
// there and still the same picture (deletion.kept_alike).
FlatDialog {
    id: dialog
    property var app: null
    objectName: "alikeConfirmation"
    parent: Overlay.overlay
    title: "이 사진을 휴지통으로 보낼까요?"
    preferredWidth: 1000
    height: parent ? Math.max(320, Math.min(760, parent.height - windowMargin)) : 640
    readonly property var shown: app ? app.management.deleteAlike : ({})
    // Let go of both pictures before the move: Windows keeps a file that is being read.
    property bool released: false
    onAboutToShow: released = false
    // The switch starts on: fetched again at the next full save, this photo would be grouped
    // with the one kept all over again.
    onOpened: { keepDeleted.checked = true; cancelButton.forceActiveFocus() }
    onRejected: {
        released = true
        if (app) app.management.cancelDelete()
    }
    onAccepted: {
        released = true
        let keep = keepDeleted.checked
        Qt.callLater(function() { if (dialog.app) dialog.app.management.confirmDelete(keep) })
    }

    component Side: ColumnLayout {
        id: side
        property var photo: ({})
        property bool keeper: false
        spacing: Theme.gapSmall
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapSmall
            Rectangle {
                objectName: side.keeper ? "alikeKeeperBadge" : "alikeTargetBadge"
                implicitWidth: sideBadge.implicitWidth + 14
                implicitHeight: 22
                radius: height / 2
                color: side.keeper ? Theme.keepFill : Theme.lineStrong
                Label {
                    id: sideBadge
                    anchors.centerIn: parent
                    text: side.keeper ? "남김" : "비슷함"
                    font.pixelSize: Theme.fontTiny
                    font.bold: true
                    color: "white"
                }
            }
            Label {
                Layout.fillWidth: true
                text: side.keeper ? "남길 사진" : "휴지통으로 보낼 사진"
                font.bold: true
                color: Theme.text
                elide: Text.ElideRight
            }
            Label {
                visible: !!side.photo.favorite
                text: "★"
                color: Theme.notice
                font.pixelSize: Theme.fontSection
            }
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 120
            radius: Theme.radiusCard
            color: Theme.well
            // The one that leaves is outlined.
            border.width: side.keeper ? 0 : 1
            border.color: Theme.accentLine
            Image {
                id: sidePicture
                objectName: side.keeper ? "alikeKeeperPicture" : "alikeTargetPicture"
                anchors.fill: parent
                anchors.margins: Theme.gapSmall
                source: dialog.released || !dialog.visible ? "" : (side.photo.url || "")
                // Both drawn at the size of the box, so the two read at one scale.
                sourceSize.width: Math.min(2048, Math.max(1, Math.round(width * Math.max(1, Screen.devicePixelRatio))))
                sourceSize.height: Math.min(2048, Math.max(1, Math.round(height * Math.max(1, Screen.devicePixelRatio))))
                fillMode: Image.PreserveAspectFit
                autoTransform: true
                asynchronous: true
                cache: false
                smooth: true
                mipmap: true
            }
            Label {
                anchors.centerIn: parent
                visible: sidePicture.status !== Image.Ready
                text: sidePicture.status === Image.Error ? "미리보기 없음" : "불러오는 중…"
                color: Theme.textFaint
                font.pixelSize: Theme.fontCaption
            }
        }
        Label {
            objectName: side.keeper ? "alikeKeeperSize" : "alikeTargetSize"
            Layout.fillWidth: true
            text: [side.photo.pixels || "", side.photo.bytes || ""].filter(part => part.length > 0).join(" · ")
            textFormat: Text.PlainText
            color: Theme.text
            font.pixelSize: Theme.fontSmall
            elide: Text.ElideRight
        }
        Label {
            Layout.fillWidth: true
            visible: text.length > 0
            text: side.photo.caption || ""
            textFormat: Text.PlainText
            color: Theme.textMuted
            font.pixelSize: Theme.fontCaption
            elide: Text.ElideRight
        }
        Label {
            Layout.fillWidth: true
            text: side.photo.name || ""
            textFormat: Text.PlainText
            color: Theme.textFaint
            font.pixelSize: Theme.fontTiny
            elide: Text.ElideMiddle
        }
    }

    contentItem: ColumnLayout {
        spacing: Theme.gap
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.gapLarge
            Side {
                // Equal halves: the same preferred width, both filling.
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                keeper: true
                photo: dialog.shown.keeper || ({})
            }
            Side {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                photo: dialog.shown.alike || ({})
            }
        }
        Label {
            objectName: "alikeSummaryText"
            Layout.fillWidth: true
            text: dialog.app ? dialog.app.management.deleteSummary : ""
            color: Theme.textMuted
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        FlatSwitch {
            id: keepDeleted
            Layout.fillWidth: true
            trailing: true
            text: "다음 저장에서 다시 받지 않기"
            Accessible.name: text
        }
    }
    footer: DialogButtons {
        FlatButton {
            objectName: "confirmAlikeRecycle"
            text: "이 사진 휴지통으로"
            tone: "primary"
            Accessible.name: "‘비슷함’ 사진 한 장을 휴지통으로"
            DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
        }
        FlatButton {
            id: cancelButton
            objectName: "cancelAlikeRecycle"
            text: "취소"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
    }
}
