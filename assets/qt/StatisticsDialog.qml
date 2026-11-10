import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "statisticsDialog"
    property var app: null
    property var stats: ({})
    signal authorSelected(string author)
    signal aiCategorySelected(string category)
    signal userTagSelected(string tag)
    title: "라이브러리 통계 요약"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape

    readonly property int frameMargin: 24
    readonly property int preferredWidth: 760
    readonly property int preferredHeight: 620
    readonly property int limitWidth: parent ? Math.max(0, parent.width - frameMargin * 2) : preferredWidth
    readonly property int limitHeight: parent ? Math.max(0, parent.height - frameMargin * 2) : preferredHeight
    width: Math.min(preferredWidth, limitWidth)
    height: Math.min(preferredHeight, limitHeight)
    x: parent ? Math.max(0, Math.round((parent.width - width) / 2)) : 0
    y: parent ? Math.max(0, Math.round((parent.height - height) / 2)) : 0
    padding: 20

    function syncContent() {
        if (!contentItem)
            return
        contentItem.width = availableWidth
        contentItem.height = availableHeight
    }

    onWidthChanged: syncContent()
    onHeightChanged: syncContent()

    background: Rectangle {
        color: "#141414"
        radius: 16
        border.width: 1
        border.color: "#2a2a2a"
    }

    header: Column {
        width: dialog.width
        Label {
            width: parent.width
            text: dialog.title
            leftPadding: 20
            rightPadding: 20
            topPadding: 16
            bottomPadding: 2
            font.pixelSize: 18
            color: "#f4f1f1"
        }
        Label {
            width: parent.width
            text: "현재 라이브러리의 파일 용량, 미디어 유형, 상위 작성자 및 월별 보관 현황입니다."
            leftPadding: 20
            rightPadding: 20
            color: "#b9afb1"
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }

    onAboutToShow: {
        syncContent()
        stats = {}
        if (app && app.library)
            app.library.requestStatistics()
    }

    Connections {
        target: app ? app.library : null
        function onStatisticsLoaded(data) {
            dialog.stats = data || {}
        }
    }

    contentItem: Flickable {
        id: statsFlick
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        contentWidth: width
        readonly property bool needsScroll: statsColumn.implicitHeight > height + 1
        contentHeight: statsColumn.implicitHeight
        ScrollBar.vertical: ScrollBar {
            policy: statsFlick.needsScroll ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        }

        ColumnLayout {
            id: statsColumn
            width: statsFlick.width - (statsFlick.needsScroll ? 12 : 0)
            spacing: 16

        // Metric summary cards. Two columns on a narrow dialog so the counts stay readable.
        GridLayout {
            Layout.fillWidth: true
            columns: statsFlick.width < 640 ? 2 : 4
            columnSpacing: 10
            rowSpacing: 10

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                color: "#1b1b1b"
                radius: 10
                border.width: 1
                border.color: "#2e2e2e"
                Column {
                    anchors.centerIn: parent
                    spacing: 4
                    Label {
                        text: "전체 미디어"
                        font.pixelSize: 12
                        color: "#999999"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Label {
                        text: dialog.stats && dialog.stats.files !== undefined ? Number(dialog.stats.files).toLocaleString() + "개" : "…"
                        font.pixelSize: 18
                        font.bold: true
                        color: "#f4f1f1"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                color: "#1b1b1b"
                radius: 10
                border.width: 1
                border.color: "#2e2e2e"
                Column {
                    anchors.centerIn: parent
                    spacing: 4
                    Label {
                        text: "총 저장 용량"
                        font.pixelSize: 12
                        color: "#999999"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Label {
                        text: dialog.stats && dialog.stats.formatted_bytes ? dialog.stats.formatted_bytes : "…"
                        font.pixelSize: 18
                        font.bold: true
                        color: "#8fd3a0"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                color: "#1b1b1b"
                radius: 10
                border.width: 1
                border.color: "#2e2e2e"
                Column {
                    anchors.centerIn: parent
                    spacing: 4
                    Label {
                        text: "저장된 트윗"
                        font.pixelSize: 12
                        color: "#999999"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Label {
                        text: dialog.stats && dialog.stats.posts !== undefined ? Number(dialog.stats.posts).toLocaleString() + "건" : "…"
                        font.pixelSize: 18
                        font.bold: true
                        color: "#f4f1f1"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                color: "#1b1b1b"
                radius: 10
                border.width: 1
                border.color: "#2e2e2e"
                Column {
                    anchors.centerIn: parent
                    width: Math.min(parent.width - 12, implicitWidth)
                    spacing: 4
                    Label {
                        text: "미디어 구성"
                        font.pixelSize: 12
                        color: "#999999"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Label {
                        text: {
                            if (!dialog.stats || dialog.stats.photo === undefined)
                                return "…"
                            return "사진 " + Number(dialog.stats.photo).toLocaleString() + " · 영상 " +
                                   Number(dialog.stats.video).toLocaleString() + " · GIF " +
                                   Number(dialog.stats.animated_gif).toLocaleString()
                        }
                        font.pixelSize: 12
                        color: "#88b0ea"
                        anchors.horizontalCenter: parent.horizontalCenter
                        elide: Text.ElideRight
                        width: Math.min(parent.width, implicitWidth)
                        horizontalAlignment: Text.AlignHCenter
                    }
                }
            }
        }

        // Authors and the monthly chart sit side by side until the dialog is narrow.
        GridLayout {
            Layout.fillWidth: true
            columns: statsFlick.width < 640 ? 1 : 2
            columnSpacing: 16
            rowSpacing: 16
            Layout.preferredHeight: statsFlick.width < 640 ? 420 : Math.max(200, statsFlick.height - 130)

            // Authors section
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "#181818"
                radius: 10
                border.width: 1
                border.color: "#262626"
                clip: true

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    Label {
                        text: "상위 작성자 TOP 10"
                        font.pixelSize: 14
                        font.bold: true
                        color: "#f4f1f1"
                    }

                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 4
                        model: dialog.stats && dialog.stats.authors ? dialog.stats.authors : []
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                        delegate: Rectangle {
                            width: ListView.view
                                ? Math.max(0, ListView.view.width - (ListView.view.contentHeight > ListView.view.height ? 12 : 0))
                                : 0
                            height: 36
                            color: authorHover.containsMouse ? "#242424" : "transparent"
                            radius: 6

                            MouseArea {
                                id: authorHover
                                anchors.fill: parent
                                hoverEnabled: true
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 8
                                anchors.rightMargin: 8
                                spacing: 8

                                Label {
                                    text: String(index + 1)
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: index < 3 ? "#e6bd79" : "#777777"
                                    Layout.preferredWidth: 20
                                }

                                Label {
                                    text: "@" + (modelData.author || "")
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: "#f4f1f1"
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }

                                Label {
                                    text: Number(modelData.files || 0).toLocaleString() + "개"
                                    font.pixelSize: 12
                                    color: "#b9afb1"
                                }

                                FlatButton {
                                    text: "모아보기"
                                    implicitHeight: 26
                                    font.pixelSize: 11
                                    onClicked: {
                                        if (modelData && modelData.author) {
                                            dialog.authorSelected(modelData.author)
                                            dialog.close()
                                        }
                                    }
                                }
                            }
                        }

                        Label {
                            anchors.centerIn: parent
                            visible: !parent.count
                            text: dialog.stats && dialog.stats.files !== undefined ? "작성자 기록이 없습니다." : "통계를 불러오는 중…"
                            color: "#777777"
                            font.pixelSize: 13
                        }
                    }
                }
            }

            // Monthly trend section
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "#181818"
                radius: 10
                border.width: 1
                border.color: "#262626"
                clip: true

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    Label {
                        text: "월별 저장 추이"
                        font.pixelSize: 14
                        font.bold: true
                        color: "#f4f1f1"
                    }

                    ListView {
                        id: monthlyList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 6
                        model: dialog.stats && dialog.stats.monthly ? dialog.stats.monthly : []
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                        readonly property int maxCount: {
                            if (!model || !model.length)
                                return 1
                            let m = 1
                            for (let i = 0; i < model.length; i++) {
                                if (model[i].count > m)
                                    m = model[i].count
                            }
                            return m
                        }

                        delegate: RowLayout {
                            width: ListView.view
                                ? Math.max(0, ListView.view.width - (ListView.view.contentHeight > ListView.view.height ? 12 : 0))
                                : 0
                            height: 28
                            spacing: 8

                            Label {
                                text: modelData.month || ""
                                font.pixelSize: 12
                                color: "#b9afb1"
                                Layout.preferredWidth: 64
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 12
                                color: "#222222"
                                radius: 4

                                Rectangle {
                                    height: parent.height
                                    width: Math.max(4, Math.round(parent.width * ((modelData.count || 0) / monthlyList.maxCount)))
                                    radius: 4
                                    color: "#d93645"
                                }
                            }

                            Label {
                                text: Number(modelData.count || 0).toLocaleString() + "개"
                                font.pixelSize: 12
                                color: "#f4f1f1"
                                Layout.preferredWidth: Math.max(54, implicitWidth)
                                horizontalAlignment: Text.AlignRight
                            }
                        }

                        Label {
                            anchors.centerIn: parent
                            visible: !parent.count
                            text: dialog.stats && dialog.stats.files !== undefined ? "월별 기록이 없습니다." : "통계를 불러오는 중…"
                            color: "#777777"
                            font.pixelSize: 13
                        }
                    }
                }
            }
        }

        // AI classification & user tag breakdown (shown when classified photos or user tags exist)
        Rectangle {
            objectName: "statsAiCategoriesCard"
            Layout.fillWidth: true
            visible: !!(dialog.stats && dialog.stats.ai_categories && dialog.stats.ai_categories.length > 0)
            implicitHeight: aiCategoriesColumn.implicitHeight + 24
            color: "#181818"
            radius: 10
            border.width: 1
            border.color: "#262626"

            ColumnLayout {
                id: aiCategoriesColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 12
                spacing: 8

                Label {
                    text: "AI 분류 현황"
                    font.pixelSize: 14
                    font.bold: true
                    color: "#f4f1f1"
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Repeater {
                        model: dialog.stats && dialog.stats.ai_categories ? dialog.stats.ai_categories : []
                        delegate: FlatButton {
                            implicitHeight: 30
                            font.pixelSize: 12
                            text: (modelData.category || "") + " (" + Number(modelData.count || 0).toLocaleString() + ")"
                            onClicked: {
                                if (modelData && modelData.category) {
                                    dialog.aiCategorySelected(modelData.category)
                                    dialog.close()
                                }
                            }
                        }
                    }
                }
            }
        }

        Rectangle {
            objectName: "statsUserTagsCard"
            Layout.fillWidth: true
            visible: !!(dialog.stats && dialog.stats.user_tags && dialog.stats.user_tags.length > 0)
            implicitHeight: userTagsColumn.implicitHeight + 24
            color: "#181818"
            radius: 10
            border.width: 1
            border.color: "#262626"

            ColumnLayout {
                id: userTagsColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 12
                spacing: 8

                Label {
                    text: "상위 내 태그"
                    font.pixelSize: 14
                    font.bold: true
                    color: "#f4f1f1"
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Repeater {
                        model: dialog.stats && dialog.stats.user_tags ? dialog.stats.user_tags : []
                        delegate: FlatButton {
                            implicitHeight: 30
                            font.pixelSize: 12
                            text: "#" + (modelData.tag || "") + " (" + Number(modelData.count || 0).toLocaleString() + ")"
                            onClicked: {
                                if (modelData && modelData.tag) {
                                    dialog.userTagSelected(modelData.tag)
                                    dialog.close()
                                }
                            }
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
            text: "닫기"
            DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
        }
        onRejected: dialog.close()
    }
}
