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
    signal duplicateSelected()
    title: "라이브러리 통계 요약"
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape

    readonly property bool loaded: stats.files !== undefined
    readonly property string problem: stats.error ? String(stats.error) : ""
    readonly property bool hasAi: !!(stats.ai_categories && stats.ai_categories.length > 0)
    readonly property bool hasTags: !!(stats.user_tags && stats.user_tags.length > 0)

    readonly property int frameMargin: 24
    readonly property int preferredWidth: 860
    readonly property int preferredHeight: 660
    readonly property int limitWidth: parent ? Math.max(0, parent.width - frameMargin * 2) : preferredWidth
    readonly property int limitHeight: parent ? Math.max(0, parent.height - frameMargin * 2) : preferredHeight
    width: Math.min(preferredWidth, limitWidth)
    height: Math.min(preferredHeight, limitHeight)
    x: parent ? Math.max(0, Math.round((parent.width - width) / 2)) : 0
    y: parent ? Math.max(0, Math.round((parent.height - height) / 2)) : 0
    padding: 20
    topPadding: 12

    function amount(value) {
        // The default format switches to 1.23e+06 from a million on.
        return Number(value || 0).toLocaleString(Qt.locale(), "f", 0)
    }

    function reload() {
        stats = {}
        if (app && app.library)
            app.library.requestStatistics()
    }

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
            text: "저장한 파일의 용량, 종류, 작성자와 월별 현황입니다."
            leftPadding: 20
            rightPadding: 20
            color: "#b9afb1"
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }

    onAboutToShow: reload()

    Connections {
        target: dialog.app ? dialog.app.library : null
        function onStatisticsLoaded(data) {
            if (dialog.visible)
                dialog.stats = data || {}
        }
    }

    component StatCard: Rectangle {
        id: card
        property string caption: ""
        property string value: ""
        property color valueColor: "#f4f1f1"
        property int valueSize: 18
        property bool actionable: false
        signal activated()
        Layout.fillWidth: true
        Layout.preferredHeight: 72
        color: cardHover.containsMouse && actionable ? "#251c1e" : "#1b1b1b"
        radius: 10
        border.width: 1
        border.color: actionable ? (cardHover.containsMouse ? "#d93645" : "#4a282d") : "#2e2e2e"
        Accessible.role: actionable ? Accessible.Button : Accessible.StaticText
        Accessible.name: caption + " " + value
        MouseArea {
            id: cardHover
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: card.actionable ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: if (card.actionable) card.activated()
        }
        Column {
            anchors.centerIn: parent
            width: parent.width - 12
            spacing: 4
            Label {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: card.caption
                font.pixelSize: 12
                color: "#999999"
            }
            Label {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: card.value
                font.pixelSize: card.valueSize
                font.bold: card.valueSize >= 16
                color: card.valueColor
                elide: Text.ElideRight
                textFormat: Text.PlainText
            }
        }
    }

    contentItem: Flickable {
        id: statsFlick
        objectName: "statsFlick"
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        contentWidth: width
        // The two lists share whatever height is left, so the page only scrolls
        // when the window is too small for their minimum.
        readonly property bool stacked: width < 640
        readonly property real fixedHeight: cards.implicitHeight
            + (extras.visible ? extras.implicitHeight + statsColumn.spacing : 0) + statsColumn.spacing
        readonly property real minimumHeight: fixedHeight + (stacked ? 400 : 190)
        readonly property bool needsScroll: minimumHeight > height + 1
        contentHeight: Math.max(height, minimumHeight)
        ScrollBar.vertical: ScrollBar {
            objectName: "statsScrollBar"
            policy: statsFlick.needsScroll ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        }
        SmoothWheel { flickable: statsFlick }

        ColumnLayout {
            id: statsColumn
            width: statsFlick.width - (statsFlick.needsScroll ? 12 : 0)
            height: statsFlick.contentHeight
            spacing: 14

            GridLayout {
                id: cards
                Layout.fillWidth: true
                columns: statsFlick.width < 500 ? 2 : (statsFlick.width < 700 ? 3 : 5)
                columnSpacing: 10
                rowSpacing: 10

                StatCard {
                    objectName: "statsFilesCard"
                    caption: "전체 미디어"
                    value: dialog.loaded ? dialog.amount(dialog.stats.files) + "개" : "…"
                }
                StatCard {
                    objectName: "statsBytesCard"
                    caption: "총 저장 용량"
                    value: dialog.loaded ? (dialog.stats.formatted_bytes || "0 B") : "…"
                    valueColor: "#8fd3a0"
                }
                StatCard {
                    objectName: "statsPostsCard"
                    caption: "저장된 트윗"
                    value: dialog.loaded ? dialog.amount(dialog.stats.posts) + "건" : "…"
                }
                StatCard {
                    objectName: "statsKindsCard"
                    caption: "미디어 구성"
                    valueSize: 12
                    valueColor: "#88b0ea"
                    value: dialog.loaded
                        ? "사진 " + dialog.amount(dialog.stats.photo) + " · 영상 " + dialog.amount(dialog.stats.video)
                          + " · GIF " + dialog.amount(dialog.stats.animated_gif)
                        : "…"
                }
                StatCard {
                    objectName: "statsDuplicatesCard"
                    caption: "중복 사진"
                    value: dialog.loaded ? dialog.amount(dialog.stats.duplicates) + "개" : "…"
                    actionable: dialog.loaded && dialog.stats.duplicates > 0
                    valueColor: actionable ? "#ff8791" : "#f4f1f1"
                    onActivated: {
                        dialog.duplicateSelected()
                        dialog.close()
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                columns: statsFlick.stacked ? 1 : 2
                columnSpacing: 14
                rowSpacing: 14

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredWidth: 1
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
                            id: authorList
                            objectName: "statsAuthorList"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 2
                            boundsBehavior: Flickable.StopAtBounds
                            model: dialog.stats.authors ? dialog.stats.authors : []
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                            SmoothWheel { flickable: authorList }

                            delegate: Rectangle {
                                id: authorRow
                                required property int index
                                required property var modelData
                                width: authorList.width - (authorList.contentHeight > authorList.height ? 12 : 0)
                                height: 34
                                color: authorHover.hovered ? "#242424" : "transparent"
                                radius: 6
                                HoverHandler { id: authorHover }

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 8
                                    anchors.rightMargin: 6
                                    spacing: 8

                                    Label {
                                        text: String(authorRow.index + 1)
                                        font.pixelSize: 12
                                        font.bold: true
                                        color: authorRow.index < 3 ? "#e6bd79" : "#777777"
                                        Layout.preferredWidth: 20
                                    }
                                    Label {
                                        text: "@" + (authorRow.modelData.author || "")
                                        font.pixelSize: 13
                                        font.bold: true
                                        color: "#f4f1f1"
                                        elide: Text.ElideRight
                                        textFormat: Text.PlainText
                                        Layout.fillWidth: true
                                    }
                                    Label {
                                        text: dialog.amount(authorRow.modelData.files) + "개"
                                        font.pixelSize: 12
                                        color: "#b9afb1"
                                    }
                                    FlatButton {
                                        text: "모아보기"
                                        implicitHeight: 26
                                        font.pixelSize: 11
                                        Accessible.name: "@" + (authorRow.modelData.author || "") + " 모아보기"
                                        onClicked: {
                                            if (authorRow.modelData && authorRow.modelData.author) {
                                                dialog.authorSelected(authorRow.modelData.author)
                                                dialog.close()
                                            }
                                        }
                                    }
                                }
                            }

                            Label {
                                anchors.centerIn: parent
                                visible: authorList.count === 0
                                text: dialog.loaded ? "아직 작성자 기록이 없어요" : (dialog.problem.length ? "" : "통계를 불러오는 중")
                                color: "#777777"
                                font.pixelSize: 13
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredWidth: 1
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
                            text: "월별 저장 추이 (게시일 기준)"
                            font.pixelSize: 14
                            font.bold: true
                            color: "#f4f1f1"
                        }

                        ListView {
                            id: monthlyList
                            objectName: "statsMonthlyList"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 4
                            boundsBehavior: Flickable.StopAtBounds
                            // Newest month first: the recent months are what people look for.
                            model: dialog.stats.monthly ? dialog.stats.monthly.slice().reverse() : []
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                            SmoothWheel { flickable: monthlyList }

                            readonly property int maxCount: {
                                const rows = dialog.stats.monthly ? dialog.stats.monthly : []
                                let most = 1
                                for (let i = 0; i < rows.length; i++) {
                                    if (rows[i].count > most)
                                        most = rows[i].count
                                }
                                return most
                            }

                            delegate: RowLayout {
                                id: monthRow
                                required property var modelData
                                width: monthlyList.width - (monthlyList.contentHeight > monthlyList.height ? 12 : 0)
                                height: 26
                                spacing: 8

                                Label {
                                    text: monthRow.modelData.month || ""
                                    font.pixelSize: 12
                                    color: "#b9afb1"
                                    Layout.preferredWidth: 60
                                }
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 10
                                    color: "#222222"
                                    radius: 4
                                    Rectangle {
                                        height: parent.height
                                        width: Math.max(4, Math.round(parent.width
                                            * ((monthRow.modelData.count || 0) / monthlyList.maxCount)))
                                        radius: 4
                                        color: "#d93645"
                                    }
                                }
                                Label {
                                    text: dialog.amount(monthRow.modelData.count) + "개"
                                    font.pixelSize: 12
                                    color: "#f4f1f1"
                                    Layout.preferredWidth: Math.max(54, implicitWidth)
                                    horizontalAlignment: Text.AlignRight
                                }
                            }

                            Label {
                                anchors.centerIn: parent
                                visible: monthlyList.count === 0
                                text: dialog.loaded ? "아직 월별 기록이 없어요" : (dialog.problem.length ? "" : "통계를 불러오는 중")
                                color: "#777777"
                                font.pixelSize: 13
                            }
                        }
                    }
                }
            }

            // AI categories and personal tags share one card; each row appears only when it has entries.
            Rectangle {
                id: extras
                objectName: "statsExtrasCard"
                Layout.fillWidth: true
                visible: dialog.hasAi || dialog.hasTags || dialog.problem.length > 0
                implicitHeight: extrasColumn.implicitHeight + 20
                color: "#181818"
                radius: 10
                border.width: 1
                border.color: dialog.problem.length ? "#4a282d" : "#262626"

                ColumnLayout {
                    id: extrasColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 10
                    spacing: 6

                    RowLayout {
                        objectName: "statsError"
                        Layout.fillWidth: true
                        visible: dialog.problem.length > 0
                        spacing: 8
                        Label {
                            Layout.fillWidth: true
                            text: dialog.problem
                            color: "#ff8791"
                            wrapMode: Text.WordWrap
                            textFormat: Text.PlainText
                        }
                        FlatButton {
                            text: "다시 시도"
                            implicitHeight: 30
                            onClicked: dialog.reload()
                        }
                    }

                    RowLayout {
                        objectName: "statsAiCategoriesCard"
                        Layout.fillWidth: true
                        visible: dialog.hasAi
                        spacing: 8
                        Label {
                            text: "AI 분류"
                            font.pixelSize: 13
                            font.bold: true
                            color: "#f4f1f1"
                            Layout.preferredWidth: 64
                            Layout.alignment: Qt.AlignTop
                            topPadding: 6
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 6
                            Repeater {
                                model: dialog.stats.ai_categories ? dialog.stats.ai_categories : []
                                delegate: FlatButton {
                                    required property var modelData
                                    implicitHeight: 28
                                    font.pixelSize: 12
                                    text: (modelData.category || "") + " " + dialog.amount(modelData.count)
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

                    RowLayout {
                        objectName: "statsUserTagsCard"
                        Layout.fillWidth: true
                        visible: dialog.hasTags
                        spacing: 8
                        Label {
                            text: "내 태그"
                            font.pixelSize: 13
                            font.bold: true
                            color: "#f4f1f1"
                            Layout.preferredWidth: 64
                            Layout.alignment: Qt.AlignTop
                            topPadding: 6
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 6
                            Repeater {
                                model: dialog.stats.user_tags ? dialog.stats.user_tags : []
                                delegate: FlatButton {
                                    required property var modelData
                                    implicitHeight: 28
                                    font.pixelSize: 12
                                    text: "#" + (modelData.tag || "") + " " + dialog.amount(modelData.count)
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
