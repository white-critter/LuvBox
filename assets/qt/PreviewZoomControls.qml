import QtQuick
import QtQuick.Controls

Row {
    id: controls
    required property var preview
    spacing: 8
    FlatButton {
        text: "−"
        Accessible.name: "미리보기 축소"
        implicitWidth: 36
        leftPadding: 0
        rightPadding: 0
        enabled: controls.preview.canZoomOut
        onClicked: controls.preview.zoomOut()
    }
    Label {
        text: Math.round(controls.preview.zoomPercent) + "%"
        width: 54
        height: 36
        verticalAlignment: Text.AlignVCenter
        horizontalAlignment: Text.AlignHCenter
        color: "#f4f1f1"
        font.pixelSize: 13
    }
    FlatButton {
        text: "+"
        Accessible.name: "미리보기 확대"
        implicitWidth: 36
        leftPadding: 0
        rightPadding: 0
        enabled: controls.preview.canZoomIn
        onClicked: controls.preview.zoomIn()
    }
    FlatButton { text: "화면 맞춤"; onClicked: controls.preview.fit() }
    FlatButton {
        text: "원본 100%"
        enabled: controls.preview.canActualSize
        onClicked: controls.preview.actualSize()
        ToolTip.visible: hovered
        ToolTip.text: "원본 배율은 1,600만 화소 이하의 확인된 사진에서 사용할 수 있습니다."
    }
}
