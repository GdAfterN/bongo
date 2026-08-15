import QtQuick
import QtQuick.Controls
import QtQuick.Effects

ComboBox {
    id: control
    implicitHeight: 44
    implicitWidth: 190
    leftPadding: 16
    rightPadding: 42
    hoverEnabled: true
    scale: control.hovered || control.popup.visible ? 1.012 : 1
    Behavior on scale { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }

    delegate: ItemDelegate {
        id: option
        required property var modelData
        required property int index
        property bool current: index === control.currentIndex
        width: ListView.view ? ListView.view.width : control.width
        height: 40
        text: control.textRole ? String(modelData[control.textRole] || "") : String(modelData)
        highlighted: control.highlightedIndex === index
        hoverEnabled: true
        scale: option.down ? 0.98 : option.hovered ? 1.008 : 1
        Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        contentItem: Text {
            text: option.text
            color: option.current ? "#fffaf3" : option.highlighted || option.hovered ? Theme.accent : Theme.text
            font.pixelSize: 13
            font.weight: option.current ? Font.DemiBold : Font.Normal
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }
        background: Rectangle {
            radius: 14
            color: option.current ? Theme.accent : option.highlighted || option.hovered ? Theme.accentSoft : "transparent"
            border.width: option.current ? 1 : 0
            border.color: option.current ? "#72ffffff" : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }
    }

    indicator: Item {
        x: control.width - width - 14
        y: (control.height - height) / 2
        width: 16
        height: 16
        LineIcon {
            anchors.fill: parent
            source: Qt.resolvedUrl("../../assets/icons/chevron-down.svg")
            color: control.hovered || control.visualFocus ? Theme.accent : Theme.graphite
            rotation: control.popup.visible ? 180 : 0
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
            Behavior on rotation { NumberAnimation { duration: Theme.motionNormal; easing.type: Easing.OutCubic } }
        }
    }

    contentItem: Text {
        text: control.displayText
        color: Theme.text
        font.pixelSize: 13
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: height / 2
        color: control.popup.visible || control.hovered ? Theme.glassHover : Theme.glassStrong
        border.width: 1
        border.color: control.popup.visible || control.visualFocus ? "#72df7845" : control.hovered ? "#45df7845" : Theme.border
        layer.enabled: true
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: control.popup.visible || control.hovered ? "#302f271f" : "#1a2f271f"
            shadowBlur: control.popup.visible || control.hovered ? 0.72 : 0.46
            shadowVerticalOffset: control.popup.visible || control.hovered ? 5 : 3
        }
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: parent.radius
            anchors.rightMargin: parent.radius
            height: 1
            color: control.popup.visible || control.hovered ? "#efffffff" : "#b8ffffff"
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }
        Rectangle {
            width: 72
            height: 72
            radius: 36
            x: parent.width - 50
            y: -22
            color: control.popup.visible || control.hovered ? "#18df7845" : "#08df7845"
            layer.enabled: true
            layer.effect: MultiEffect { blurEnabled: true; blur: 1; blurMax: 48 }
            Behavior on color { ColorAnimation { duration: Theme.motionNormal } }
        }
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }

    popup: Popup {
        y: control.height + 6
        width: control.width
        implicitHeight: Math.min(280, contentItem.implicitHeight + 12)
        padding: 6
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            spacing: 2
            ScrollBar.vertical: GlassScrollBar { policy: ScrollBar.AsNeeded }
        }
        background: Rectangle {
            radius: 18
            color: "#e8ffffff"
            border.color: "#c8ffffff"
            layer.enabled: true
            layer.effect: MultiEffect { shadowEnabled: true; shadowColor: "#302f271f"; shadowBlur: 0.75; shadowVerticalOffset: 7 }
        }
    }
}
