import QtQuick
import QtQuick.Controls

ProgressBar {
    id: control
    implicitHeight: 7
    background: Rectangle { radius: height / 2; color: Theme.track }
    contentItem: Item {
        clip: true
        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: height / 2
            color: Theme.accent
            Behavior on width { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
        }
    }
}
