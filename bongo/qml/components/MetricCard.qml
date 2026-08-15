import QtQuick
import QtQuick.Layouts

AppCard {
    id: root
    property string label: ""
    property string value: "-"
    property string suffix: ""
    property string symbol: "clock.svg"
    property color accent: Theme.accent
    property real shownValue: 0
    glowColor: root.accent
    hoverScale: 1.009
    hoverLift: 4
    implicitHeight: 126

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Text { text: root.label; color: Theme.textMuted; font.pixelSize: 13 }
            Item { Layout.fillWidth: true }
            Rectangle {
                width: 34; height: 34; radius: 17
                color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.11)
                LineIcon { anchors.centerIn: parent; width: 18; height: 18; source: Qt.resolvedUrl("../../assets/icons/" + root.symbol); color: root.accent }
            }
        }
        RowLayout {
            spacing: 5
            Text { text: root.value; color: Theme.text; font.pixelSize: root.value.length > 8 ? 21 : 29; font.weight: Font.Bold }
            Text { text: root.suffix; color: Theme.textMuted; font.pixelSize: 12; Layout.alignment: Qt.AlignBottom; Layout.bottomMargin: 4 }
        }
    }
    SequentialAnimation on opacity {
        running: true
        NumberAnimation { from: 0; to: 1; duration: 420; easing.type: Easing.OutCubic }
    }
}
