import QtQuick
import QtQuick.Controls
import QtQuick.Effects

Rectangle {
    id: card
    property bool hoverable: true
    property color baseColor: Theme.glass
    property color borderColor: Theme.border
    property color glowColor: Theme.accent
    property real cardRadius: Theme.cardRadius
    property real frostStrength: 1
    property real hoverScale: 1.006
    property real hoverLift: 3
    property bool hovered: hoverHandler.hovered
    default property alias content: contentItem.data

    color: baseColor
    radius: cardRadius
    border.color: hovered && hoverable ? "#c8ffffff" : borderColor
    border.width: 1
    scale: hovered && hoverable ? hoverScale : 1
    transform: Translate {
        id: liftTransform
        y: card.hovered && card.hoverable ? -card.hoverLift : 0
        Behavior on y { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }
    }
    layer.enabled: true
    layer.smooth: true
    layer.effect: MultiEffect {
        shadowEnabled: true
        shadowColor: card.hovered && card.hoverable ? "#3d382f25" : "#26382f25"
        shadowBlur: card.hovered && card.hoverable ? 0.82 : 0.62
        shadowVerticalOffset: card.hovered && card.hoverable ? 10 : 6
        shadowHorizontalOffset: 0
    }

    Behavior on scale { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }
    Behavior on border.color { ColorAnimation { duration: Theme.motionNormal } }
    Behavior on baseColor { ColorAnimation { duration: Theme.motionNormal } }

    HoverHandler { id: hoverHandler; enabled: card.hoverable }

    Rectangle {
        anchors.fill: parent
        radius: card.cardRadius
        gradient: Gradient {
            GradientStop { position: 0; color: Qt.rgba(1, 1, 1, Math.min(0.78, (card.hovered && card.hoverable ? 0.45 : 0.29) * card.frostStrength)) }
            GradientStop { position: 0.48; color: Qt.rgba(1, 1, 1, Math.min(0.38, 0.094 * card.frostStrength)) }
            GradientStop { position: 1; color: Qt.rgba(1, 1, 1, Math.min(0.18, 0.02 * card.frostStrength)) }
        }
    }

    Rectangle {
        width: Math.min(260, card.width * 0.42)
        height: width
        radius: width / 2
        x: card.width - width * 0.72
        y: card.height - height * 0.62
        color: Qt.rgba(card.glowColor.r, card.glowColor.g, card.glowColor.b, Math.min(0.18, (card.hovered && card.hoverable ? 0.10 : 0.045) * card.frostStrength))
        layer.enabled: true
        layer.effect: MultiEffect { blurEnabled: true; blur: 1; blurMax: 64 }
        Behavior on color { ColorAnimation { duration: 320 } }
    }

    Rectangle {
        width: Math.min(210, card.width * 0.36)
        height: width
        radius: width / 2
        x: Math.max(-width * 0.42, Math.min(card.width - width * 0.58, hoverHandler.point.position.x - width / 2))
        y: Math.max(-height * 0.42, Math.min(card.height - height * 0.58, hoverHandler.point.position.y - height / 2))
        color: Qt.rgba(1, 1, 1, Math.min(0.42, 0.26 * card.frostStrength))
        opacity: card.hovered && card.hoverable ? 1 : 0
        layer.enabled: true
        layer.effect: MultiEffect { blurEnabled: true; blur: 1; blurMax: 64 }
        Behavior on x { NumberAnimation { duration: 90; easing.type: Easing.OutQuad } }
        Behavior on y { NumberAnimation { duration: 90; easing.type: Easing.OutQuad } }
        Behavior on opacity { NumberAnimation { duration: Theme.motionNormal } }
    }

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: card.cardRadius
        anchors.rightMargin: card.cardRadius
        height: 1
        color: card.hovered && card.hoverable ? "#efffffff" : "#c8ffffff"
        Behavior on color { ColorAnimation { duration: Theme.motionNormal } }
    }

    Item {
        id: contentItem
        anchors.fill: parent
    }
}
