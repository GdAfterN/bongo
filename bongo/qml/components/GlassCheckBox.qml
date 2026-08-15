import QtQuick
import QtQuick.Controls

CheckBox {
    id: control
    implicitHeight: 32
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
        border.color: control.checked ? "#32df7845" : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }
    indicator: Rectangle {
        x: 0
        y: (control.height - height) / 2
        width: 21
        height: 21
        radius: 7
        color: control.checked ? Theme.accent : Theme.glassStrong
        border.color: control.checked ? Theme.accent : control.hovered ? "#62df7845" : Theme.divider
        LineIcon {
            anchors.centerIn: parent
            width: 13
            height: 13
            source: Qt.resolvedUrl("../../assets/icons/check.svg")
            color: "#fffaf3"
            opacity: control.checked ? 1 : 0
            scale: control.checked ? 1 : 0.6
            Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
            Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutBack } }
        }
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
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
