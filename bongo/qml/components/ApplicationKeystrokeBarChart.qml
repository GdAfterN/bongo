import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var points: []
    property real loadProgress: 0
    property var accents: ["#c9653d", "#d5784d", "#df8b61", "#e69c77", "#edad8d", "#efbda3"]

    function maximum() {
        return points && points.length > 0 ? Math.max(1, Number(points[0].value) || 0) : 1
    }
    function barProgress(index) {
        var staggerRange = 1 + Math.max(0, points.length - 1) * 0.09
        return Math.max(0, Math.min(1, loadProgress * staggerRange - index * 0.09))
    }
    function playLoadAnimation() {
        loadProgress = 0
        loadAnimation.restart()
    }

    onPointsChanged: playLoadAnimation()

    NumberAnimation {
        id: loadAnimation
        target: root
        property: "loadProgress"
        from: 0
        to: 1
        duration: 1050
        easing.type: Easing.OutCubic
    }
    ListView {
        id: ranking
        anchors.fill: parent
        clip: true
        spacing: 5
        model: root.points
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }

        delegate: Rectangle {
            id: row
            required property var modelData
            required property int index
            width: ranking.width - 10
            height: 40
            radius: 10
            color: hover.hovered ? "#58ffffff" : "transparent"
            scale: hover.hovered ? 1.006 : 1
            transformOrigin: Item.Center
            property color accent: root.accents[index % root.accents.length]
            property real progress: root.barProgress(index)
            Behavior on color { ColorAnimation { duration: 160 } }
            Behavior on scale { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 5
                anchors.rightMargin: 5
                spacing: 7

                Rectangle {
                    width: 25
                    height: 25
                    radius: 13
                    color: index < 3 ? Qt.rgba(row.accent.r, row.accent.g, row.accent.b, 0.16) : "#62ffffff"
                    border.color: index < 3 ? Qt.rgba(row.accent.r, row.accent.g, row.accent.b, 0.42) : Theme.border
                    Text { anchors.centerIn: parent; text: index + 1; color: index < 3 ? row.accent : Theme.textMuted; font.pixelSize: 10; font.weight: Font.Bold }
                }
                Text {
                    Layout.preferredWidth: 90
                    text: modelData.label
                    elide: Text.ElideRight
                    color: hover.hovered ? Theme.text : Theme.textMuted
                    font.pixelSize: 11
                    font.weight: hover.hovered ? Font.DemiBold : Font.Normal
                    Behavior on color { ColorAnimation { duration: 150 } }
                }
                Rectangle {
                    id: track
                    Layout.fillWidth: true
                    height: hover.hovered ? 22 : 18
                    radius: 9
                    color: Theme.track
                    clip: true
                    Behavior on height { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

                    Rectangle {
                        id: fill
                        height: parent.height
                        width: row.progress <= 0 ? 0 : Math.max(4, parent.width * (Number(row.modelData.value) || 0) / root.maximum() * row.progress)
                        radius: 9
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0; color: row.accent }
                            GradientStop { position: 1; color: Qt.lighter(row.accent, 1.35) }
                        }
                    }
                }
                Text {
                    Layout.preferredWidth: 50
                    horizontalAlignment: Text.AlignRight
                    text: modelData.valueLabel
                    color: row.accent
                    font.pixelSize: 11
                    font.weight: Font.Bold
                    opacity: row.progress
                }
            }

            HoverHandler { id: hover }
            ToolTip.visible: hover.hovered
            ToolTip.delay: 120
            ToolTip.text: modelData.label + "\n键盘敲击 " + modelData.valueLabel + " 次"
        }

        Text {
            anchors.centerIn: parent
            visible: root.points.length === 0
            text: "今天还没有键盘敲击记录"
            color: "#9aa19e"
            font.pixelSize: 12
        }
    }
}
