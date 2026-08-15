import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property var sourceItems: []
    property var question: ({})
    property var counts: ({all:0, wrong:0, unanswered:0})
    property string mode: "all"
    property int sourceId: 0
    property int selectedOption: -1
    property string feedback: ""
    property color feedbackColor: "#68706d"

    function reloadSources() { sourceItems = [{id:0, title:"全部知识"}].concat(bridge.sources()); counts = bridge.practiceCounts(sourceId) }
    function load(advance) { selectedOption = -1; feedback = ""; question = bridge.nextPractice(sourceId, mode, advance); counts = bridge.practiceCounts(sourceId) }
    Component.onCompleted: { reloadSources(); load(false) }
    Connections { target: bridge; function onPracticeChanged() { root.counts = bridge.practiceCounts(root.sourceId) } function onSourcesChanged() { root.reloadSources() } }

    ColumnLayout {
        anchors.fill: parent; spacing: 16
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout { Text { text: "智能练习"; color: Theme.text; font.pixelSize: 27; font.weight: Font.Bold } Text { text: "错题、未回答和普通题目使用同一套可追踪练习链路"; color: Theme.textMuted; font.pixelSize: 13 } }
            Item { Layout.fillWidth: true }
            GlassComboBox {
                model: root.sourceItems; textRole: "title"; valueRole: "id"; implicitWidth: 230
                onActivated: { root.sourceId = currentValue; root.load(false) }
            }
        }
        RowLayout {
            spacing: 8
            Repeater {
                model: [{key:"all", label:"全部题目"}, {key:"wrong", label:"错题复习"}, {key:"unanswered", label:"未回答"}]
                PrimaryButton { required property var modelData; text: modelData.label + " (" + (root.counts[modelData.key] || 0) + ")"; secondary: root.mode !== modelData.key; onClicked: { root.mode = modelData.key; root.load(false) } }
            }
        }
        AppCard {
            Layout.fillWidth: true; Layout.fillHeight: true; hoverable: false
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 28; spacing: 15
                Text { text: root.question.title || "尚未加载题目"; color: Theme.accent; font.pixelSize: 13; font.weight: Font.DemiBold }
                Text { Layout.fillWidth: true; text: root.question.prompt || "请先导入知识资料。"; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: 20; font.weight: Font.DemiBold; lineHeight: 1.35 }
                Repeater {
                    model: root.question.options || []
                    SelectableSurface {
                        required property string modelData
                        required property int index
                        Layout.fillWidth: true; implicitHeight: optionText.implicitHeight + 28; surfaceRadius: 13
                        selected: root.selectedOption === index
                        onActivated: root.selectedOption = index
                        Text { id: optionText; anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.margins: 16; text: String.fromCharCode(65 + index) + ". " + modelData; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: 14 }
                    }
                }
                Text { Layout.fillWidth: true; visible: root.feedback.length > 0; text: root.feedback; color: root.feedbackColor; wrapMode: Text.Wrap; font.pixelSize: 14; lineHeight: 1.45 }
                Item { Layout.fillHeight: true }
                RowLayout {
                    Layout.alignment: Qt.AlignRight
                    PrimaryButton { text: "换一题"; secondary: true; onClicked: root.load(true) }
                    PrimaryButton {
                        text: "提交答案"; enabled: root.question.id > 0 && root.selectedOption >= 0
                        onClicked: {
                            var result = bridge.answerPractice(root.question.id, root.selectedOption)
                            root.feedbackColor = result.correct ? "#16805c" : "#b3443e"
                            root.feedback = (result.correct ? "回答正确。" : "回答错误，正确答案是 " + String.fromCharCode(65 + result.correctIndex) + "。") + "\n" + result.explanation
                            root.counts = bridge.practiceCounts(root.sourceId)
                        }
                    }
                }
            }
        }
    }
}
