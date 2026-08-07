from __future__ import annotations

import math
import os
import random
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


class InputSignals(QObject):
    key_pressed = Signal()
    mouse_clicked = Signal(str)
    mouse_moved = Signal(float, float)


class GlobalInputMonitor:
    """Maps global input to anonymous animation events; no key or position is stored."""

    def __init__(self, signals: InputSignals):
        self.signals = signals
        self.keyboard_listener = None
        self.mouse_listener = None
        self._last_move = 0.0

    def start(self) -> None:
        if os.environ.get("BONGO_DISABLE_GLOBAL_INPUT") == "1":
            return
        try:
            from pynput import keyboard, mouse
        except ImportError:
            return

        self.keyboard_listener = keyboard.Listener(on_press=lambda _key: self.signals.key_pressed.emit())

        def on_click(_x, _y, button, pressed):
            if pressed:
                self.signals.mouse_clicked.emit("right" if str(button).endswith("right") else "left")

        def on_move(x, y):
            import time

            now = time.monotonic()
            if now - self._last_move < 0.05:
                return
            self._last_move = now
            self.signals.mouse_moved.emit(float(x), float(y))

        self.mouse_listener = mouse.Listener(on_click=on_click, on_move=on_move)
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def stop(self) -> None:
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                listener.stop()


class PetCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(250, 190)
        self.action = "idle"
        self.left_paw = False
        self.look_x = 0.0
        self.look_y = 0.0
        self.blink = False
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._reset_action)
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._blink_once)
        self._blink_timer.start(3200)

    def react(self, action: str) -> None:
        self.action = action
        if action == "key":
            self.left_paw = not self.left_paw
        self.update()
        self._reset_timer.start(190 if action in {"key", "left", "right"} else 500)

    def look_at(self, normalized_x: float, normalized_y: float) -> None:
        self.look_x = max(-1.0, min(1.0, normalized_x))
        self.look_y = max(-1.0, min(1.0, normalized_y))
        self.update()

    def _reset_action(self) -> None:
        self.action = "idle"
        self.update()

    def _blink_once(self) -> None:
        self.blink = True
        self.update()
        QTimer.singleShot(130, self._end_blink)

    def _end_blink(self) -> None:
        self.blink = False
        self.update()
        self._blink_timer.start(random.randint(2600, 4800))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(5, 5)
        ink = QColor("#273139")
        fur = QColor("#f2a65a")
        light = QColor("#fff1dc")
        accent = QColor("#4b8f77")
        painter.setPen(QPen(ink, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))

        # Tail follows the mouse direction.
        tail_path = QPainterPath()
        tail_path.moveTo(188, 132)
        tail_path.cubicTo(225, 130, 228 + self.look_x * 8, 83, 211, 70 - self.look_y * 8)
        painter.setPen(QPen(fur, 18, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(tail_path)
        painter.setPen(QPen(ink, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))

        painter.setBrush(fur)
        painter.drawRoundedRect(QRectF(54, 77, 142, 91), 43, 43)
        painter.setBrush(light)
        painter.drawEllipse(QRectF(87, 110, 77, 51))

        head = QPainterPath()
        head.moveTo(63, 75)
        head.lineTo(70, 24)
        head.lineTo(101, 45)
        head.cubicTo(125, 34, 151, 36, 170, 47)
        head.lineTo(198, 26)
        head.lineTo(195, 79)
        head.cubicTo(186, 111, 158, 125, 128, 125)
        head.cubicTo(94, 125, 68, 110, 63, 75)
        painter.setBrush(fur)
        painter.drawPath(head)

        painter.setBrush(light)
        painter.drawEllipse(QRectF(99, 80, 60, 39))
        eye_offset_x = self.look_x * 3
        eye_offset_y = self.look_y * 2
        painter.setPen(QPen(ink, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self.blink:
            painter.drawLine(97, 70, 110, 70)
            painter.drawLine(151, 70, 164, 70)
        else:
            painter.setBrush(ink)
            painter.drawEllipse(QRectF(99 + eye_offset_x, 63 + eye_offset_y, 9, 13))
            painter.drawEllipse(QRectF(153 + eye_offset_x, 63 + eye_offset_y, 9, 13))
        painter.drawLine(127, 96, 132, 99)
        painter.drawLine(132, 99, 137, 96)

        paw_y_left = 133 - (19 if self.action == "key" and self.left_paw else 0)
        paw_y_right = 133 - (19 if self.action == "key" and not self.left_paw else 0)
        if self.action == "left":
            paw_y_left -= 18
        if self.action == "right":
            paw_y_right -= 18
        painter.setBrush(fur)
        painter.drawEllipse(QRectF(48, paw_y_left, 55, 39))
        painter.drawEllipse(QRectF(159, paw_y_right, 55, 39))

        painter.setBrush(QColor("#d9e8e2"))
        painter.setPen(QPen(accent, 3))
        painter.drawRoundedRect(QRectF(77, 158, 111, 19), 5, 5)
        for x in range(88, 180, 15):
            painter.drawLine(x, 165, x + 8, 165)

        if self.action == "thinking":
            painter.setBrush(accent)
            painter.setPen(Qt.PenStyle.NoPen)
            for index, radius in enumerate((4, 6, 8)):
                angle = index * 2.1
                painter.drawEllipse(QRectF(207 + math.cos(angle) * 13, 34 + math.sin(angle) * 13, radius, radius))


class PetWindow(QWidget):
    answer_selected = Signal(int, int)

    def __init__(self, question_loader: Callable[[], dict | None] | None = None):
        super().__init__()
        self.question_loader = question_loader
        self.current_question: dict | None = None
        self._drag_offset = QPoint()
        self.setObjectName("petWindow")
        self.setWindowTitle("Bongo Study Pet")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(390, 405)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(3)
        self.bubble = QFrame()
        self.bubble.setStyleSheet(
            "QFrame{background:#fffdf8;border:2px solid #273139;border-radius:10px;}"
            "QLabel{color:#202429;font-size:12px;background:transparent;border:none;}"
            "QPushButton{background:#e5f1ec;color:#23493d;border:1px solid #8eb6a7;"
            "border-radius:4px;padding:4px;text-align:left;font-size:11px;}"
            "QPushButton:hover{background:#cce5dc;}"
        )
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(10, 8, 10, 8)
        bubble_layout.setSpacing(4)
        self.question_label = QLabel()
        self.question_label.setWordWrap(True)
        bubble_layout.addWidget(self.question_label)
        self.option_buttons = []
        for index in range(4):
            button = QPushButton()
            button.clicked.connect(lambda _checked=False, choice=index: self._answer(choice))
            bubble_layout.addWidget(button)
            self.option_buttons.append(button)
        self.feedback_label = QLabel()
        self.feedback_label.setWordWrap(True)
        bubble_layout.addWidget(self.feedback_label)
        self.bubble.hide()
        layout.addWidget(self.bubble)
        self.canvas = PetCanvas()
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)

        self.signals = InputSignals()
        self.signals.key_pressed.connect(lambda: self.canvas.react("key"))
        self.signals.mouse_clicked.connect(self.canvas.react)
        self.signals.mouse_moved.connect(self._follow_mouse)
        self.monitor = GlobalInputMonitor(self.signals)

    def start_input_monitor(self) -> None:
        self.monitor.start()

    def stop_input_monitor(self) -> None:
        self.monitor.stop()

    def show_next_question(self) -> None:
        if not self.question_loader:
            return
        question = self.question_loader()
        if question:
            self.show_question(question)

    def show_question(self, question: dict) -> None:
        self.current_question = question
        self.question_label.setText(question["prompt"])
        for index, option in enumerate(question["options"]):
            self.option_buttons[index].setText(f"{chr(65 + index)}. {option}")
            self.option_buttons[index].show()
            self.option_buttons[index].setEnabled(True)
        self.feedback_label.clear()
        self.bubble.show()
        self.canvas.react("thinking")

    def show_message(self, message: str, timeout_ms: int = 5000) -> None:
        self.current_question = None
        self.question_label.setText(message)
        for button in self.option_buttons:
            button.hide()
        self.feedback_label.clear()
        self.bubble.show()
        QTimer.singleShot(timeout_ms, self.bubble.hide)

    def set_answer_feedback(self, correct: bool, explanation: str) -> None:
        color = "#176b4d" if correct else "#a33d39"
        self.feedback_label.setStyleSheet(f"color:{color};font-weight:600;")
        self.feedback_label.setText(("答对了。" if correct else "再想一想。") + explanation)
        for button in self.option_buttons:
            button.setEnabled(False)
        self.canvas.react("left" if correct else "thinking")
        QTimer.singleShot(7000, self.bubble.hide)

    def _answer(self, selected_index: int) -> None:
        if self.current_question:
            self.answer_selected.emit(int(self.current_question["id"]), selected_index)

    def _follow_mouse(self, x: float, y: float) -> None:
        screen = self.screen().geometry()
        normalized_x = (x - screen.center().x()) / max(screen.width() / 2, 1)
        normalized_y = (y - screen.center().y()) / max(screen.height() / 2, 1)
        self.canvas.look_at(normalized_x, normalized_y)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.bubble.geometry().contains(event.position().toPoint()):
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and not self._drag_offset.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._drag_offset = QPoint()
