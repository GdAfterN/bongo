import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var points: []
    property int hoverIndex: -1
    property int animatedIndex: -1
    property int previousIndex: -1
    property real hoverProgress: 1
    property real loadProgress: 0
    property real pointerX: 0
    property real pointerY: 0
    property var colors: ["#dc8154", "#8da194", "#c6a66a", "#8da3af", "#a790a0", "#bc8879", "#9da49b"]
    property var liquidColors: [
        ["#f4b48f", "#dc8154", "#c96c40"],
        ["#bdccc2", "#8da194", "#71887a"],
        ["#ead5a5", "#c6a66a", "#ad8b52"],
        ["#c1d0d7", "#8da3af", "#718b99"],
        ["#d4c4d0", "#a790a0", "#8e7687"],
        ["#e2b9ad", "#bc8879", "#a66f61"],
        ["#ccd1ca", "#9da49b", "#838c81"]
    ]

    function liquidGradient(ctx, index, centerX, centerY, radius) {
        var palette = liquidColors[index % liquidColors.length]
        var gradient = ctx.createLinearGradient(
            centerX - radius * 0.78,
            centerY - radius * 0.82,
            centerX + radius * 0.72,
            centerY + radius * 0.88
        )
        gradient.addColorStop(0, palette[0])
        gradient.addColorStop(0.46, palette[1])
        gradient.addColorStop(0.72, palette[1])
        gradient.addColorStop(1, palette[2])
        return gradient
    }

    function playLoadAnimation() {
        loadProgress = 0
        loadAnimation.restart()
    }

    function totalSeconds() {
        var total = 0
        for (var i = 0; i < points.length; i++) total += Number(points[i].seconds) || 0
        return total
    }
    function percent(index) {
        var total = totalSeconds()
        return total > 0 ? (Number(points[index].seconds) || 0) * 100 / total : 0
    }
    function duration(seconds) {
        var minutes = Math.floor((Number(seconds) || 0) / 60)
        var hours = Math.floor(minutes / 60)
        minutes %= 60
        return hours > 0 ? hours + "小时" + minutes + "分钟" : minutes + "分钟"
    }
    function updateHover(mouseX, mouseY) {
        pointerX = chart.x + mouseX
        pointerY = chart.y + mouseY
        var centerX = chart.width / 2
        var centerY = chart.height / 2
        var radius = Math.min(chart.width, chart.height) * 0.40
        var distance = Math.sqrt(Math.pow(mouseX - centerX, 2) + Math.pow(mouseY - centerY, 2))
        if (distance < radius * 0.54 || distance > radius + 8 || totalSeconds() <= 0) {
            hoverIndex = -1
            return
        }
        var angle = Math.atan2(mouseY - centerY, mouseX - centerX) + Math.PI / 2
        if (angle < 0) angle += Math.PI * 2
        var cursor = 0
        for (var i = 0; i < points.length; i++) {
            cursor += percent(i) / 100 * Math.PI * 2
            if (angle <= cursor) {
                hoverIndex = i
                return
            }
        }
        hoverIndex = -1
    }

    onPointsChanged: {
        hoverIndex = -1
        playLoadAnimation()
    }
    onHoverIndexChanged: {
        previousIndex = animatedIndex
        animatedIndex = hoverIndex
        hoverProgress = 0
        hoverAnimation.restart()
    }
    onHoverProgressChanged: chart.requestPaint()
    onLoadProgressChanged: chart.requestPaint()

    NumberAnimation {
        id: hoverAnimation
        target: root
        property: "hoverProgress"
        from: 0
        to: 1
        duration: 230
        easing.type: Easing.OutCubic
    }
    NumberAnimation {
        id: loadAnimation
        target: root
        property: "loadProgress"
        from: 0
        to: 1
        duration: 900
        easing.type: Easing.OutCubic
    }

    RowLayout {
        anchors.fill: parent
        spacing: 8

        Canvas {
            id: chart
            Layout.preferredWidth: Math.max(180, root.width * 0.62)
            Layout.fillHeight: true
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                var total = root.totalSeconds()
                var centerX = chart.width / 2
                var centerY = chart.height / 2
                var radius = Math.min(chart.width, chart.height) * 0.40
                var innerRadius = radius * 0.54
                if (total <= 0) {
                    ctx.strokeStyle = "#e7eae9"
                    ctx.lineWidth = Math.max(10, radius - innerRadius)
                    ctx.beginPath(); ctx.arc(centerX, centerY, (radius + innerRadius) / 2, 0, Math.PI * 2); ctx.stroke()
                    ctx.fillStyle = "#9aa19e"
                    ctx.font = "12px 'Microsoft YaHei UI'"
                    ctx.textAlign = "center"
                    ctx.fillText("暂无数据", centerX, centerY + 4)
                    return
                }
                var startAngle = -Math.PI / 2
                var cursorRatio = 0
                for (var i = 0; i < root.points.length; i++) {
                    var segmentRatio = root.percent(i) / 100
                    var segmentEndRatio = cursorRatio + segmentRatio
                    if (root.loadProgress <= cursorRatio) break
                    var visibleEndRatio = Math.min(segmentEndRatio, root.loadProgress)
                    var endAngle = -Math.PI / 2 + visibleEndRatio * Math.PI * 2
                    var emphasis = 0
                    if (i === root.animatedIndex) emphasis = Math.max(emphasis, root.hoverProgress)
                    if (i === root.previousIndex) emphasis = Math.max(emphasis, 1 - root.hoverProgress)
                    var focusStrength = root.animatedIndex >= 0
                        ? Math.max(root.hoverProgress, root.previousIndex >= 0 ? 1 - root.hoverProgress : 0)
                        : (root.previousIndex >= 0 ? 1 - root.hoverProgress : 0)
                    var outerRadius = radius + emphasis * 6
                    ctx.beginPath()
                    ctx.arc(centerX, centerY, outerRadius, startAngle, endAngle)
                    ctx.arc(centerX, centerY, innerRadius, endAngle, startAngle, true)
                    ctx.closePath()
                    ctx.fillStyle = root.liquidGradient(ctx, i, centerX, centerY, outerRadius)
                    ctx.globalAlpha = emphasis > 0 ? 1 : 1 - focusStrength * 0.48
                    ctx.fill()

                    var sheen = ctx.createLinearGradient(
                        centerX - outerRadius,
                        centerY - outerRadius,
                        centerX + outerRadius,
                        centerY + outerRadius
                    )
                    sheen.addColorStop(0, "rgba(255,255,255,0.34)")
                    sheen.addColorStop(0.34, "rgba(255,255,255,0.10)")
                    sheen.addColorStop(0.7, "rgba(255,255,255,0)")
                    ctx.strokeStyle = sheen
                    ctx.lineWidth = 1.15
                    ctx.beginPath()
                    ctx.arc(centerX, centerY, outerRadius - 1.2, startAngle, endAngle)
                    ctx.stroke()
                    cursorRatio = segmentEndRatio
                    startAngle = -Math.PI / 2 + cursorRatio * Math.PI * 2
                }
                ctx.globalAlpha = 1
                ctx.textAlign = "center"
                var centerIndex = root.animatedIndex >= 0 ? root.animatedIndex : root.previousIndex
                var showFocused = root.animatedIndex >= 0 || (root.previousIndex >= 0 && root.hoverProgress < 1)
                ctx.fillStyle = showFocused ? root.colors[centerIndex % root.colors.length] : "#858780"
                ctx.font = "bold 11px 'Microsoft YaHei UI'"
                ctx.fillText(showFocused ? String(root.points[centerIndex].label) : "工作时间", centerX, centerY - 5, innerRadius * 1.72)
                ctx.fillStyle = "#292c28"
                ctx.font = "bold 13px 'Microsoft YaHei UI'"
                ctx.fillText(showFocused ? String(root.points[centerIndex].duration) : root.duration(total), centerX, centerY + 15)
            }
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.NoButton
                onPositionChanged: function(mouse) { root.updateHover(mouse.x, mouse.y) }
                onExited: root.hoverIndex = -1
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 7
            Repeater {
                model: root.points
                RowLayout {
                    id: legendRow
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true
                    spacing: 7
                    opacity: root.hoverIndex < 0 || root.hoverIndex === index ? 1 : 0.45
                    scale: root.hoverIndex === index ? 1.04 : 1
                    Behavior on opacity { NumberAnimation { duration: 180 } }
                    Behavior on scale { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                    Rectangle {
                        width: 8
                        height: 8
                        radius: 4
                        gradient: Gradient {
                            GradientStop { position: 0; color: root.liquidColors[legendRow.index % root.liquidColors.length][0] }
                            GradientStop { position: 0.55; color: root.liquidColors[legendRow.index % root.liquidColors.length][1] }
                            GradientStop { position: 1; color: root.liquidColors[legendRow.index % root.liquidColors.length][2] }
                        }
                    }
                    Text { Layout.fillWidth: true; text: modelData.label; elide: Text.ElideRight; color: Theme.textMuted; font.pixelSize: 11 }
                    Text { text: modelData.duration; color: Theme.text; font.pixelSize: 10; font.weight: Font.DemiBold }
                }
            }
        }
    }

    Rectangle {
        id: tooltip
        visible: root.hoverIndex >= 0 && root.hoverIndex < root.points.length
        opacity: visible ? 1 : 0
        width: 174
        height: 68
        radius: 15
        color: "#f5ffffff"
        border.color: Theme.border
        x: Math.max(6, Math.min(root.width - width - 6, root.pointerX + 12))
        y: Math.max(6, Math.min(root.height - height - 6, root.pointerY - height - 10))
        z: 5
        Behavior on x { NumberAnimation { duration: 100; easing.type: Easing.OutCubic } }
        Behavior on y { NumberAnimation { duration: 100; easing.type: Easing.OutCubic } }
        Behavior on opacity { NumberAnimation { duration: 140 } }
        Column {
            anchors.fill: parent
            anchors.margins: 10
            spacing: 5
            Text { text: tooltip.visible ? root.points[root.hoverIndex].label : ""; color: Theme.text; font.pixelSize: 12; font.weight: Font.Bold }
            Text { text: tooltip.visible ? root.points[root.hoverIndex].duration + " · " + root.percent(root.hoverIndex).toFixed(1) + "%" : ""; color: Theme.textMuted; font.pixelSize: 11 }
        }
    }
}
