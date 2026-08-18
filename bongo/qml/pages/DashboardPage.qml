import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property var dashboardData: ({ stats: [], activity: [], trend: [], applicationUsage: [], applicationKeystrokes: [] })
    property var newsItems: []
    property string liveDateTime: ""
    signal openNews(int newsId)

    function reload() {
        dashboardData = bridge.dashboard()
        newsItems = bridge.news()
    }
    function refreshDashboard() { dashboardData = bridge.dashboard() }
    function updateLiveDateTime() {
        liveDateTime = Qt.formatDateTime(new Date(), "yyyy年MM月dd日 · HH:mm:ss")
    }
    function rankingDepth(index) { return 1 - Math.min(19, Math.max(0, index)) / 19 }
    function rankingBadgeColor(index) {
        return Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.07 + root.rankingDepth(index) * 0.33)
    }
    function rankingBadgeBorder(index) {
        return Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14 + root.rankingDepth(index) * 0.38)
    }
    function rankingBadgeText(index) {
        return Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.52 + root.rankingDepth(index) * 0.48)
    }

    Component.onCompleted: {
        updateLiveDateTime()
        reload()
    }
    Timer {
        interval: 1000
        repeat: true
        running: root.visible
        triggeredOnStart: true
        onTriggered: root.updateLiveDateTime()
    }
    onVisibleChanged: {
        if (visible) Qt.callLater(function() {
            usageDonut.playLoadAnimation()
            keystrokeChart.playLoadAnimation()
        })
    }
    Connections {
        target: bridge
        function onDashboardChanged() { root.dashboardData = bridge.dashboard() }
        function onNewsChanged() { root.newsItems = bridge.news() }
    }

    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight + 24
        clip: true
        ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }

        ColumnLayout {
            id: contentColumn
            width: parent.width
            spacing: 18

            RowLayout {
                Layout.fillWidth: true
                ColumnLayout {
                    spacing: 4
                    Text { text: "你好，欢迎回来"; color: Theme.text; font.pixelSize: 28; font.weight: Font.Bold }
                    Text { text: "Bongo 正在整理你的知识、专注时间与最新 AI 动态"; color: Theme.textMuted; font.pixelSize: 13 }
                }
                Item { Layout.fillWidth: true }
                PrimaryButton {
                    text: "刷新数据"
                    secondary: true
                    onClicked: root.refreshDashboard()
                }
                AppCard {
                    id: clockCard
                    Layout.preferredWidth: 218
                    Layout.preferredHeight: 42
                    cardRadius: 21
                    baseColor: Theme.glassStrong
                    glowColor: Theme.accent
                    frostStrength: 0.9
                    hoverScale: 1.012
                    hoverLift: 2
                    Text {
                        anchors.centerIn: parent
                        text: root.liveDateTime
                        color: clockCard.hovered ? Theme.accentHover : Theme.accent
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: width > 1100 ? 5 : width > 720 ? 3 : 2
                columnSpacing: 14
                rowSpacing: 14
                Repeater {
                    model: root.dashboardData.stats || []
                    MetricCard {
                        Layout.fillWidth: true
                        label: modelData.label
                        value: modelData.value
                        suffix: modelData.suffix
                        symbol: modelData.icon
                        accent: modelData.accent
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 660
                columns: width > 1000 ? 3 : 1
                columnSpacing: 16
                rowSpacing: 16

                AppCard {
                    Layout.columnSpan: root.width > 1000 ? 2 : 1
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    hoverable: true
                    hoverScale: 1.003
                    hoverLift: 2
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 20; spacing: 12
                        SectionTitle { Layout.fillWidth: true; title: "近七日工作趋势"; subtitle: "每日键盘敲击与工作时间 · 悬停查看具体数据" }
                        DualMetricLineChart { Layout.fillWidth: true; Layout.fillHeight: true; points: root.dashboardData.trend || [] }
                    }
                }

                AppCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.rowSpan: root.width > 1000 ? 2 : 1
                    hoverable: true
                    hoverScale: 1.003
                    hoverLift: 2
                    frostStrength: 1.85
                    baseColor: "#d8fff8ee"
                    borderColor: "#efffffff"
                    glowColor: Theme.accent
                    Rectangle {
                        anchors.fill: parent
                        radius: Theme.cardRadius
                        gradient: Gradient {
                            orientation: Gradient.Vertical
                            GradientStop { position: 0.0; color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.18) }
                            GradientStop { position: 0.42; color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.085) }
                            GradientStop { position: 1.0; color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.018) }
                        }
                    }
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 18; spacing: 12
                        SectionTitle { Layout.fillWidth: true; title: "AI 热讯 Top 20"; subtitle: "来自 Hacker News · 点击查看" }
                        ListView {
                            id: ranking
                            Layout.fillWidth: true; Layout.fillHeight: true
                            clip: true; spacing: 4
                            model: root.newsItems
                            ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                            delegate: Rectangle {
                                required property var modelData
                                required property int index
                                width: ranking.width; height: 62; radius: 11
                                color: mouse.containsMouse ? "#30ffffff" : "#0cffffff"
                                border.color: mouse.containsMouse ? "#70ffffff" : "#24ffffff"
                                border.width: 1
                                opacity: modelData.isRead ? 0.58 : 1
                                Behavior on color { ColorAnimation { duration: 130 } }
                                RowLayout {
                                    anchors.fill: parent; anchors.leftMargin: 8; anchors.rightMargin: 8; spacing: 10
                                    Rectangle {
                                        width: 32; height: 32; radius: 16
                                        color: root.rankingBadgeColor(index)
                                        border.color: root.rankingBadgeBorder(index)
                                        border.width: 1
                                        scale: mouse.containsMouse ? 1.08 : 1
                                        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                                        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                                        Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
                                        Text { anchors.centerIn: parent; text: index + 1; color: root.rankingBadgeText(index); font.pixelSize: 11; font.weight: Font.Bold }
                                    }
                                    ColumnLayout {
                                        Layout.fillWidth: true; spacing: 3
                                        Text { Layout.fillWidth: true; text: modelData.title; elide: Text.ElideRight; color: Theme.text; font.pixelSize: 13; font.weight: Font.DemiBold }
                                        Text { text: modelData.author + " · " + modelData.publishedDisplay; color: Theme.textFaint; font.pixelSize: 10 }
                                    }
                                }
                                MouseArea { id: mouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.openNews(modelData.id) }
                                NumberAnimation on opacity { from: 0; to: modelData.isRead ? 0.58 : 1; duration: 300; easing.type: Easing.OutCubic }
                            }
                        }
                        Text {
                            visible: root.newsItems.length === 0
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            text: "暂无简讯\n启动后会在后台逐条生成"
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            color: Theme.textFaint
                            font.pixelSize: 12
                        }
                    }
                }

                AppCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    hoverable: true
                    hoverScale: 1.004
                    hoverLift: 3
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 20; spacing: 10
                        SectionTitle { Layout.fillWidth: true; title: "今日软件使用"; subtitle: root.dashboardData.trackingEnabled ? "前台工作时间分布 · 悬停查看详情" : "活动记录尚未开启" }
                        ApplicationUsageDonut { id: usageDonut; Layout.fillWidth: true; Layout.fillHeight: true; points: root.dashboardData.applicationUsage || [] }
                    }
                }

                AppCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    hoverable: true
                    hoverScale: 1.004
                    hoverLift: 3
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 20; spacing: 10
                        SectionTitle { Layout.fillWidth: true; title: "今日应用敲击排行"; subtitle: "按键盘敲击次数降序 · 悬停突出显示" }
                        ApplicationKeystrokeBarChart { id: keystrokeChart; Layout.fillWidth: true; Layout.fillHeight: true; points: root.dashboardData.applicationKeystrokes || [] }
                    }
                }
            }
        }
    }
}
