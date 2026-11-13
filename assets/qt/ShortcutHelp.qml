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
            { keys: "Ctrl+F", text: "사진 검색으로 이동" },
            { keys: "Enter · Space", text: "미리보기 열기 (여러 개 선택 중에는 선택·해제)" },
            { keys: "방향키", text: "사진 사이 이동" },
            { keys: "F", text: "즐겨찾기 추가·해제" },
            { keys: "B", text: "즐겨찾기만 보기 켜기·끄기" },
            { keys: "D", text: "중복 사진만 보기 켜기·끄기" },
            { keys: "S", text: "통계 요약 열기" },
            { keys: "Ctrl+A", text: "불러온 항목 모두 선택" },
            { keys: "Ctrl+클릭", text: "여러 개 선택에 추가·제외" },
            { keys: "Shift+클릭", text: "마지막으로 누른 사진부터 여기까지 선택" },
            { keys: "끌기", text: "사각형을 그려 닿은 사진 선택 (Ctrl을 누르면 추가)" },
            { keys: "마우스 휠", text: "스크롤 · 계속 굴리면 빨라짐" },
            { keys: "Delete", text: "선택한 사진을 휴지통으로 (확인 후 이동)" },
            { keys: "Esc", text: "선택 해제 · 검색어 지우기" },
            { keys: "F5", text: "새로고침 · 새 사진 보기" },
            { keys: "F1", text: "이 안내 열기" }
        ] },
        { title: "미리보기", rows: [
            { keys: "← · →", text: "이전 · 다음 사진" },
            { keys: "F", text: "즐겨찾기 추가·해제" },
            { keys: "+ · −", text: "확대 · 축소 (마우스 휠도 가능)" },
            { keys: "0 · 1", text: "화면 맞춤 · 원본 100%" },
            { keys: "M", text: "영상 소리 켜기·끄기" },
            { keys: "Delete", text: "휴지통으로 (이동 후 다음 사진으로 돌아옴)" },
            { keys: "Esc", text: "미리보기 닫기" }
        ] }
    ]

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
            rowSpacing: Theme.gapLarge
            Repeater {
                model: dialog.sections
                delegate: ColumnLayout {
                    id: section
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.alignment: Qt.AlignTop
                    spacing: 6
                    Label {
                        text: section.modelData.title
                        font.bold: true
                        font.pixelSize: Theme.fontSection
                        color: Theme.text
                    }
                    Repeater {
                        model: section.modelData.rows
                        delegate: RowLayout {
                            id: entry
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: Theme.gap
                            Accessible.role: Accessible.StaticText
                            Accessible.name: entry.modelData.keys + ": " + entry.modelData.text
                            Rectangle {
                                Layout.preferredWidth: 112
                                Layout.preferredHeight: 26
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
                                Layout.minimumHeight: 26
                            }
                        }
                    }
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
        }
        onRejected: dialog.close()
    }
}
