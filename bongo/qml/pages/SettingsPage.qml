import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property var config: ({})
    property int tabIndex: 0
    function reload() { config = bridge.settings(); applyConfig() }
    function applyConfig() {
        if (!config.provider) return
        providerCombo.currentIndex = Math.max(0, config.providers.indexOf(config.provider))
        modelInput.text = config.model || ""
        baseUrlInput.text = config.baseUrl || ""
        apiKeyInput.text = config.apiKey || ""
    }
    Component.onCompleted: reload()
    Connections { target: bridge; function onSettingsChanged() { root.reload() } }

    ColumnLayout {
        anchors.fill: parent; spacing: 16
        ColumnLayout { Text { text: "设置"; color: Theme.text; font.pixelSize: 27; font.weight: Font.Bold } Text { text: "模型、桌宠和匿名活动记录都只保存在本机"; color: Theme.textMuted; font.pixelSize: 13 } }
        RowLayout {
            spacing: 8
            Repeater { model: ["模型与对话", "桌宠", "活动记录"]
                PrimaryButton { required property string modelData; required property int index; text: modelData; secondary: root.tabIndex !== index; onClicked: root.tabIndex = index }
            }
        }
        AppCard {
            Layout.fillWidth: true; Layout.fillHeight: true; hoverable: false
            StackLayout {
                anchors.fill: parent; anchors.margins: 26; currentIndex: root.tabIndex
                Flickable {
                    contentWidth: width; contentHeight: modelForm.implicitHeight; clip: true
                    ColumnLayout {
                        id: modelForm; width: parent.width; spacing: 12
                        Text { text: "导入与出题模型"; color: Theme.text; font.pixelSize: 19; font.weight: Font.Bold }
                        Text { text: "使用官方 OpenAI / Anthropic SDK"; color: Theme.textMuted; font.pixelSize: 12 }
                        Label { text: "提供商"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassComboBox { id: providerCombo; Layout.fillWidth: true; model: root.config.providers || [] }
                        Label { text: "模型名称"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassTextField { id: modelInput; Layout.fillWidth: true; placeholderText: "留空使用后端默认值" }
                        Label { text: "API Key"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassTextField { id: apiKeyInput; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: "仅保存在本机 SQLite" }
                        Label { text: "兼容接口 Base URL"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassTextField { id: baseUrlInput; Layout.fillWidth: true; placeholderText: "可选" }
                        PrimaryButton { Layout.alignment: Qt.AlignRight; text: "保存模型设置"; onClicked: bridge.saveModelSettings(providerCombo.currentText, modelInput.text, baseUrlInput.text, apiKeyInput.text) }
                    }
                }
                Flickable {
                    contentWidth: width; contentHeight: petForm.implicitHeight; clip: true
                    ColumnLayout {
                        id: petForm; width: parent.width; spacing: 10
                        Text { text: "桌宠设置"; color: Theme.text; font.pixelSize: 19; font.weight: Font.Bold }
                        GlassSwitch { id: petVisible; text: "显示桌宠"; checked: root.config.petVisible || false }
                        GlassSwitch { id: petAlwaysTop; text: "窗口置顶"; checked: root.config.petAlwaysTop || false }
                        GlassSwitch { id: petPassThrough; text: "点击穿透（答题时自动暂停）"; checked: root.config.petPassThrough || false }
                        GlassSwitch { id: petKeepScreen; text: "保持在屏幕内"; checked: root.config.petKeepScreen || false }
                        GlassSwitch { id: petModelMirror; text: "模型镜像"; checked: root.config.petModelMirror || false }
                        GlassSwitch { id: petMouseMirror; text: "鼠标镜像"; checked: root.config.petMouseMirror || false }
                        GlassSwitch { id: petKeyboard; text: "响应键盘"; checked: root.config.petKeyboard || false }
                        GlassSwitch { id: petMouse; text: "响应鼠标"; checked: root.config.petMouse || false }
                        Label { text: "不透明度 · " + Math.round(opacitySlider.value) + "%"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassSlider { id: opacitySlider; Layout.fillWidth: true; from: 10; to: 100; value: root.config.petOpacity || 100 }
                        Label { text: "窗口尺寸 · " + Math.round(scaleSlider.value) + "%"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassSlider { id: scaleSlider; Layout.fillWidth: true; from: 50; to: 200; value: root.config.petScale || 100 }
                        Label { text: "气泡等待时间 · " + Math.round(timeoutSlider.value) + " 秒"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassSlider { id: timeoutSlider; Layout.fillWidth: true; from: 10; to: 300; value: root.config.questionTimeout || 45 }
                        Label { text: "显示器适配预设"; color: Theme.textMuted; font.pixelSize: 12; font.weight: Font.DemiBold }
                        GlassComboBox { id: displayProfile; Layout.fillWidth: true; model: [{label:"笔记本 2880×1800（200%）", value:"laptop_2880_200"}, {label:"2K 27 寸显示器", value:"desktop_2k_100"}]; textRole: "label"; valueRole: "value"; Component.onCompleted: currentIndex = root.config.displayProfile === "desktop_2k_100" ? 1 : 0 }
                        RowLayout { Layout.fillWidth: true; PrimaryButton { text: "立即显示桌宠"; secondary: true; onClicked: bridge.showPet() } Item { Layout.fillWidth: true } PrimaryButton { text: "应用桌宠设置"; onClicked: bridge.savePetSettings({visible:petVisible.checked, opacity:Math.round(opacitySlider.value), scale:Math.round(scaleSlider.value), alwaysTop:petAlwaysTop.checked, passThrough:petPassThrough.checked, keepScreen:petKeepScreen.checked, modelMirror:petModelMirror.checked, mouseMirror:petMouseMirror.checked, keyboard:petKeyboard.checked, mouse:petMouse.checked, questionTimeout:Math.round(timeoutSlider.value), displayProfile:displayProfile.currentValue}) } }
                    }
                }
                ColumnLayout {
                    spacing: 14
                    Text { text: "匿名活动记录"; color: Theme.text; font.pixelSize: 19; font.weight: Font.Bold }
                    Text { Layout.fillWidth: true; text: "仅保存前台应用进程名、键盘次数、鼠标活跃秒数和点击次数；不保存具体按键、输入内容、窗口标题和鼠标坐标。"; wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 14; lineHeight: 1.45 }
                    GlassSwitch { id: activityEnabled; text: "记录匿名键鼠活动"; checked: root.config.activityTracking || false; onToggled: bridge.saveActivitySettings(checked) }
                    Item { Layout.fillHeight: true }
                    PrimaryButton { Layout.alignment: Qt.AlignLeft; text: "清空活动历史"; danger: true; onClicked: bridge.clearActivityHistory() }
                }
            }
        }
    }
}
