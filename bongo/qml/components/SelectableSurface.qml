import QtQuick
import QtQuick.Controls
import QtQuick.Effects

Button {
    id: surface
    property bool selected: false
    property bool selectable: true
    property color restingColor: "#55ffffff"
    property color hoverColor: Theme.accentSoft
    property color selectedColor: "#3ddf7845"
    property real surfaceRadius: 14
    property real hoverScale: 1.008
    property real hoverLift: 2
    default property alias content: contentLayer.data
    signal activated()

    hoverEnabled: true
    padding: 0
    focusPolicy: Qt.NoFocus
    onClicked: {
        if (selectable)
            activated()
    }

    background: Rectangle {
        id: visual
        radius: surface.surfaceRadius
        color: surface.selected ? surface.selectedColor : surface.hovered ? surface.hoverColor : surface.restingColor
        border.width: surface.selected ? 2 : 1
        border.color: surface.selected ? "#8adf7845" : surface.hovered ? "#62df7845" : Theme.border
        scale: surface.down ? 0.988 : surface.hovered ? surface.hoverScale : 1
        transform: Translate {
            y: surface.hovered ? -surface.hoverLift : 0
            Behavior on y { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }
        }
        layer.enabled: true
        layer.smooth: true
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: surface.selected ? "#323f2d24" : surface.hovered ? "#283f342d" : "#143f342d"
            shadowBlur: surface.selected || surface.hovered ? 0.68 : 0.38
            shadowVerticalOffset: surface.hovered ? 6 : 3
        }

        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on scale { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }

        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.margins: 7
            width: 4
            radius: 2
            color: Theme.accent
            opacity: surface.selected ? 1 : 0
            scale: surface.selected ? 1 : 0.45
            Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
            Behavior on scale { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutBack } }
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: surface.surfaceRadius
            anchors.rightMargin: surface.surfaceRadius
            height: 1
            color: surface.hovered || surface.selected ? "#f4ffffff" : "#b8ffffff"
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }

        Rectangle {
            width: Math.min(110, surface.width * 0.34)
            height: width
            radius: width / 2
            x: surface.width - width * 0.72
            y: -height * 0.38
            color: surface.selected ? "#20df7845" : surface.hovered ? "#13df7845" : "#05df7845"
            layer.enabled: true
            layer.effect: MultiEffect { blurEnabled: true; blur: 1; blurMax: 42 }
            Behavior on color { ColorAnimation { duration: Theme.motionNormal } }
        }
    }

    contentItem: Item { id: contentLayer }
}
