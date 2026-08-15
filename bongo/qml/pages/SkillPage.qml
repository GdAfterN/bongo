import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property var skillItems: []
    property var sourceItems: []
    property var preview: ({})
    property int selectedId: 0
    function openEditor(editing) {
        createDialog.editing = editing
        skillName.text = editing ? (root.preview.name || "") : ""
        skillTitle.text = editing ? (root.preview.title || "") : ""
        skillDescription.text = editing ? (root.preview.description || "") : ""
        includeQuestions.checked = editing ? Boolean(root.preview.includeQuestions) : true
        includeMistakes.checked = editing ? Boolean(root.preview.includeMistakes) : true
        includeConversations.checked = editing ? Boolean(root.preview.includeConversations) : true
        includeGrowth.checked = editing ? Boolean(root.preview.includeGrowth) : true
        createDialog.open()
    }
    function reload() { skillItems = bridge.skills(); sourceItems = bridge.sources() }
    Component.onCompleted: reload()
    Connections { target: bridge; function onSkillsChanged() { root.reload() } function onSourcesChanged() { root.reload() } }

    RowLayout {
        anchors.fill: parent; spacing: 16
        AppCard {
            Layout.preferredWidth: 360; Layout.fillHeight: true; hoverable: false
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 16; spacing: 12
                SectionTitle { Layout.fillWidth: true; title: "Learning Skills"; subtitle: "知识、错题、对话与成长沉淀" }
                PrimaryButton { Layout.fillWidth: true; text: "创建 Skill"; onClicked: root.openEditor(false) }
                ListView {
                    id: skillList; Layout.fillWidth: true; Layout.fillHeight: true; model: root.skillItems; spacing: 8; clip: true
                    ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: SelectableSurface {
                        required property var modelData
                        width: skillList.width; height: 82; surfaceRadius: 13
                        selected: root.selectedId === modelData.id
                        onActivated: { root.selectedId = modelData.id; root.preview = bridge.skillPreview(modelData.id) }
                        Column { anchors.fill: parent; anchors.margins: 13; spacing: 5
                            Row { width: parent.width; spacing: 8; Text { text: modelData.title; color: Theme.text; font.pixelSize: 14; font.weight: Font.DemiBold } Text { text: modelData.status; color: modelData.status === "最新" ? Theme.sage : Theme.accent; font.pixelSize: 10 } }
                            Text { width: parent.width; text: modelData.sourceCount + " 份知识 · " + modelData.questionCount + " 道题 · v" + (modelData.version || "-"); elide: Text.ElideRight; color: Theme.textFaint; font.pixelSize: 11 }
                            Text { width: parent.width; text: modelData.description; elide: Text.ElideRight; color: Theme.textMuted; font.pixelSize: 11 }
                        }
                    }
                }
            }
        }
        AppCard {
            Layout.fillWidth: true; Layout.fillHeight: true; hoverable: false
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 24; spacing: 14
                Text { text: root.preview.title || "选择一个 Skill"; color: Theme.text; font.pixelSize: 25; font.weight: Font.Bold }
                Text { visible: root.preview.name !== undefined; text: root.preview.name || ""; color: Theme.accent; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text { Layout.fillWidth: true; text: root.preview.description || "查看、删除和导出已经沉淀的学习能力。"; wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 14; lineHeight: 1.4 }
                GridLayout {
                    visible: root.selectedId > 0; Layout.fillWidth: true; columns: 4; columnSpacing: 10
                    Repeater { model: [{label:"题目", value:root.preview.questionCount || 0}, {label:"历史错题", value:root.preview.historicalMistakes || 0}, {label:"薄弱项", value:root.preview.weakQuestions || 0}, {label:"成长值", value:root.preview.growthScore || 0}]
                        Rectangle { required property var modelData; Layout.fillWidth: true; height: 82; radius: 18; color: "#58ffffff"; border.color: Theme.border; Column { anchors.centerIn: parent; spacing: 5; Text { anchors.horizontalCenter: parent.horizontalCenter; text: modelData.value; color: Theme.text; font.pixelSize: 22; font.weight: Font.Bold } Text { anchors.horizontalCenter: parent.horizontalCenter; text: modelData.label; color: Theme.textMuted; font.pixelSize: 11 } } }
                    }
                }
                Text { visible: root.selectedId > 0; text: "知识来源"; color: "#303936"; font.pixelSize: 16; font.weight: Font.DemiBold }
                Repeater { model: root.preview.sources || []; Text { required property string modelData; text: "•  " + modelData; color: "#68716e"; font.pixelSize: 13 } }
                Item { Layout.fillHeight: true }
                RowLayout { Layout.fillWidth: true; visible: root.selectedId > 0; PrimaryButton { text: "删除"; danger: true; onClicked: bridge.deleteSkill(root.selectedId) } PrimaryButton { text: "编辑"; secondary: true; onClicked: root.openEditor(true) } Item { Layout.fillWidth: true } PrimaryButton { text: "导出 Skill"; onClicked: bridge.exportSkill(root.selectedId) } }
            }
        }
    }

    Dialog {
        id: createDialog; property bool editing: false; modal: true; width: 560; height: 650; anchors.centerIn: parent; title: editing ? "编辑 Learning Skill" : "创建 Learning Skill"; standardButtons: Dialog.NoButton
        background: Rectangle { color: Theme.glassStrong; radius: Theme.cardRadius; border.color: Theme.border }
        contentItem: Flickable {
            contentWidth: width; contentHeight: form.implicitHeight; clip: true
            ColumnLayout {
                id: form; width: parent.width; spacing: 10
                GlassTextField { id: skillName; Layout.fillWidth: true; placeholderText: "标识，例如 algorithm-review" }
                GlassTextField { id: skillTitle; Layout.fillWidth: true; placeholderText: "显示名称" }
                GlassTextArea { id: skillDescription; Layout.fillWidth: true; Layout.preferredHeight: 90; placeholderText: "用途描述" }
                Text { text: "知识来源"; font.weight: Font.DemiBold }
                Repeater { model: root.sourceItems
                    GlassCheckBox { required property var modelData; text: modelData.title; property int sourceId: modelData.id; checked: createDialog.editing && (root.preview.sourceIds || []).indexOf(sourceId) >= 0 }
                }
                GlassCheckBox { id: includeQuestions; text: "包含完整题库"; checked: true }
                GlassCheckBox { id: includeMistakes; text: "包含错题纠正"; checked: true }
                GlassCheckBox { id: includeConversations; text: "包含对话洞察"; checked: true }
                GlassCheckBox { id: includeGrowth; text: "包含成长画像"; checked: true }
                RowLayout { Layout.fillWidth: true; PrimaryButton { text: "取消"; secondary: true; onClicked: createDialog.close() } Item { Layout.fillWidth: true } PrimaryButton { text: createDialog.editing ? "保存" : "创建"; onClicked: { var ids=[]; for (var i=0; i<form.children.length; i++) { var item=form.children[i]; if (item.sourceId !== undefined && item.checked) ids.push(item.sourceId) } if (createDialog.editing) bridge.updateSkill(root.selectedId, skillName.text, skillTitle.text, skillDescription.text, ids, includeQuestions.checked, includeMistakes.checked, includeConversations.checked, includeGrowth.checked); else bridge.createSkill(skillName.text, skillTitle.text, skillDescription.text, ids, includeQuestions.checked, includeMistakes.checked, includeConversations.checked, includeGrowth.checked); createDialog.close() } } }
            }
        }
    }
}
