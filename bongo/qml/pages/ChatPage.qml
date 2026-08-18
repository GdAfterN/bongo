import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "chatPage"
    property string mode: "chat"
    property var conversationItems: []
    property var connections: []
    property int conversationId: 0
    property bool chatBusy: false
    property string workDir: ""
    property var backendConfig: ({chatBackends: [], chatBackend: "default"})
    property string activeRequestId: ""
    property int requestSequence: 0
    property bool canonicalReloadPending: false
    readonly property string currentBackend: {
        if (mode === "chat")
            return "default"
        for (var index = 0; index < conversationItems.length; index++) {
            var conversation = conversationItems[index]
            if (Number(conversation.id) === conversationId)
                return String(conversation.backend || "default")
        }
        return String(backendCombo.currentValue || "default")
    }

    ListModel {
        id: messageModel
        dynamicRoles: true
    }

    function reload() {
        conversationItems = bridge.conversations()
        connections = bridge.ragConnections()
        backendConfig = bridge.settings()
    }
    function filteredConversations() { return conversationItems.filter(function(item) { return item.mode === root.mode }) }
    function backendLabel(backend) {
        if (backend === "cc")
            return "Claude Code"
        if (backend === "codex")
            return "Codex"
        return "默认"
    }
    function clearMessages() {
        messageModel.clear()
        canonicalReloadPending = false
    }
    function appendCanonicalMessage(item) {
        messageModel.append({
            messageId: Number(item.id || 0),
            requestId: "",
            role: String(item.role || "assistant"),
            content: String(item.content || ""),
            status: "done",
            citations: item.citations || []
        })
    }
    function reloadMessages() {
        var rows = conversationId > 0 ? bridge.messages(conversationId) : []
        messageModel.clear()
        for (var index = 0; index < rows.length; index++)
            appendCanonicalMessage(rows[index])
        canonicalReloadPending = false
        Qt.callLater(function() { messages.positionViewAtEnd() })
    }
    function requestMessageIndex(requestId, role) {
        for (var index = 0; index < messageModel.count; index++) {
            var item = messageModel.get(index)
            if (item.requestId === requestId && item.role === role)
                return index
        }
        return -1
    }
    function appendOptimisticTurn(requestId, content) {
        messageModel.append({
            messageId: 0,
            requestId: requestId,
            role: "user",
            content: content,
            status: "sending",
            citations: []
        })
        messageModel.append({
            messageId: 0,
            requestId: requestId,
            role: "assistant",
            content: "",
            status: "waiting",
            citations: []
        })
        Qt.callLater(function() { messages.positionViewAtEnd() })
    }
    function updateAssistant(requestId, delta) {
        var index = requestMessageIndex(requestId, "assistant")
        if (index < 0)
            return
        var followOutput = messages.atYEnd || messages.contentHeight <= messages.height
        var previous = String(messageModel.get(index).content || "")
        messageModel.setProperty(index, "content", previous + delta)
        messageModel.setProperty(index, "status", "streaming")
        if (followOutput)
            Qt.callLater(function() { messages.positionViewAtEnd() })
    }
    function failRequest(requestId, error) {
        var index = requestMessageIndex(requestId, "assistant")
        if (index >= 0) {
            messageModel.setProperty(index, "content", String(error || "模型暂时没有返回结果，请稍后重试。"))
            messageModel.setProperty(index, "status", "error")
        }
        if (activeRequestId === requestId) {
            activeRequestId = ""
            chatBusy = false
        }
        canonicalReloadPending = false
        reload()
        Qt.callLater(function() { messages.positionViewAtEnd() })
    }
    function canSubmit() {
        return !chatBusy
                && chatInput.text.trim().length > 0
                && (mode === "chat"
                    ? connections.length > 0
                    : (conversationId > 0 || workDir.length > 0))
    }
    function nextRequestId() {
        requestSequence += 1
        return mode + "-" + Date.now().toString(36) + "-" + requestSequence
    }
    function submitMessage() {
        if (!canSubmit())
            return
        var content = chatInput.text.trim()
        var requestId = nextRequestId()
        activeRequestId = requestId
        chatBusy = true
        appendOptimisticTurn(requestId, content)
        chatInput.clear()
        chatInput.forceActiveFocus()
        try {
            if (mode === "chat")
                bridge.sendChat(requestId, 0, conversationId, content)
            else
                bridge.sendWork(requestId, conversationId, workDir, backendCombo.currentValue || "default", content)
        } catch (error) {
            failRequest(requestId, String(error))
        }
    }
    function insertInputNewline() {
        var start = Math.min(chatInput.selectionStart, chatInput.selectionEnd)
        var end = Math.max(chatInput.selectionStart, chatInput.selectionEnd)
        if (start !== end)
            chatInput.remove(start, end)
        chatInput.insert(start, "\n")
        chatInput.cursorPosition = start + 1
    }
    Component.onCompleted: reload()
    Connections {
        target: bridge
        ignoreUnknownSignals: true
        function onSourcesChanged() { root.reload() }
        function onConversationsChanged() { root.reload() }
        function onMessagesChanged() {
            if (root.activeRequestId.length > 0)
                root.canonicalReloadPending = true
            else
                root.reloadMessages()
        }
        function onSettingsChanged() { root.reload() }
        function onBusyChanged(task, busy) { if (task === "chat") root.chatBusy = busy }
        function onChatStarted(requestId, conversationId) {
            if (requestId !== root.activeRequestId)
                return
            root.conversationId = conversationId
            root.reload()
        }
        function onChatDelta(requestId, delta) {
            if (requestId === root.activeRequestId)
                root.updateAssistant(requestId, delta)
        }
        function onChatFailed(requestId, error) {
            if (requestId === root.activeRequestId)
                root.failRequest(requestId, error)
        }
        function onChatStreamCompleted(requestId, conversationId) {
            if (requestId !== root.activeRequestId)
                return
            root.conversationId = conversationId
            root.activeRequestId = ""
            root.chatBusy = false
            root.reload()
            Qt.callLater(function() { root.reloadMessages() })
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
                    enabled: !root.chatBusy
                    onClicked: { root.mode = modelData.key; root.conversationId = 0; root.clearMessages() }
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
                    PrimaryButton { Layout.fillWidth: true; text: "新建"; enabled: !root.chatBusy; onClicked: { root.conversationId = 0; root.clearMessages(); if (root.mode === "work") root.workDir = "" } }
                    ListView {
                        id: conversationList; Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6; clip: true; model: root.filteredConversations()
                        ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: SelectableSurface {
                            required property var modelData; width: conversationList.width; height: 68; surfaceRadius: 12; selected: root.conversationId === modelData.id
                            enabled: !root.chatBusy
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
                        PrimaryButton { visible: root.mode === "chat"; enabled: !root.chatBusy; text: "RAG 配置"; secondary: true; onClicked: ragDialog.openForCurrent() }
                        PrimaryButton { visible: root.mode === "work" && root.conversationId === 0; enabled: !root.chatBusy; text: root.workDir ? "更换目录" : "选择目录"; secondary: true; onClicked: { var path = bridge.chooseWorkDirectory(); if (path) root.workDir = path } }
                        GlassComboBox {
                            id: backendCombo
                            objectName: "currentBackendControl"
                            implicitWidth: 190
                            leftPadding: 34
                            model: root.backendConfig.chatBackends || []
                            textRole: "label"
                            valueRole: "value"
                            displayText: "当前后端 · " + root.backendLabel(root.currentBackend)
                            enabled: root.mode === "work" && root.conversationId === 0 && !root.chatBusy
                            showIndicator: root.mode === "work" && root.conversationId === 0

                            Rectangle {
                                anchors.left: parent.left
                                anchors.leftMargin: 16
                                anchors.verticalCenter: parent.verticalCenter
                                width: 7
                                height: 7
                                radius: 4
                                color: Theme.accent
                                z: 2
                            }
                        }
                    }
                    ListView {
                        id: messages; objectName: "chatMessages"; Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12; clip: true; model: messageModel
                        ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: MessageBubble {
                            width: messages.width
                            messageRole: model.role
                            body: model.content
                            deliveryState: model.status
                        }
                        Text { anchors.centerIn: parent; visible: messageModel.count === 0; text: root.mode === "chat" ? "配置 RAG 后直接提问，无需选择单个文档" : "选择目录与后端后，把任务交给 Work Agent"; color: Theme.textFaint; font.pixelSize: 14 }
                    }
                    GlassTextArea {
                        id: chatInput
                        objectName: "chatInput"
                        Layout.fillWidth: true
                        implicitHeight: 96
                        placeholderText: root.mode === "chat" ? "询问外部知识库中的内容…" : "描述需要在该目录完成的任务…"
                        font.pixelSize: 14
                        Keys.priority: Keys.BeforeItem
                        Keys.onPressed: function(event) {
                            var isReturn = event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                            if (!isReturn || event.isAutoRepeat || chatInput.inputMethodComposing)
                                return
                            if ((event.modifiers & Qt.ControlModifier) !== 0)
                                root.insertInputNewline()
                            else
                                root.submitMessage()
                            event.accepted = true
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: root.chatBusy ? (root.mode === "chat" ? "正在检索并回答…" : "Agent 正在检查并执行…") : "Enter 发送 · Ctrl + Enter 换行"; color: Theme.textMuted; font.pixelSize: 11 }
                        Item { Layout.fillWidth: true }
                        PrimaryButton {
                            objectName: "chatSendButton"
                            text: root.chatBusy ? "生成中…" : "发送"
                            enabled: root.canSubmit()
                            onClicked: root.submitMessage()
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
