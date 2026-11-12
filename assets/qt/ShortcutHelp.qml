import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "shortcutHelp"
    title: "단축키"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(520, parent ? Math.max(240, parent.width - 32) : 520)
    height: Math.min(600, parent ? Math.max(200, parent.height - 32) : 600)
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    padding: 20

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

    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }
    header: Label {
        text: dialog.title
        leftPadding: 20
        rightPadding: 20
        topPadding: 16
        bottomPadding: 2
        font.pixelSize: 18
    }
    onOpened: closeButton.forceActiveFocus()
    contentItem: FocusScrollView {
        id: scroll
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > height + 2 ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        ColumnLayout {
            width: scroll.availableWidth - 12
            spacing: 14
            Repeater {
                model: dialog.sections
                delegate: ColumnLayout {
                    id: section
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 6
                    Label {
                        text: section.modelData.title
                        font.bold: true
                        color: "#f4f1f1"
                    }
                    Repeater {
                        model: section.modelData.rows
                        delegate: RowLayout {
                            id: entry
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 12
                            Accessible.role: Accessible.StaticText
                            Accessible.name: entry.modelData.keys + ": " + entry.modelData.text
                            Rectangle {
                                Layout.preferredWidth: 112
                                Layout.preferredHeight: 26
                                Layout.alignment: Qt.AlignTop
                                radius: 6
                                color: "#222222"
                                border.width: 1
                                border.color: "#353535"
                                Label {
                                    anchors.centerIn: parent
                                    text: entry.modelData.keys
                                    font.pixelSize: 12
                                    color: "#f4f1f1"
                                    textFormat: Text.PlainText
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: entry.modelData.text
                                color: "#b9afb1"
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
    footer: DialogButtonBox {
        background: null
        FlatButton {
            id: closeButton
            objectName: "shortcutHelpClose"
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }
}
