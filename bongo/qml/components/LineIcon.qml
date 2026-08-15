import QtQuick
import QtQuick.Effects

Item {
    id: root
    property url source
    property color color: Theme.graphite

    Image {
        id: iconSource
        anchors.fill: parent
        source: root.source
        fillMode: Image.PreserveAspectFit
        sourceSize.width: Math.max(24, root.width * 2)
        sourceSize.height: Math.max(24, root.height * 2)
        visible: false
        smooth: true
        mipmap: true
    }
    MultiEffect {
        anchors.fill: parent
        source: iconSource
        colorization: 1
        colorizationColor: root.color
    }
}
