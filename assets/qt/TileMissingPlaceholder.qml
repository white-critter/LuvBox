pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

Rectangle {
    id: placeholder
    property bool active: false
    property bool missingFile: false
    property string captionText: ""

    radius: Theme.radiusControl
    color: Theme.well
    border.width: 1
    border.color: Theme.line
    visible: placeholder.active

    Column {
        anchors.centerIn: parent
        spacing: 6
        Loader {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 32
            height: 26
            active: placeholder.active
            sourceComponent: Canvas {
                width: 32
                height: 26
                onPaint: {
                    const ctx = getContext("2d")
                    ctx.reset()
                    ctx.strokeStyle = Theme.textFaint
                    ctx.lineWidth = 1.6
                    ctx.lineJoin = "round"
                    ctx.lineCap = "round"
                    ctx.strokeRect(2, 2, 28, 22)
                    ctx.beginPath()
                    ctx.arc(10, 9.5, 2.2, 0, Math.PI * 2)
                    ctx.stroke()
                    ctx.beginPath()
                    ctx.moveTo(5, 21)
                    ctx.lineTo(13, 13)
                    ctx.lineTo(18, 18)
                    ctx.lineTo(23, 14)
                    ctx.lineTo(27, 21)
                    ctx.stroke()
                    ctx.strokeStyle = Theme.accent
                    ctx.lineWidth = 1.8
                    ctx.beginPath()
                    ctx.moveTo(4, 4)
                    ctx.lineTo(28, 22)
                    ctx.stroke()
                }
            }
        }
        Label {
            anchors.horizontalCenter: parent.horizontalCenter
            text: placeholder.captionText.indexOf("클라우드") >= 0 ? "클라우드"
                : (placeholder.missingFile || placeholder.captionText.indexOf("파일 없음") >= 0 ? "파일 없음" : "미리보기 없음")
            font.pixelSize: Theme.fontTiny
            color: Theme.textFaint
            textFormat: Text.PlainText
        }
    }
}
