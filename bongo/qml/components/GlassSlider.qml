import QtQuick
import QtQuick.Controls

Slider {
    id: control
    implicitHeight: 30
    background: Rectangle {
        x: control.leftPadding
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: control.availableWidth
        height: 5
        radius: 3
        color: Theme.track
        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: parent.radius
            color: Theme.accent
        }
    }
    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: control.hovered || control.pressed ? 20 : 18
        height: width
        radius: width / 2
        color: "#fffaf3"
        border.width: 2
        border.color: Theme.accent
        Behavior on width { NumberAnimation { duration: Theme.motionFast } }
    }
}
