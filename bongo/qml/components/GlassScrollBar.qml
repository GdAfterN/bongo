import QtQuick
import QtQuick.Controls

ScrollBar {
    id: control
    implicitWidth: 8
    implicitHeight: 8
    padding: 1
    contentItem: Rectangle {
        implicitWidth: 6
        implicitHeight: 6
        radius: 3
        color: control.pressed ? Theme.accent : control.hovered ? "#b0df7845" : "#74938f87"
        opacity: control.active ? 1 : 0.5
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
    }
    background: Item {}
}
