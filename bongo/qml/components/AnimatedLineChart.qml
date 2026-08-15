import QtQuick

Item {
    id: root
    property var points: []
    property color lineColor: "#d97a4c"
    property color fillColor: "#ffe4d6"
    property string emptyText: "暂无数据"
    property real flowProgress: 0

    onPointsChanged: canvas.requestPaint()
    onFlowProgressChanged: canvas.requestPaint()
    NumberAnimation on flowProgress {
        from: 0
        to: 1
        duration: 3000
        loops: Animation.Infinite
        easing.type: Easing.InOutSine
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var left = 42, right = 18, top = 20, bottom = 32
            var width = canvas.width - left - right
            var height = canvas.height - top - bottom
            ctx.font = "11px 'Segoe UI'"
            ctx.strokeStyle = "#eceeed"
            ctx.fillStyle = "#8a918e"
            ctx.lineWidth = 1
            for (var grid = 0; grid < 4; grid++) {
                var gy = top + height * grid / 3
                ctx.beginPath(); ctx.moveTo(left, gy); ctx.lineTo(left + width, gy); ctx.stroke()
            }
            if (!root.points || root.points.length === 0) {
                ctx.fillText(root.emptyText, left + width / 2 - 30, top + height / 2)
                return
            }
            var maximum = 1
            for (var i = 0; i < root.points.length; i++) maximum = Math.max(maximum, Number(root.points[i].value))
            var count = root.points.length
            var gradient = ctx.createLinearGradient(0, top, 0, top + height)
            gradient.addColorStop(0, root.fillColor)
            gradient.addColorStop(1, "rgba(255,255,255,0)")
            ctx.beginPath()
            for (i = 0; i < count; i++) {
                var x = left + (count === 1 ? width / 2 : width * i / (count - 1))
                var y = top + height - height * Number(root.points[i].value) / maximum
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
            }
            ctx.lineTo(left + width, top + height); ctx.lineTo(left, top + height); ctx.closePath()
            ctx.fillStyle = gradient; ctx.fill()
            ctx.beginPath()
            for (i = 0; i < count; i++) {
                x = left + (count === 1 ? width / 2 : width * i / (count - 1))
                y = top + height - height * Number(root.points[i].value) / maximum
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
            }
            ctx.strokeStyle = root.lineColor; ctx.lineWidth = 3; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.stroke()
            ctx.fillStyle = "#8a918e"
            for (i = 0; i < count; i++) {
                x = left + (count === 1 ? width / 2 : width * i / (count - 1))
                var label = String(root.points[i].label)
                if (count <= 8 || i % 2 === 0) ctx.fillText(label, x - ctx.measureText(label).width / 2, top + height + 22)
            }
            if (count > 1) {
                var flowPosition = root.flowProgress * (count - 1)
                var segment = Math.min(count - 2, Math.floor(flowPosition))
                var segmentProgress = flowPosition - segment
                var startX = left + width * segment / (count - 1)
                var endX = left + width * (segment + 1) / (count - 1)
                var startY = top + height - height * Number(root.points[segment].value) / maximum
                var endY = top + height - height * Number(root.points[segment + 1].value) / maximum
                var flowX = startX + (endX - startX) * segmentProgress
                var flowY = startY + (endY - startY) * segmentProgress
                ctx.globalAlpha = 0.18
                ctx.fillStyle = root.lineColor
                ctx.beginPath(); ctx.arc(flowX, flowY, 10, 0, Math.PI * 2); ctx.fill()
                ctx.globalAlpha = 0.95
                ctx.beginPath(); ctx.arc(flowX, flowY, 3.5, 0, Math.PI * 2); ctx.fill()
                ctx.globalAlpha = 1
            }
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }
}
