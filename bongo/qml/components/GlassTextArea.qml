import QtQuick
import QtQuick.Controls

TextArea {
    id: control
    leftPadding: 15
    rightPadding: 15
    topPadding: 12
    bottomPadding: 12
    color: Theme.text
    placeholderTextColor: Theme.textFaint
    selectionColor: Theme.accentSoft
    selectedTextColor: Theme.text
    font.pixelSize: 13
    wrapMode: TextArea.Wrap
    background: Rectangle {
        radius: 18
        color: control.activeFocus || control.hovered ? Theme.glassHover : Theme.glassStrong
        border.width: 1
        border.color: control.activeFocus ? "#72df7845" : control.hovered ? "#45df7845" : Theme.border
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }
}
