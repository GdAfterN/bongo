import QtQuick
import QtQuick.Controls

Switch {
    id: control
    implicitHeight: 34
    spacing: 10
    hoverEnabled: true
    scale: control.down ? 0.98 : control.hovered ? 1.012 : 1
    Behavior on scale { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }
    background: Rectangle {
        x: -8
        width: control.width + 16
        height: control.height
        radius: height / 2
        color: control.hovered ? Theme.accentSoft : "transparent"
        border.color: control.checked ? "#35df7845" : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }
    indicator: Rectangle {
        x: 0
        y: (control.height - height) / 2
        width: 42
        height: 24
        radius: 12
        color: control.checked ? Theme.accent : "#dedbd4"
        border.color: control.checked ? Theme.accent : "#c9c5bd"
        Rectangle {
            width: 18
            height: 18
            radius: 9
            y: 3
            x: control.checked ? parent.width - width - 3 : 3
            color: "#fffaf3"
            Behavior on x { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }
        }
        Behavior on color { ColorAnimation { duration: Theme.motionNormal } }
    }
    contentItem: Text {
        leftPadding: control.indicator.width + control.spacing
        text: control.text
        color: control.hovered ? Theme.accent : Theme.text
        font.pixelSize: 13
        verticalAlignment: Text.AlignVCenter
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }
}
