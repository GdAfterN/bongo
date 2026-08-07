from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QWidget,
)


class _WrappedTextControl:
    _horizontal_padding = 20
    _vertical_padding = 16

    def _setup_wrapped_text(self, parent: QWidget, left_margin: int, label_style: str) -> None:
        self._wrapped_label = QLabel(parent)
        self._wrapped_label.setWordWrap(True)
        self._wrapped_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._wrapped_label.setStyleSheet(label_style)
        layout = QHBoxLayout(parent)
        layout.setContentsMargins(left_margin, 8, 10, 8)
        layout.addWidget(self._wrapped_label)
        parent.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        parent.setMinimumHeight(40)
        self._horizontal_padding = left_margin + 10

    def set_wrapped_text(self, text: str) -> None:
        self._wrapped_label.setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()

    def wrapped_text(self) -> str:
        return self._wrapped_label.text()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        text_width = max(80, width - self._horizontal_padding)
        bounds = self.fontMetrics().boundingRect(
            QRect(0, 0, text_width, 10000),
            Qt.TextFlag.TextWordWrap,
            self.wrapped_text(),
        )
        return max(40, bounds.height() + self._vertical_padding)

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
            "QLabel{background:transparent;border:none;color:#203d34;font-size:11px;}",
        )
