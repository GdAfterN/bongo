import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var points: []
    property color keyboardColor: "#d97a4c"
    property color workColor: "#7f9787"
    property real loadProgress: 0
    property int hoverIndex: -1
    property real plotLeft: 58
    property real plotRight: 58
    property real plotTop: 48
    property real plotBottom: 34

    function plotWidth() { return Math.max(1, width - plotLeft - plotRight) }
    function plotHeight() { return Math.max(1, height - plotTop - plotBottom) }
    function pointX(index) {
        if (!points || points.length <= 1) return plotLeft + plotWidth() / 2
        return plotLeft + plotWidth() * index / (points.length - 1)
    }
    function maximum(field) {
        var value = 1
        for (var i = 0; i < points.length; i++) value = Math.max(value, Number(points[i][field]) || 0)
        return value
    }
    function pointY(index, field) {
        return plotTop + plotHeight() - plotHeight() * (Number(points[index][field]) || 0) / maximum(field)
    }
    function updateHover(mouseX) {
        if (!points || points.length === 0 || mouseX < plotLeft || mouseX > width - plotRight) {
            hoverIndex = -1
            return
        }
        var ratio = (mouseX - plotLeft) / plotWidth()
        hoverIndex = points.length === 1 ? 0 : Math.max(0, Math.min(points.length - 1, Math.round(ratio * (points.length - 1))))
    }
    function compactNumber(value) {
        var number = Number(value) || 0
        if (number >= 10000) return (number / 10000).toFixed(number >= 100000 ? 0 : 1) + "万"
        if (number >= 1000) return (number / 1000).toFixed(number >= 10000 ? 0 : 1) + "k"
        return Math.round(number).toString()
    }
    function hourLabel(seconds) {
        var hours = Number(seconds) / 3600
        return hours >= 10 ? Math.round(hours) + "h" : hours.toFixed(1) + "h"
    }

    onPointsChanged: {
        loadProgress = 0
        loadAnimation.restart()
    }
    onLoadProgressChanged: canvas.requestPaint()
    onHoverIndexChanged: canvas.requestPaint()

    NumberAnimation {
        id: loadAnimation
        target: root
        property: "loadProgress"
        from: 0
        to: 1
        duration: 720
        easing.type: Easing.OutCubic
    }

    Row {
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.rightMargin: root.plotRight
        spacing: 18
        z: 3
        Row {
            spacing: 6
            Rectangle { width: 18; height: 3; radius: 2; color: root.keyboardColor; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "键盘敲击数"; color: Theme.textMuted; font.pixelSize: 11 }
        }
        Row {
            spacing: 6
            Rectangle { width: 18; height: 3; radius: 2; color: root.workColor; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "工作时间"; color: Theme.textMuted; font.pixelSize: 11 }
        }
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var chartWidth = root.plotWidth()
            var chartHeight = root.plotHeight()
            var count = root.points ? root.points.length : 0
            var keyMaximum = root.maximum("keys")
            var workMaximum = root.maximum("workSeconds")

            ctx.font = "10px 'Microsoft YaHei UI'"
            ctx.lineWidth = 1
            for (var grid = 0; grid < 4; grid++) {
                var ratio = grid / 3
                var gridY = root.plotTop + chartHeight * ratio
                ctx.strokeStyle = "#ddd8cf"
                ctx.beginPath()
                ctx.moveTo(root.plotLeft, gridY)
                ctx.lineTo(root.plotLeft + chartWidth, gridY)
                ctx.stroke()
                ctx.fillStyle = "#96958f"
                var keyText = root.compactNumber(keyMaximum * (1 - ratio))
                ctx.fillText(keyText, root.plotLeft - ctx.measureText(keyText).width - 9, gridY + 4)
                var workText = root.hourLabel(workMaximum * (1 - ratio))
                ctx.fillText(workText, root.plotLeft + chartWidth + 9, gridY + 4)
            }

            if (count === 0) {
                ctx.fillStyle = "#96958f"
                ctx.fillText("暂无活动数据", root.plotLeft + chartWidth / 2 - 30, root.plotTop + chartHeight / 2)
                return
            }

            var fields = ["keys", "workSeconds"]
            var colors = [root.keyboardColor, root.workColor]
            for (var series = 0; series < fields.length; series++) {
                ctx.globalAlpha = 0.055 * root.loadProgress
                ctx.beginPath()
                ctx.moveTo(root.pointX(0), root.plotTop + chartHeight)
                for (var i = 0; i < count; i++) {
                    var x = root.pointX(i)
                    var y = root.pointY(i, fields[series])
                    ctx.lineTo(x, y)
                }
                ctx.lineTo(root.pointX(count - 1), root.plotTop + chartHeight)
                ctx.closePath()
                ctx.fillStyle = colors[series]
                ctx.fill()
                ctx.globalAlpha = root.loadProgress
                ctx.beginPath()
                for (i = 0; i < count; i++) {
                    x = root.pointX(i)
                    y = root.pointY(i, fields[series])
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                }
                ctx.strokeStyle = colors[series]
                ctx.lineWidth = 2.4
                ctx.lineJoin = "round"
                ctx.lineCap = "round"
                ctx.stroke()
            }
            ctx.globalAlpha = 1

            ctx.fillStyle = "#8c8d87"
            for (i = 0; i < count; i++) {
                x = root.pointX(i)
                var label = String(root.points[i].label)
                ctx.fillText(label, x - ctx.measureText(label).width / 2, root.plotTop + chartHeight + 22)
            }

            if (root.hoverIndex >= 0 && root.hoverIndex < count) {
                x = root.pointX(root.hoverIndex)
                ctx.strokeStyle = "#aaa69e"
                ctx.lineWidth = 1
                ctx.setLineDash([4, 4])
                ctx.beginPath()
                ctx.moveTo(x, root.plotTop)
                ctx.lineTo(x, root.plotTop + chartHeight)
                ctx.stroke()
                ctx.setLineDash([])
                for (series = 0; series < fields.length; series++) {
                    y = root.pointY(root.hoverIndex, fields[series])
                    ctx.fillStyle = "#f8f4ed"
                    ctx.strokeStyle = colors[series]
                    ctx.lineWidth = 3
                    ctx.beginPath()
                    ctx.arc(x, y, 5, 0, Math.PI * 2)
                    ctx.fill()
                    ctx.stroke()
                }
            }
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
        onPositionChanged: function(mouse) { root.updateHover(mouse.x) }
        onExited: root.hoverIndex = -1
    }

    Rectangle {
        id: tooltip
        z: 5
        visible: root.hoverIndex >= 0 && root.hoverIndex < root.points.length
        opacity: visible ? 1 : 0
        width: 220
        height: 92
        radius: 16
        color: "#f5ffffff"
        border.color: Theme.border
        x: Math.max(8, Math.min(root.width - width - 8, root.pointX(root.hoverIndex) - width / 2))
        y: 6
        Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        Behavior on opacity { NumberAnimation { duration: 150 } }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 5
            Text {
                text: tooltip.visible ? root.points[root.hoverIndex].dateLabel + (root.points[root.hoverIndex].simulated ? " · 示例补全" : "") : ""
                color: Theme.text
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            RowLayout {
                Rectangle { width: 8; height: 8; radius: 4; color: root.keyboardColor }
                Text { text: "键盘敲击"; color: Theme.textMuted; font.pixelSize: 11 }
                Item { Layout.fillWidth: true }
                Text { text: tooltip.visible ? Number(root.points[root.hoverIndex].keys).toLocaleString(Qt.locale("zh_CN"), "f", 0) + " 次" : ""; color: root.keyboardColor; font.pixelSize: 13; font.weight: Font.Bold }
            }
            RowLayout {
                Rectangle { width: 8; height: 8; radius: 4; color: root.workColor }
                Text { text: "工作时间"; color: Theme.textMuted; font.pixelSize: 11 }
                Item { Layout.fillWidth: true }
                Text { text: tooltip.visible ? root.points[root.hoverIndex].workLabel : ""; color: root.workColor; font.pixelSize: 13; font.weight: Font.Bold }
            }
        }
    }
}
