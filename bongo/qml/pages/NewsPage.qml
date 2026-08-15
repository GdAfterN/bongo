import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property var newsItems: []
    property var selected: ({})
    property bool busy: false
    property int progress: 0
    property string progressStage: ""
    property string progressDetail: ""
    function reload() { newsItems = bridge.news(); if (selected.id) { for (var i=0; i<newsItems.length; i++) if (newsItems[i].id === selected.id) selected = newsItems[i] } }
    function selectNewsById(newsId) { reload(); for (var i=0; i<newsItems.length; i++) if (newsItems[i].id === newsId) { selected = newsItems[i]; return } }
    Component.onCompleted: reload()
    Connections {
        target: bridge
        function onNewsChanged() { root.reload() }
        function onBusyChanged(task, value) { if (task === "news") root.busy = value }
        function onNewsProgressChanged(percent, stage, detail) { root.progress = percent; root.progressStage = stage; root.progressDetail = detail }
    }
    RowLayout {
        anchors.fill: parent; spacing: 16
        AppCard {
            Layout.preferredWidth: 410; Layout.fillHeight: true; hoverable: false
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 16; spacing: 10
                RowLayout { Layout.fillWidth: true; ColumnLayout { Text { text: "最新 AI 简讯"; color: Theme.text; font.pixelSize: 20; font.weight: Font.Bold } Text { text: "20 条独立生成 · 每 8 小时更新"; color: Theme.textMuted; font.pixelSize: 11 } } Item { Layout.fillWidth: true } PrimaryButton { text: root.busy ? "抓取中" : "主动抓取"; enabled: !root.busy; onClicked: bridge.refreshNews(true) } }
                GlassProgressBar { visible: root.busy; Layout.fillWidth: true; from: 0; to: 100; value: root.progress }
                Text { visible: root.busy; Layout.fillWidth: true; text: root.progressStage + (root.progressDetail ? " · " + root.progressDetail : ""); elide: Text.ElideRight; color: Theme.textMuted; font.pixelSize: 10 }
                ListView {
                    id: newsList; Layout.fillWidth: true; Layout.fillHeight: true; spacing: 7; clip: true; model: root.newsItems
                    ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: SelectableSurface {
                        required property var modelData
                        required property int index
                        width: newsList.width; height: 78; surfaceRadius: 13; opacity: modelData.isRead ? 0.58 : 1
                        selected: root.selected.id === modelData.id
                        onActivated: root.selected = modelData
                        RowLayout { anchors.fill: parent; anchors.margins: 11; spacing: 10
                            Rectangle { width: 32; height: 32; radius: 16; color: index < 3 ? Theme.accentSoft : "#54ffffff"; border.color: index < 3 ? "#4ddf7845" : Theme.border; Text { anchors.centerIn: parent; text: index + 1; color: index < 3 ? Theme.accent : Theme.textMuted; font.weight: Font.Bold } }
                            ColumnLayout { Layout.fillWidth: true; spacing: 4; Text { Layout.fillWidth: true; text: modelData.title; elide: Text.ElideRight; color: Theme.text; font.pixelSize: 13; font.weight: Font.DemiBold } Text { text: modelData.publishedDisplay + " · " + modelData.author; color: Theme.textFaint; font.pixelSize: 10 } Text { text: modelData.isRead ? "已读" : "未读"; color: modelData.isRead ? Theme.textFaint : Theme.accent; font.pixelSize: 9 } }
                        }
                    }
                }
            }
        }
        AppCard {
            Layout.fillWidth: true; Layout.fillHeight: true; hoverable: false
            Flickable {
                anchors.fill: parent; anchors.margins: 26; contentWidth: width; contentHeight: detail.implicitHeight; clip: true
                ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                ColumnLayout {
                    id: detail; width: parent.width; spacing: 14
                    Text { Layout.fillWidth: true; text: root.selected.title || "选择一条简讯查看详情"; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: 26; font.weight: Font.Bold; lineHeight: 1.2 }
                    Text { visible: Number(root.selected.id || 0) > 0; text: (root.selected.publishedDisplay || "") + " · 作者：" + (root.selected.author || "未知"); color: Theme.textMuted; font.pixelSize: 12 }
                    Rectangle { visible: Number(root.selected.id || 0) > 0; Layout.fillWidth: true; height: 1; color: Theme.divider }
                    Text { Layout.fillWidth: true; text: root.selected.summary || "AI 简讯会在启动后自动抓取。"; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: 16; lineHeight: 1.55 }
                    Text { visible: String(root.selected.original_title || "").length > 0; Layout.fillWidth: true; text: "原始标题：" + (root.selected.original_title || ""); wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 11 }
                    RowLayout { visible: Number(root.selected.id || 0) > 0; Layout.fillWidth: true; PrimaryButton { text: Boolean(root.selected.isRead) ? "已阅" : "朕已阅"; secondary: Boolean(root.selected.isRead); onClicked: { bridge.markNewsRead(root.selected.id); root.reload() } } Item { Layout.fillWidth: true } PrimaryButton { text: "打开原文"; onClicked: bridge.openUrl(root.selected.original_url || "") } }
                }
            }
        }
    }
}
