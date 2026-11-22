import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FlatDialog {
    id: dialog
    objectName: "shortcutHelp"
    title: "단축키"
    // Wide enough for the two lists side by side; a narrow window stacks them.
    readonly property bool wide: !!parent && parent.width - windowMargin >= 860
    preferredWidth: wide ? 900 : 520
    readonly property int windowLimit: parent ? Math.max(200, parent.height - 32) : 600
    readonly property int chromeHeight: topPadding + bottomPadding
        + (header && header.implicitHeight > 0 ? header.implicitHeight + spacing : 0)
        + (footer && footer.implicitHeight > 0 ? footer.implicitHeight + spacing : 0)
    height: Math.min(lists.implicitHeight + chromeHeight + 4, windowLimit)

    // Each key is listed where it works. Keep this in step with the Shortcut
    // items and key handlers in MainWindow.qml.
    readonly property var sections: [
        { title: "갤러리", rows: [
            { keys: "Ctrl+F", text: "사진 검색" },
            { keys: "Enter · Space", text: "미리보기 · 여러 개 선택 중에는 선택·해제" },
            { keys: "방향키", text: "사진 이동" },
            { keys: "F", text: "즐겨찾기 추가·해제" },
            { keys: "B", text: "즐겨찾기만 보기" },
            { keys: "D", text: "중복 사진만 보기" },
            { keys: "S", text: "통계 보기" },
            { keys: "Ctrl+A", text: "불러온 항목 모두 선택" },
            { keys: "Ctrl+클릭", text: "선택 추가·해제" },
            { keys: "Shift+클릭", text: "범위 선택" },
            { keys: "끌기", text: "사각형 선택 · Ctrl로 추가" },
            { keys: "마우스 휠", text: "스크롤" },
            { keys: "Delete", text: "선택한 사진을 휴지통으로" },
            { keys: "Esc", text: "선택 해제 · 선택 끝내기 · 검색어 지우기" },
            { keys: "F5", text: "새로고침 · 새 사진 보기" },
            { keys: "F1", text: "단축키 보기" }
        ] },
        { title: "폴더 정리", rows: [
            { keys: "Ctrl+F", text: "파일명 검색" },
            { keys: "Enter · Space", text: "미리보기 · 여러 개 선택 중에는 선택·해제" },
            { keys: "방향키", text: "사진 이동" },
            { keys: "F", text: "즐겨찾기 추가·해제" },
            { keys: "B", text: "즐겨찾기만 보기" },
            { keys: "D", text: "중복 사진만 보기" },
            { keys: "Ctrl+A", text: "불러온 사진 모두 선택" },
            { keys: "Ctrl+클릭", text: "선택 추가·해제" },
            { keys: "Shift+클릭", text: "범위 선택" },
            { keys: "끌기", text: "사각형 선택 · Ctrl로 추가" },
            { keys: "Delete", text: "선택한 사진을 휴지통으로" },
            { keys: "Esc", text: "선택 해제 · 선택 끝내기 · 검색어 지우기" },
            { keys: "F5", text: "새로고침" },
            { keys: "F1", text: "단축키 보기" }
        ] },
        { title: "미리보기", rows: [
            { keys: "← · →", text: "이전 · 다음 사진" },
            { keys: "F", text: "즐겨찾기 추가·해제" },
            { keys: "+ · − · 휠", text: "확대 · 축소" },
            { keys: "0 · 1", text: "화면 맞춤 · 원본 100%" },
            { keys: "M", text: "영상 소리 켜기·끄기" },
            { keys: "Delete", text: "휴지통으로" },
            { keys: "Esc", text: "미리보기 닫기" }
        ] }
    ]

    component SectionBox: ColumnLayout {
        id: section
        property var sectionData: ({ title: "", rows: [] })
        spacing: 4
        Label {
            text: section.sectionData.title || ""
            font.bold: true
            font.pixelSize: Theme.fontSection
            color: Theme.text
            textFormat: Text.PlainText
        }
        Repeater {
            model: section.sectionData.rows || []
            delegate: RowLayout {
                id: entry
                required property var modelData
                Layout.fillWidth: true
                spacing: Theme.gap
                Accessible.role: Accessible.StaticText
                Accessible.name: entry.modelData.keys + ": " + entry.modelData.text
                Rectangle {
                    Layout.preferredWidth: 112
                    Layout.preferredHeight: 24
                    Layout.alignment: Qt.AlignTop
                    radius: Theme.radiusSmall
                    color: Theme.field
                    border.width: 1
                    border.color: Theme.lineStrong
                    Label {
                        anchors.centerIn: parent
                        text: entry.modelData.keys
                        font.pixelSize: Theme.fontCaption
                        color: Theme.text
                        textFormat: Text.PlainText
                    }
                }
                Label {
                    Layout.fillWidth: true
                    text: entry.modelData.text
                    color: Theme.textMuted
                    wrapMode: Text.WordWrap
                    textFormat: Text.PlainText
                    verticalAlignment: Text.AlignVCenter
                    Layout.minimumHeight: 24
                }
            }
        }
    }

    function scrollBy(delta) {
        const flick = scroll.contentItem as Flickable
        if (!flick || flick.contentHeight <= flick.height)
            return false
        const maxY = Math.max(0, flick.contentHeight - flick.height)
        flick.contentY = Math.max(0, Math.min(maxY, flick.contentY + delta))
        return true
    }

    onOpened: closeButton.forceActiveFocus()
    contentItem: FocusScrollView {
        id: scroll
        clip: true
        contentWidth: availableWidth
        rightPadding: ScrollBar.vertical.policy === ScrollBar.AlwaysOn ? 14 : 0
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        GridLayout {
            id: lists
            width: scroll.availableWidth
            columns: dialog.wide ? 2 : 1
            columnSpacing: Theme.gapSection
            rowSpacing: Theme.gap
            SectionBox {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                sectionData: dialog.sections[0]
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                spacing: Theme.gap
                SectionBox {
                    Layout.fillWidth: true
                    sectionData: dialog.sections[1]
                }
                SectionBox {
                    Layout.fillWidth: true
                    sectionData: dialog.sections[2]
                }
            }
        }
    }
    footer: DialogButtons {
        FlatButton {
            id: closeButton
            objectName: "shortcutHelpClose"
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            Keys.onPressed: event => {
                if (event.key === Qt.Key_Down)
                    event.accepted = dialog.scrollBy(48)
                else if (event.key === Qt.Key_Up)
                    event.accepted = dialog.scrollBy(-48)
                else if (event.key === Qt.Key_PageDown)
                    event.accepted = dialog.scrollBy(Math.max(120, scroll.height - 48))
                else if (event.key === Qt.Key_PageUp)
                    event.accepted = dialog.scrollBy(-Math.max(120, scroll.height - 48))
                else if (event.key === Qt.Key_Home)
                    event.accepted = dialog.scrollBy(-scroll.contentHeight)
                else if (event.key === Qt.Key_End)
                    event.accepted = dialog.scrollBy(scroll.contentHeight)
            }
        }
        onRejected: dialog.close()
    }
}
