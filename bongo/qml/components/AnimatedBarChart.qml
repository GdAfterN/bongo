import QtQuick

Item {
    id: root
    property var points: []
    property color barColor: "#d97a4c"
    property real reveal: 0
    onPointsChanged: { reveal = 0; animation.restart(); canvas.requestPaint() }
    NumberAnimation { id: animation; target: root; property: "reveal"; from: 0; to: 1; duration: 850; easing.type: Easing.OutBack }
    onRevealChanged: canvas.requestPaint()

    Canvas {
        id: canvas
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d"); ctx.reset()
            var left = 36, right = 12, top = 18, bottom = 30
            var width = canvas.width - left - right, height = canvas.height - top - bottom
            ctx.font = "10px 'Segoe UI'"; ctx.strokeStyle = "#ddd8cf"; ctx.fillStyle = "#8c8d87"
            for (var grid = 0; grid < 4; grid++) {
                var y = top + height * grid / 3
                ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + width, y); ctx.stroke()
            }
            if (!root.points || root.points.length === 0) return
            var maximum = 1
            for (var i = 0; i < root.points.length; i++) maximum = Math.max(maximum, Number(root.points[i].value))
            var slot = width / root.points.length
            var barWidth = Math.min(24, slot * 0.56)
            for (i = 0; i < root.points.length; i++) {
                var value = Number(root.points[i].value)
                var barHeight = height * value / maximum * root.reveal
                var x = left + i * slot + (slot - barWidth) / 2
                y = top + height - barHeight
                var gradient = ctx.createLinearGradient(0, y, 0, top + height)
                gradient.addColorStop(0, root.barColor); gradient.addColorStop(1, "#c9c6ff")
                ctx.fillStyle = gradient
                ctx.beginPath(); ctx.roundedRect(x, y, barWidth, barHeight, 6, 6); ctx.fill()
                if (i % 2 === 0) {
                    ctx.fillStyle = "#8c8d87"
                    var label = String(root.points[i].label)
                    ctx.fillText(label, x + barWidth / 2 - ctx.measureText(label).width / 2, top + height + 20)
                }
            }
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }
}
