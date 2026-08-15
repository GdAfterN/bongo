import QtQuick
import QtQuick.Controls

TextField {
    id: control
    implicitHeight: 42
    leftPadding: 16
    rightPadding: 16
    color: Theme.text
    placeholderTextColor: Theme.textFaint
    selectionColor: Theme.accentSoft
    selectedTextColor: Theme.text
    font.pixelSize: 13
    background: Rectangle {
        radius: height / 2
        color: control.activeFocus || control.hovered ? Theme.glassHover : Theme.glassStrong
        border.width: 1
        border.color: control.activeFocus ? "#72df7845" : control.hovered ? "#45df7845" : Theme.border
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }
}
