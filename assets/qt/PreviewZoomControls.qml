import QtQuick
import QtQuick.Controls

Row {
    id: controls
    required property var preview
    spacing: 8
    FlatButton {
        text: "−"
        focusPolicy: Qt.TabFocus
        Accessible.name: "미리보기 축소"
        implicitWidth: 36
        leftPadding: 0
        rightPadding: 0
        enabled: controls.preview.canZoomOut
        tip: "축소 (-)"
        onClicked: controls.preview.zoomOut()
    }
    Label {
        text: controls.preview.ready ? Math.round(controls.preview.zoomPercent) + "%" : "—"
        width: Math.max(54, implicitWidth)
        height: 36
        verticalAlignment: Text.AlignVCenter
        horizontalAlignment: Text.AlignHCenter
        color: Theme.text
        font.pixelSize: Theme.fontSmall
    }
    FlatButton {
        text: "+"
        focusPolicy: Qt.TabFocus
        Accessible.name: "미리보기 확대"
        implicitWidth: 36
        leftPadding: 0
        rightPadding: 0
        enabled: controls.preview.canZoomIn
        tip: "확대 (+ / =)"
        onClicked: controls.preview.zoomIn()
    }
    FlatButton {
        text: "화면 맞춤"
        focusPolicy: Qt.TabFocus
        enabled: controls.preview.ready
        tip: "화면에 맞추기 (0)"
        onClicked: controls.preview.fit()
    }
    FlatButton {
        text: "원본 100%"
        focusPolicy: Qt.TabFocus
        enabled: controls.preview.canActualSize
        onClicked: controls.preview.actualSize()
        tip: "원본 100%는 1,600만 화소 이하 사진에서 쓸 수 있습니다. (1)"
    }
}
