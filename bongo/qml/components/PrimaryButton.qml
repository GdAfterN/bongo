import QtQuick
import QtQuick.Controls
import QtQuick.Effects

Button {
    id: control
    property bool secondary: false
    property bool danger: false
    implicitHeight: 40
    implicitWidth: Math.max(102, contentItem.implicitWidth + 34)
    hoverEnabled: true
    padding: 0
    scale: control.down ? 0.965 : control.hovered ? 1.018 : 1
    Behavior on scale { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }

    contentItem: Text {
        text: control.text
        color: control.danger
               ? Theme.danger
               : control.secondary
                 ? (control.hovered ? Theme.accent : Theme.text)
                 : "#fffaf3"
        opacity: control.enabled ? 1 : 0.45
        font.pixelSize: 13
        font.weight: Font.DemiBold
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }
    background: Rectangle {
        radius: height / 2
        color: control.danger
               ? (control.hovered ? "#24b85f59" : Theme.dangerSoft)
               : control.secondary
                 ? (control.hovered ? Theme.accentSoft : "#7dffffff")
                 : (control.hovered ? Theme.accentHover : Theme.accent)
        border.width: 1
        border.color: control.danger ? "#3db85f59" : control.hovered ? "#58df7845" : Theme.border
        layer.enabled: true
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: control.hovered ? "#3a9c4a25" : "#269c4a25"
            shadowBlur: control.hovered ? 0.68 : 0.42
            shadowVerticalOffset: control.hovered ? 5 : 3
        }
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }
}
