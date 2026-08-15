import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property string mode: "chat"
    property var conversationItems: []
    property var messageItems: []
    property var connections: []
    property int conversationId: 0
    property bool chatBusy: false
    property string workDir: ""
    property var backendConfig: ({chatBackends: [], chatBackend: "default"})
    function reload() {
        conversationItems = bridge.conversations()
        connections = bridge.ragConnections()
        backendConfig = bridge.settings()
    }
    function filteredConversations() { return conversationItems.filter(function(item) { return item.mode === root.mode }) }
    function reloadMessages() { messageItems = bridge.messages(conversationId); Qt.callLater(function() { messages.positionViewAtEnd() }) }
    Component.onCompleted: reload()
    Connections {
        target: bridge
        function onSourcesChanged() { root.reload() }
        function onConversationsChanged() { root.reload() }
        function onMessagesChanged() { root.reloadMessages() }
        function onSettingsChanged() { root.reload() }
        function onBusyChanged(task, busy) { if (task === "chat") root.chatBusy = busy }
        function onChatCompleted() {
            root.reload(); var rows = root.filteredConversations()
            if (rows.length) { root.conversationId = rows[0].id; root.workDir = rows[0].workDir || root.workDir; root.reloadMessages() }
        }
    }

    ColumnLayout {
        anchors.fill: parent; spacing: 14
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout { Text { text: "会话"; color: Theme.text; font.pixelSize: 27; font.weight: Font.Bold } Text { text: root.mode === "chat" ? "通过外部 RAG 检索知识并对话" : "让 Agent 在所选本地目录中完成工作"; color: Theme.textMuted; font.pixelSize: 13 } }
            Item { Layout.fillWidth: true }
            Repeater {
                model: [{key:"chat", label:"Chat"}, {key:"work", label:"Work"}]
                PrimaryButton {
                    required property var modelData; text: modelData.label; secondary: root.mode !== modelData.key
                    onClicked: { root.mode = modelData.key; root.conversationId = 0; root.messageItems = [] }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 16
            AppCard {
                Layout.preferredWidth: 280; Layout.fillHeight: true; hoverable: false
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 16; spacing: 12
                    SectionTitle { Layout.fillWidth: true; title: root.mode === "chat" ? "Chat 会话" : "Work 会话"; subtitle: root.mode === "chat" ? "连接外部知识库" : "目录与后端在创建时固定" }
                    PrimaryButton { Layout.fillWidth: true; text: "新建"; onClicked: { root.conversationId = 0; root.messageItems = []; if (root.mode === "work") root.workDir = "" } }
                    ListView {
                        id: conversationList; Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6; clip: true; model: root.filteredConversations()
                        ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: SelectableSurface {
                            required property var modelData; width: conversationList.width; height: 68; surfaceRadius: 12; selected: root.conversationId === modelData.id
                            onActivated: { root.conversationId = modelData.id; root.workDir = modelData.workDir || ""; bridge.selectConversation(modelData.id); root.reloadMessages() }
                            Column { anchors.fill: parent; anchors.margins: 11; spacing: 4
                                Text { width: parent.width; text: modelData.title; elide: Text.ElideRight; color: Theme.text; font.pixelSize: 13; font.weight: Font.DemiBold }
                                Text { width: parent.width; text: root.mode === "chat" ? (modelData.ragName || "外部 RAG") : modelData.backend + " · " + modelData.workDir; elide: Text.ElideMiddle; color: Theme.textFaint; font.pixelSize: 10 }
                            }
                        }
                    }
                }
            }
            AppCard {
                Layout.fillWidth: true; Layout.fillHeight: true; hoverable: false
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 18; spacing: 12
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Text { text: root.mode === "chat" ? "和 Bongo 一起学习" : "Bongo Work Agent"; color: Theme.text; font.pixelSize: 22; font.weight: Font.Bold }
                            Text { text: root.mode === "chat" ? (root.connections.length ? "当前连接：" + root.connections[0].name : "尚未配置外部 RAG") : (root.workDir || "请选择本地工作目录"); color: Theme.textMuted; font.pixelSize: 12; elide: Text.ElideMiddle; Layout.maximumWidth: 520 }
                        }
                        Item { Layout.fillWidth: true }
                        PrimaryButton { visible: root.mode === "chat"; text: "RAG 配置"; secondary: true; onClicked: ragDialog.openForCurrent() }
                        PrimaryButton { visible: root.mode === "work" && root.conversationId === 0; text: root.workDir ? "更换目录" : "选择目录"; secondary: true; onClicked: { var path = bridge.chooseWorkDirectory(); if (path) root.workDir = path } }
                        GlassComboBox { id: backendCombo; visible: root.mode === "work" && root.conversationId === 0; implicitWidth: 170; model: root.backendConfig.chatBackends || []; textRole: "label"; valueRole: "value" }
                    }
                    ListView {
                        id: messages; Layout.fillWidth: true; Layout.fillHeight: true; spacing: 10; clip: true; model: root.messageItems
                        ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: Item {
                            required property var modelData; width: messages.width; height: bubble.implicitHeight
                            Rectangle {
                                id: bubble; anchors.right: modelData.role === "user" ? parent.right : undefined; anchors.left: modelData.role === "user" ? undefined : parent.left
                                width: Math.min(parent.width * 0.8, messageText.implicitWidth + 30); implicitHeight: messageText.implicitHeight + 24; radius: 15
                                color: modelData.role === "user" ? "#36df7845" : "#72ffffff"; border.color: modelData.role === "user" ? "#45df7845" : Theme.border
                                Text { id: messageText; anchors.fill: parent; anchors.margins: 12; text: modelData.content; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: 14; lineHeight: 1.4 }
                            }
                        }
                        Text { anchors.centerIn: parent; visible: root.messageItems.length === 0; text: root.mode === "chat" ? "配置 RAG 后直接提问，无需选择单个文档" : "选择目录与后端后，把任务交给 Work Agent"; color: Theme.textFaint; font.pixelSize: 14 }
                    }
                    GlassTextArea { id: chatInput; Layout.fillWidth: true; implicitHeight: 96; placeholderText: root.mode === "chat" ? "询问外部知识库中的内容…" : "描述需要在该目录完成的任务…"; font.pixelSize: 14 }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: root.chatBusy ? (root.mode === "chat" ? "正在检索并回答…" : "Agent 正在检查并执行…") : (root.mode === "chat" ? "检索引用和会话记录保存在本机" : "默认后端保留 ReAct 工具执行轨迹"); color: Theme.textMuted; font.pixelSize: 11 }
                        Item { Layout.fillWidth: true }
                        PrimaryButton {
                            text: root.chatBusy ? "处理中…" : "发送"; enabled: !root.chatBusy && chatInput.text.trim().length > 0 && (root.mode === "chat" ? root.connections.length > 0 : (root.conversationId > 0 || root.workDir.length > 0))
                            onClicked: { if (root.mode === "chat") bridge.sendChat(0, root.conversationId, chatInput.text); else bridge.sendWork(root.conversationId, root.workDir, backendCombo.currentValue || "default", chatInput.text); chatInput.clear() }
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: ragDialog; modal: true; anchors.centerIn: parent; width: 620; height: 600; title: "外部 RAG 连接"; standardButtons: Dialog.NoButton
        function openForCurrent() {
            var item = root.connections.length ? root.connections[0] : {}
            connectionId.text = item.id || 0; connectionName.text = item.name || "默认 RAG"; baseUrl.text = item.baseUrl || ""; apiKey.text = item.apiKey || ""; knowledgeId.text = item.knowledgeId || ""
            uploadPath.text = item.uploadPath || "/documents"; retrievalPath.text = item.retrievalPath || "/retrieval"; deletePath.text = item.deletePath || "/documents/{document_id}"; open()
        }
        background: Rectangle { color: Theme.glassStrong; radius: Theme.cardRadius; border.color: Theme.border }
        contentItem: Flickable {
            contentWidth: width; contentHeight: form.implicitHeight; clip: true; ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
            ColumnLayout {
                id: form; width: parent.width; spacing: 10
                Text { text: "类似 Dify External Knowledge 的 HTTP 接口；本次不启用 MCP。"; color: Theme.textMuted; font.pixelSize: 12 }
                Text { id: connectionId; visible: false; text: "0" }
                Text { text: "连接名称"; color: Theme.textMuted; font.pixelSize: 11 } GlassTextField { id: connectionName; Layout.fillWidth: true }
                Text { text: "Base URL"; color: Theme.textMuted; font.pixelSize: 11 } GlassTextField { id: baseUrl; Layout.fillWidth: true; placeholderText: "https://rag.example.com" }
                Text { text: "API Key"; color: Theme.textMuted; font.pixelSize: 11 } GlassTextField { id: apiKey; Layout.fillWidth: true; echoMode: TextInput.Password }
                Text { text: "Knowledge ID"; color: Theme.textMuted; font.pixelSize: 11 } GlassTextField { id: knowledgeId; Layout.fillWidth: true }
                Text { text: "上传 / 检索 / 删除路径"; color: Theme.textMuted; font.pixelSize: 11 }
                RowLayout { Layout.fillWidth: true; GlassTextField { id: uploadPath; Layout.fillWidth: true } GlassTextField { id: retrievalPath; Layout.fillWidth: true } GlassTextField { id: deletePath; Layout.fillWidth: true } }
                RowLayout {
                    Layout.fillWidth: true; Item { Layout.fillWidth: true }
                    PrimaryButton { visible: Number(connectionId.text) > 0; text: "测试连接"; secondary: true; onClicked: bridge.testRagConnection(Number(connectionId.text)) }
                    PrimaryButton { text: "取消"; secondary: true; onClicked: ragDialog.close() }
                    PrimaryButton { text: "保存并启用"; onClicked: { bridge.saveRagConnection(Number(connectionId.text), connectionName.text, baseUrl.text, apiKey.text, knowledgeId.text, uploadPath.text, retrievalPath.text, deletePath.text); ragDialog.close() } }
                }
            }
        }
    }
}
