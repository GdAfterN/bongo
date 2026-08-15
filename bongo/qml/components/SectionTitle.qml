import QtQuick
import QtQuick.Layouts

RowLayout {
    id: root
    property string title: ""
    property string subtitle: ""
    property alias trailing: trailingItem.data
    ColumnLayout {
        spacing: 3
        Text { text: root.title; color: Theme.text; font.pixelSize: 18; font.weight: Font.Bold }
        Text { visible: root.subtitle.length > 0; text: root.subtitle; color: Theme.textMuted; font.pixelSize: 12 }
    }
    Item { Layout.fillWidth: true }
    Item { id: trailingItem; implicitWidth: childrenRect.width; implicitHeight: childrenRect.height }
}
