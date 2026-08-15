import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property string currentType: "document"
    property var codeItems: []
    property var documentItems: []
    property bool ingestBusy: false
    property var bankQuestions: []
    property var selectedQuestion: ({})
    function reload() { codeItems = bridge.sources(); documentItems = bridge.ragDocuments() }
    Component.onCompleted: reload()
    Connections {
        target: bridge
        function onSourcesChanged() { root.reload() }
        function onBusyChanged(task, busy) { if (task === "ingest") root.ingestBusy = busy }
    }

    ColumnLayout {
        anchors.fill: parent; spacing: 16
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                Text { text: "知识库"; color: Theme.text; font.pixelSize: 27; font.weight: Font.Bold }
                Text { text: root.currentType === "document" ? "文档同步到外部 RAG，用于 Chat 检索" : "代码在本地拆解为算法题，用于练习与桌宠出题"; color: Theme.textMuted; font.pixelSize: 13 }
            }
            Item { Layout.fillWidth: true }
            PrimaryButton {
                text: root.ingestBusy ? "正在处理…" : root.currentType === "code" ? "导入算法题" : "上传到 RAG"
                enabled: !root.ingestBusy
                onClicked: bridge.importKnowledge(root.currentType)
            }
        }
        RowLayout {
            spacing: 8
            Repeater {
                model: [{key:"document", label:"文档知识"}, {key:"code", label:"代码知识"}]
                PrimaryButton { required property var modelData; text: modelData.label; secondary: root.currentType !== modelData.key; onClicked: root.currentType = modelData.key }
            }
        }
        AppCard {
            Layout.fillWidth: true; Layout.fillHeight: true; hoverable: false
            ListView {
                id: list
                anchors.fill: parent; anchors.margins: 14; clip: true; spacing: 8
                model: root.currentType === "code" ? root.codeItems : root.documentItems
                ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                delegate: AppCard {
                    required property var modelData
                    width: list.width; height: 86; cardRadius: 16; baseColor: "#55ffffff"; hoverScale: 1.008; hoverLift: 2
                    RowLayout {
                        anchors.fill: parent; anchors.margins: 15; spacing: 14
                        Rectangle {
                            width: 44; height: 44; radius: 22; color: root.currentType === "code" ? "#18a18d9e" : Theme.accentSoft
                            LineIcon { anchors.centerIn: parent; width: 21; height: 21; source: Qt.resolvedUrl("../../assets/icons/" + (root.currentType === "code" ? "code.svg" : "knowledge.svg")); color: root.currentType === "code" ? Theme.mauve : Theme.accent }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true; spacing: 4
                            Text { Layout.fillWidth: true; text: root.currentType === "code" ? modelData.title : modelData.name; elide: Text.ElideRight; color: Theme.text; font.pixelSize: 15; font.weight: Font.DemiBold }
                            Text {
                                text: root.currentType === "code"
                                    ? modelData.name + " · " + modelData.questionCount + " 道题 · " + modelData.createdAt
                                    : modelData.connectionName + " · " + ({ready:"已索引", uploading:"上传中", failed:"同步失败"}[modelData.status] || modelData.status) + " · " + modelData.createdAt
                                color: modelData.status === "failed" ? Theme.danger : Theme.textFaint; font.pixelSize: 11
                            }
                        }
                        GlassSwitch { visible: root.currentType === "code"; checked: visible && modelData.bubbleEnabled; text: "气泡"; onToggled: if (visible) bridge.setSourceBubbleEnabled(modelData.id, checked) }
                        PrimaryButton {
                            visible: root.currentType === "code"; text: "题库"; secondary: true; implicitWidth: 72; enabled: visible && modelData.questionCount > 0
                            onClicked: { root.bankQuestions = bridge.questions(modelData.id); root.selectedQuestion = root.bankQuestions.length ? root.bankQuestions[0] : {}; bankDialog.open() }
                        }
                        PrimaryButton { visible: root.currentType === "document" && modelData.status === "failed"; text: "重试"; secondary: true; implicitWidth: 72; onClicked: bridge.retryRagDocument(modelData.id) }
                        PrimaryButton { text: "删除"; danger: true; implicitWidth: 72; onClicked: root.currentType === "code" ? bridge.deleteSource(modelData.id) : bridge.deleteRagDocument(modelData.id) }
                    }
                }
                Text { anchors.centerIn: parent; visible: list.count === 0; text: root.currentType === "code" ? "还没有导入算法题题解" : "还没有同步到外部 RAG 的文档"; color: Theme.textFaint; font.pixelSize: 14 }
            }
        }
    }

    Dialog {
        id: bankDialog; modal: true; width: Math.min(root.width - 80, 980); height: Math.min(root.height - 80, 680); anchors.centerIn: parent
        title: "本地算法题库"; standardButtons: Dialog.NoButton
        background: Rectangle { color: Theme.glassStrong; radius: Theme.cardRadius; border.color: Theme.border }
        footer: Item { implicitHeight: 54; PrimaryButton { anchors.right: parent.right; anchors.rightMargin: 18; anchors.verticalCenter: parent.verticalCenter; text: "关闭"; secondary: true; onClicked: bankDialog.close() } }
        contentItem: RowLayout {
            spacing: 14
            ListView {
                id: bankList; Layout.preferredWidth: 360; Layout.fillHeight: true; model: root.bankQuestions; spacing: 7; clip: true
                ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                delegate: SelectableSurface {
                    required property var modelData; required property int index
                    width: bankList.width; height: 70; surfaceRadius: 12; selected: root.selectedQuestion.id === modelData.id; onActivated: root.selectedQuestion = modelData
                    Column { anchors.fill: parent; anchors.margins: 11; spacing: 4
                        Text { width: parent.width; text: (index + 1) + ". " + modelData.prompt; elide: Text.ElideRight; color: Theme.text; font.pixelSize: 13; font.weight: Font.DemiBold }
                        Text { text: modelData.topic; color: Theme.accent; font.pixelSize: 10 }
                    }
                }
            }
            Rectangle { Layout.fillHeight: true; width: 1; color: Theme.divider }
            Flickable {
                Layout.fillWidth: true; Layout.fillHeight: true; contentWidth: width; contentHeight: detail.implicitHeight; clip: true
                ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                ColumnLayout {
                    id: detail; width: parent.width; spacing: 12
                    Text { Layout.fillWidth: true; text: root.selectedQuestion.title || ""; color: Theme.accent; font.pixelSize: 12; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; text: root.selectedQuestion.prompt || "选择一道题查看详情"; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: 18; font.weight: Font.DemiBold; lineHeight: 1.4 }
                    Repeater { model: root.selectedQuestion.options || []; Text { required property string modelData; required property int index; Layout.fillWidth: true; text: String.fromCharCode(65 + index) + ". " + modelData; wrapMode: Text.Wrap; color: index === root.selectedQuestion.correctIndex ? "#16805c" : Theme.textMuted; font.pixelSize: 14 } }
                    Rectangle { Layout.fillWidth: true; height: 1; color: Theme.divider }
                    Text { Layout.fillWidth: true; text: "解析\n" + (root.selectedQuestion.explanation || "暂无解析"); wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 14; lineHeight: 1.5 }
                }
            }
        }
    }
}
