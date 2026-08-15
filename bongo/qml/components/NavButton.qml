import QtQuick
import QtQuick.Controls

Button {
    id: control
    property bool selected: false
    property url iconSource
    hoverEnabled: true
    implicitWidth: 44
    implicitHeight: 44
    padding: 0

    contentItem: Item {
        LineIcon {
            anchors.centerIn: parent
            width: 18
            height: 18
            source: control.iconSource
            color: control.selected ? "#fffaf3" : control.hovered ? Theme.accent : Theme.graphite
            scale: control.hovered ? 1.06 : 1
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
            Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        }
    }
    background: Rectangle {
        radius: width / 2
        color: control.selected ? Theme.accent : Theme.graphite
        opacity: control.selected || control.hovered ? 1 : 0
        scale: control.down ? 0.92 : control.selected || control.hovered ? 1 : 0.78
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
        Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
    }

    ToolTip.visible: control.hovered
    ToolTip.delay: 280
    ToolTip.timeout: 2400
    ToolTip.text: control.text
}
