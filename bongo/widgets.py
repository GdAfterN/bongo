from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from PySide6.QtCore import QLineF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QWidget,
)

from .application_names import display_application_name


class _WrappedTextControl:
    _horizontal_padding = 20
    _vertical_padding = 16

    def _setup_wrapped_text(
        self,
        parent: QWidget,
        left_margin: int,
        label_style: str,
        right_margin: int = 10,
    ) -> None:
        self._wrapped_label = QLabel(parent)
        self._wrapped_label.setWordWrap(True)
        self._wrapped_label.setMinimumWidth(0)
        self._wrapped_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._wrapped_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._wrapped_label.setStyleSheet(label_style)
        layout = QHBoxLayout(parent)
        layout.setContentsMargins(left_margin, 8, right_margin, 8)
        layout.addWidget(self._wrapped_label)
        parent.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        parent.setMinimumHeight(40)
        self._horizontal_padding = left_margin + right_margin

    def set_wrapped_text(self, text: str) -> None:
        self._wrapped_label.setText(text)
        self.setAccessibleName(text)
        self._sync_wrapped_height()
        QTimer.singleShot(0, self._sync_wrapped_height)
        self.updateGeometry()

    def wrapped_text(self) -> str:
        return self._wrapped_label.text()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        text_width = max(80, width - self._horizontal_padding)
        text_height = self._wrapped_label.heightForWidth(text_width)
        if text_height < 0:
            text_height = self._wrapped_label.fontMetrics().boundingRect(
                QRect(0, 0, text_width, 10000),
                Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere,
                self.wrapped_text(),
            ).height()
        return max(40, text_height + self._vertical_padding + 4)

    def _sync_wrapped_height(self) -> None:
        target = self.heightForWidth(max(80, self.width()))
        if self.minimumHeight() != target or self.maximumHeight() != target:
            self.setFixedHeight(target)
            self.updateGeometry()

        # A resize can settle before the child layout receives the new height.
        # Reactivate it even when the outer control is already at its target.
        inner_layout = self.layout()
        if inner_layout is not None:
            inner_layout.invalidate()
            inner_layout.activate()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_wrapped_height()

    def sizeHint(self) -> QSize:
        width = max(240, self.width())
        return QSize(240, self.heightForWidth(width))


class WrappedRadioButton(_WrappedTextControl, QRadioButton):
    def __init__(self, parent: QWidget | None = None):
        QRadioButton.__init__(self, "", parent)
        self._setup_wrapped_text(
            self,
            34,
            "QLabel{background:transparent;border:none;color:#203d34;font-size:14px;}",
        )


class WrappedPushButton(_WrappedTextControl, QPushButton):
    def __init__(self, parent: QWidget | None = None):
        QPushButton.__init__(self, "", parent)
        self._setup_wrapped_text(
            self,
            8,
            "QLabel{background:transparent;border:none;color:#203d34;font-size:14px;}",
        )


class ActivityTimelineWidget(QWidget):
    """Render deterministic half-hour stacked keyboard activity bars."""

    COLORS = (
        "#268060",
        "#3f6fa8",
        "#c4772d",
        "#8a5ca6",
        "#bd4f5c",
        "#4d8f96",
        "#777d3d",
        "#6f7680",
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bins: list[dict] = []
        self._applications: list[str] = []
        self._colors: dict[str, QColor] = {}

    def set_activity(self, rows: list[dict]) -> None:
        totals: dict[str, int] = defaultdict(int)
        grouped: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in rows:
            count = int(row.get("key_press_count", 0))
            if count <= 0:
                continue
            try:
                started_at = datetime.fromisoformat(str(row["bucket_start"]))
            except (KeyError, TypeError, ValueError):
                continue
            half_hour = started_at.replace(
                minute=0 if started_at.minute < 30 else 30,
                second=0,
                microsecond=0,
            )
            application = display_application_name(row.get("application", "unknown"))
            grouped[half_hour][application] += count
            totals[application] += count

        self._applications = [
            name for name, _count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        ]
        self._colors = {
            application: QColor(self.COLORS[index % len(self.COLORS)])
            for index, application in enumerate(self._applications)
        }
        if grouped:
            first = min(grouped).replace(minute=0, second=0, microsecond=0)
            last = max(grouped)
            cursor = first
            bins = []
            while cursor <= last:
                counts = dict(grouped.get(cursor, {}))
                bins.append(
                    {
                        "started_at": cursor,
                        "counts": counts,
                        "total": sum(counts.values()),
                    }
                )
                cursor = cursor.replace(minute=30) if cursor.minute == 0 else (
                    cursor.replace(minute=0) + timedelta(hours=1)
                )
            self._bins = bins
        else:
            self._bins = []
        self.setToolTip(self._tooltip_text())
        self.update()

    def _tooltip_text(self) -> str:
        if not self._applications:
            return "今天暂无键盘活动"
        totals = defaultdict(int)
        for item in self._bins:
            for application, count in item["counts"].items():
                totals[application] += count
        return "\n".join(
            f"{application}: {totals[application]} 次"
            for application in self._applications
        )

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if not self._bins:
            painter.setPen(QColor("#687078"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "今天暂无键盘活动")
            return

        metrics = QFontMetrics(self.font())
        left = 42
        right = 12
        top = 14
        bottom = 32
        plot = QRectF(
            left,
            top,
            max(1, self.width() - left - right),
            max(1, self.height() - top - bottom),
        )
        maximum = max(int(item["total"]) for item in self._bins) or 1
        painter.setPen(QPen(QColor("#d9dee2"), 1))
        for ratio in (0.0, 0.5, 1.0):
            y = plot.bottom() - plot.height() * ratio
            painter.drawLine(QLineF(plot.left(), y, plot.right(), y))
            label = str(round(maximum * ratio))
            painter.setPen(QColor("#687078"))
            painter.drawText(QRectF(0, y - 9, left - 6, 18), Qt.AlignmentFlag.AlignRight, label)
            painter.setPen(QPen(QColor("#d9dee2"), 1))

        slot = plot.width() / max(1, len(self._bins))
        bar_width = max(2.0, min(22.0, slot * 0.72))
        for index, item in enumerate(self._bins):
            x = plot.left() + index * slot + (slot - bar_width) / 2
            y = plot.bottom()
            for application in self._applications:
                count = int(item["counts"].get(application, 0))
                if count <= 0:
                    continue
                height = plot.height() * count / maximum
                y -= height
                painter.fillRect(QRectF(x, y, bar_width, height), self._colors[application])
            if index == 0 or index == len(self._bins) - 1 or item["started_at"].hour % 2 == 0 and item["started_at"].minute == 0:
                label = item["started_at"].strftime("%H:%M")
                label_width = metrics.horizontalAdvance(label) + 4
                painter.setPen(QColor("#687078"))
                painter.drawText(
                    QRectF(x + bar_width / 2 - label_width / 2, plot.bottom() + 6, label_width, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )
