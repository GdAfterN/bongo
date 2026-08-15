import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "pages"

ApplicationWindow {
    id: window
    width: 1440
    height: 900
    minimumWidth: 1080
    minimumHeight: 700
    visible: true
    title: ""
    color: Theme.background
    font.family: "Microsoft YaHei UI"
    property int currentPage: 0
    property string toastTitle: ""
    property string toastDetail: ""
    property bool allowClose: false
    property int pendingNewsId: 0

    Component.onCompleted: bridge.attachWindow(window)
    onClosing: function(close) {
        if (!allowClose) {
            close.accepted = false
            window.hide()
        }
    }

    Connections {
        target: bridge
        function onShowWindowRequested() {
            window.visibility = Window.Windowed
            window.visible = true
            window.raise()
            window.requestActivate()
        }
        function onNavigateRequested(newsId) { window.pendingNewsId = newsId; newsPage.selectNewsById(newsId); window.currentPage = 5; window.show(); window.raise(); window.requestActivate() }
        function onStatusChanged(title, detail) { window.toastTitle = title; window.toastDetail = detail; toast.open() }
    }

    Popup {
        id: toast
        x: window.width - width - 28
        y: 24
        width: 370
        height: Math.max(76, toastColumn.implicitHeight + 26)
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        enter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 220 } NumberAnimation { property: "x"; from: window.width; to: window.width - toast.width - 28; duration: 280; easing.type: Easing.OutCubic } }
        exit: Transition { NumberAnimation { property: "opacity"; to: 0; duration: 180 } }
        background: Rectangle { radius: 18; color: Theme.glassStrong; border.color: Theme.border }
        contentItem: ColumnLayout { id: toastColumn; anchors.fill: parent; anchors.margins: 13; Text { text: window.toastTitle; color: Theme.text; font.pixelSize: 14; font.weight: Font.Bold } Text { Layout.fillWidth: true; visible: window.toastDetail.length > 0; text: window.toastDetail; wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 11 } }
        Timer { interval: 5000; running: toast.opened; onTriggered: toast.close() }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            Layout.preferredWidth: 96
            Layout.fillHeight: true
            color: "#69ffffff"
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 12
                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 58
                    Layout.bottomMargin: 10
                    LineIcon {
                        anchors.centerIn: parent
                        width: 44
                        height: 44
                        source: Qt.resolvedUrl("../assets/icons/cat.svg")
                        color: Theme.graphite
                    }
                }
                Rectangle {
                    Layout.alignment: Qt.AlignHCenter
                    width: 58
                    height: primaryNavigation.implicitHeight + 12
                    radius: 29
                    color: Theme.glassStrong
                    border.color: Theme.border
                    border.width: 1
                    Column {
                        id: primaryNavigation
                        anchors.centerIn: parent
                        spacing: 2
                        Repeater {
                            model: [
                                {label:"首页", page:0, icon:"home.svg"},
                                {label:"会话", page:1, icon:"chat.svg"},
                                {label:"知识库", page:2, icon:"knowledge.svg"},
                                {label:"练习", page:3, icon:"practice.svg"},
                                {label:"AI 简讯", page:5, icon:"news.svg"}
                            ]
                            NavButton {
                                required property var modelData
                                text: modelData.label
                                iconSource: Qt.resolvedUrl("../assets/icons/" + modelData.icon)
                                selected: window.currentPage === modelData.page
                                onClicked: window.currentPage = modelData.page
                            }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle {
                    Layout.alignment: Qt.AlignHCenter
                    width: 58
                    height: secondaryNavigation.implicitHeight + 12
                    radius: 29
                    color: Theme.glassStrong
                    border.color: Theme.border
                    border.width: 1
                    Column {
                        id: secondaryNavigation
                        anchors.centerIn: parent
                        spacing: 2
                        Repeater {
                            model: [
                                {label:"Skill", page:4, icon:"skill.svg"},
                                {label:"设置", page:6, icon:"settings.svg"}
                            ]
                            NavButton {
                                required property var modelData
                                text: modelData.label
                                iconSource: Qt.resolvedUrl("../assets/icons/" + modelData.icon)
                                selected: window.currentPage === modelData.page
                                onClicked: window.currentPage = modelData.page
                            }
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: Theme.background
            clip: true
            Rectangle {
                width: 420; height: 420; radius: 210
                x: parent.width - width * 0.68; y: -height * 0.46
                color: "#20df7845"
            }
            Rectangle {
                width: 360; height: 360; radius: 180
                x: -width * 0.42; y: parent.height - height * 0.55
                color: "#18829789"
            }
            Item {
                anchors.fill: parent
                anchors.margins: 24
                StackLayout {
                    id: pages
                    anchors.fill: parent
                    currentIndex: window.currentPage
                    property int previousIndex: 0
                    onCurrentIndexChanged: {
                        var current = itemAt(currentIndex)
                        if (current) { current.opacity = 0; current.x = 18; pageEnter.target = current; pageEnter.restart() }
                        previousIndex = currentIndex
                    }
                    DashboardPage { id: dashboardPage; onOpenNews: function(newsId) { newsPage.selectNewsById(newsId); window.currentPage = 5 } }
                    ChatPage {}
                    KnowledgePage {}
                    PracticePage {}
                    SkillPage {}
                    NewsPage { id: newsPage }
                    SettingsPage {}
                }
                ParallelAnimation {
                    id: pageEnter
                    property Item target
                    NumberAnimation { target: pageEnter.target; property: "opacity"; from: 0; to: 1; duration: 260; easing.type: Easing.OutCubic }
                    NumberAnimation { target: pageEnter.target; property: "x"; from: 18; to: 0; duration: 300; easing.type: Easing.OutCubic }
                }
            }
        }
    }
}
