import QtQuick
import QtQuick.Controls
import QtQuick.Effects

Item {
    id: root

    property string messageRole: "assistant"
    property string body: ""
    property string deliveryState: "done"

    readonly property bool fromUser: messageRole === "user"
    readonly property bool waiting: !fromUser && body.length === 0
            && (deliveryState === "waiting" || deliveryState === "streaming")
    readonly property bool failed: deliveryState === "error"

    implicitHeight: bubble.implicitHeight
    height: implicitHeight

    TextMetrics {
        id: bodyMetrics
        text: root.body.length > 0 ? root.body : "正在思考"
        font.pixelSize: 14
        font.family: Qt.application.font.family
    }

    Rectangle {
        id: bubble
        anchors.left: root.fromUser ? undefined : parent.left
        anchors.right: root.fromUser ? parent.right : undefined
        width: Math.min(
            root.width * (root.fromUser ? 0.72 : 0.82),
            Math.max(root.fromUser ? 148 : 210, bodyMetrics.advanceWidth + 46)
        )
        implicitHeight: contentColumn.implicitHeight + 26
        height: implicitHeight
        radius: 19
        border.width: 1
        border.color: root.failed
                ? "#58b85f59"
                : root.fromUser ? "#68df7845" : "#c8ffffff"
        gradient: Gradient {
            GradientStop {
                position: 0
                color: root.failed
                        ? "#edf7e6e3"
                        : root.fromUser ? "#f4edd3c7" : "#f2ffffff"
            }
            GradientStop {
                position: 1
                color: root.failed
                        ? "#dff2dbd8"
                        : root.fromUser ? "#dfefc7b9" : "#d9ffffff"
            }
        }
        scale: hoverHandler.hovered ? 1.006 : 1
        transform: Translate {
            y: hoverHandler.hovered ? -2 : 0
            Behavior on y {
                NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic }
            }
        }
        layer.enabled: true
        layer.smooth: true
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: root.fromUser ? "#263f2e24" : "#20382f25"
            shadowBlur: hoverHandler.hovered ? 0.72 : 0.52
            shadowVerticalOffset: hoverHandler.hovered ? 7 : 4
        }

        Behavior on scale {
            NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic }
        }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: bubble.radius
            anchors.rightMargin: bubble.radius
            height: 1
            color: "#eaffffff"
        }

        Column {
            id: contentColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 13
            spacing: 7

            Row {
                spacing: 7

                Rectangle {
                    width: 7
                    height: 7
                    radius: 4
                    anchors.verticalCenter: parent.verticalCenter
                    color: root.failed ? Theme.danger : root.fromUser ? Theme.accent : Theme.sage
                }

                Text {
                    text: root.fromUser
                            ? "你"
                            : root.failed ? "Bongo · 回答失败" : "Bongo"
                    color: root.failed ? Theme.danger : root.fromUser ? Theme.accent : Theme.textMuted
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.letterSpacing: 0.3
                }
            }

            TextEdit {
                id: messageText
                visible: !root.waiting
                width: parent.width
                height: contentHeight
                text: root.body
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                textFormat: TextEdit.MarkdownText
                color: Theme.text
                selectionColor: Theme.accentSoft
                selectedTextColor: Theme.text
                font.pixelSize: 14
            }

            Row {
                id: loadingRow
                visible: root.waiting
                height: 22
                spacing: 6

                Text {
                    text: "正在思考"
                    anchors.verticalCenter: parent.verticalCenter
                    color: Theme.textMuted
                    font.pixelSize: 13
                }

                Repeater {
                    model: 3

                    Rectangle {
                        required property int index
                        width: 5
                        height: 5
                        radius: 3
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.accent

                        SequentialAnimation on opacity {
                            running: loadingRow.visible
                            loops: Animation.Infinite
                            PauseAnimation { duration: index * 140 }
                            NumberAnimation { to: 0.28; duration: 320; easing.type: Easing.InOutSine }
                            NumberAnimation { to: 1; duration: 320; easing.type: Easing.InOutSine }
                            PauseAnimation { duration: (2 - index) * 140 }
                        }
                    }
                }
            }
        }

        HoverHandler { id: hoverHandler }
    }

    NumberAnimation on opacity {
        from: 0
        to: 1
        duration: Theme.motionNormal
    }
}
