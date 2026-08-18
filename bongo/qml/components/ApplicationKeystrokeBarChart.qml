import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    objectName: "applicationKeystrokeChart"
    property var points: []
    property real loadProgress: 0
    property int hoveredIndex: -1
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

    onPointsChanged: {
        hoveredIndex = -1
        playLoadAnimation()
    }

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
            objectName: "keystrokeRow" + index
            required property var modelData
            required property int index
            width: ranking.width - 10
            height: 40
            radius: 10
            color: isHovered ? "#70fff1e5" : "transparent"
            border.width: 1
            border.color: isHovered ? "#78df7845" : "transparent"
            scale: isHovered ? 1.008 : 1
            transformOrigin: Item.Center
            z: isHovered ? 1 : 0
            property color accent: root.accents[index % root.accents.length]
            property real progress: root.barProgress(index)
            property bool isHovered: root.hoveredIndex === index
            Behavior on color { ColorAnimation { duration: 160 } }
            Behavior on border.color { ColorAnimation { duration: 160 } }
            Behavior on scale { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

            Rectangle {
                anchors.left: parent.left
                anchors.leftMargin: 2
                anchors.verticalCenter: parent.verticalCenter
                width: 3
                height: row.isHovered ? 22 : 8
                radius: 2
                color: row.accent
                opacity: row.isHovered ? 1 : 0
                Behavior on height { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                Behavior on opacity { NumberAnimation { duration: 140 } }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 8
                anchors.rightMargin: 7
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
                    color: row.isHovered ? Theme.text : Theme.textMuted
                    font.pixelSize: 11
                    font.weight: row.isHovered ? Font.DemiBold : Font.Normal
                    Behavior on color { ColorAnimation { duration: 150 } }
                }
                Rectangle {
                    id: track
                    Layout.fillWidth: true
                    height: row.isHovered ? 22 : 18
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
                Rectangle {
                    Layout.preferredWidth: 70
                    height: 28
                    radius: 14
                    color: row.isHovered ? "#eafffaf5" : "transparent"
                    border.width: 1
                    border.color: row.isHovered ? Qt.rgba(row.accent.r, row.accent.g, row.accent.b, 0.48) : "transparent"
                    opacity: row.progress
                    Behavior on color { ColorAnimation { duration: 160 } }
                    Behavior on border.color { ColorAnimation { duration: 160 } }

                    Text {
                        anchors.centerIn: parent
                        text: modelData.valueLabel + (row.isHovered ? " 次" : "")
                        color: row.accent
                        font.pixelSize: 11
                        font.weight: Font.Bold
                    }
                }
            }

            HoverHandler {
                id: hover
                onHoveredChanged: {
                    if (hovered)
                        root.hoveredIndex = row.index
                    else if (root.hoveredIndex === row.index)
                        root.hoveredIndex = -1
                }
            }
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
