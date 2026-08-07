from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


class InputSignals(QObject):
    key_changed = Signal(str, bool)
    mouse_button_changed = Signal(str, bool)
    mouse_moved = Signal(float, float)


class GlobalInputMonitor:
    """Forwards transient input state to the pet without persisting user input."""

    SPECIAL_KEYS = {
        "alt": "Alt",
        "alt_gr": "AltGr",
        "backspace": "Backspace",
        "caps_lock": "CapsLock",
        "ctrl": "Control",
        "ctrl_l": "ControlLeft",
        "ctrl_r": "ControlRight",
        "delete": "Delete",
        "enter": "Return",
        "esc": "Escape",
        "left": "ArrowLeft",
        "right": "ArrowRight",
        "shift": "Shift",
        "shift_l": "ShiftLeft",
        "shift_r": "ShiftRight",
        "space": "Space",
        "tab": "Tab",
        "up": "ArrowUp",
        "down": "ArrowDown",
        "cmd": "Meta",
        "cmd_l": "Meta",
        "cmd_r": "Meta",
    }
    CHARACTER_KEYS = {
        "/": "Slash",
        "`": "BackQuote",
    }

    def __init__(self, signals: InputSignals):
        self.signals = signals
        self.keyboard_listener = None
        self.mouse_listener = None
        self._last_move = 0.0

    @classmethod
    def _key_name(cls, key) -> str:
        character = getattr(key, "char", None)
        if character:
            if character.isalpha():
                return f"Key{character.upper()}"
            if character.isdigit():
                return f"Num{character}"
            return cls.CHARACTER_KEYS.get(character, "")
        name = getattr(key, "name", "") or str(key).rsplit(".", 1)[-1]
        return cls.SPECIAL_KEYS.get(name, "")

    def start(self) -> None:
        if os.environ.get("BONGO_DISABLE_GLOBAL_INPUT") == "1":
            return
        try:
            from pynput import keyboard, mouse
        except ImportError:
            return

        def on_press(key):
            if name := self._key_name(key):
                self.signals.key_changed.emit(name, True)

        def on_release(key):
            if name := self._key_name(key):
                self.signals.key_changed.emit(name, False)

        def on_click(_x, _y, button, pressed):
            name = "right" if str(button).endswith("right") else "left"
            self.signals.mouse_button_changed.emit(name, bool(pressed))

        def on_move(x, y):
            import time

            now = time.monotonic()
            if now - self._last_move < 0.05:
                return
            self._last_move = now
            self.signals.mouse_moved.emit(float(x), float(y))

        self.keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.mouse_listener = mouse.Listener(on_click=on_click, on_move=on_move)
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def stop(self) -> None:
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                listener.stop()
        self.keyboard_listener = None
        self.mouse_listener = None


class BongoCatView(QWebEngineView):
    """Hosts the MIT-licensed ayangweb/BongoCat Live2D model."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ready = False
        self.mirrored = False
        self.setFixedSize(374, 187)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            True,
        )
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            False,
        )
        self.loadFinished.connect(self._loaded)
        renderer = Path(__file__).parent / "assets" / "bongocat" / "index.html"
        self.load(QUrl.fromLocalFile(str(renderer.resolve())))

    def _loaded(self, success: bool) -> None:
        self.ready = success
        if success:
            self.set_mirrored(self.mirrored)

    def _call(self, method: str, *arguments) -> None:
        payload = ",".join(json.dumps(value, ensure_ascii=True) for value in arguments)
        self.page().runJavaScript(f"window.bongoPet?.{method}({payload})")

    def set_key(self, key: str, pressed: bool) -> None:
        self._call("setKey", key, pressed)

    def set_mouse_button(self, button: str, pressed: bool) -> None:
        self._call("setMouseButton", button, pressed)

    def look_at(self, normalized_x: float, normalized_y: float) -> None:
        self._call("lookAt", normalized_x, normalized_y)

    def react(self, action: str) -> None:
        self._call("pulse", action)

    def set_mirrored(self, mirrored: bool) -> None:
        self.mirrored = mirrored
        if self.ready:
            self._call("setMirror", mirrored)


@dataclass(frozen=True)
class PetSettings:
    visible: bool = True
    opacity: int = 100
    scale: int = 100
    always_on_top: bool = True
    pass_through: bool = False
    keep_in_screen: bool = True
    model_mirror: bool = False
    mouse_mirror: bool = False
    keyboard_enabled: bool = True
    mouse_enabled: bool = True


class PetWindow(QWidget):
    answer_selected = Signal(int, int)
    position_changed = Signal(int, int)

    def __init__(self, question_loader: Callable[[], dict | None] | None = None):
        super().__init__()
        self.question_loader = question_loader
        self.current_question: dict | None = None
        self.pet_settings = PetSettings()
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
            "QFrame{background:#fffdf8;border:2px solid #202429;border-radius:8px;}"
            "QLabel{color:#202429;font-size:12px;background:transparent;border:none;}"
            "QPushButton{background:#eef3f1;color:#203d34;border:1px solid #8eb6a7;"
            "border-radius:4px;padding:4px;text-align:left;font-size:11px;}"
            "QPushButton:hover{background:#d8e9e3;}"
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
        self.canvas = BongoCatView()
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)

        self.signals = InputSignals()
        self.signals.key_changed.connect(self._set_key)
        self.signals.mouse_button_changed.connect(self._set_mouse_button)
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

    def apply_settings(self, settings: PetSettings, update_visibility: bool = True) -> None:
        was_visible = self.isVisible()
        self.pet_settings = settings
        self.setWindowOpacity(max(10, min(100, settings.opacity)) / 100)
        scale = max(50, min(200, settings.scale)) / 100
        canvas_width = round(374 * scale)
        canvas_height = round(187 * scale)
        self.canvas.setFixedSize(canvas_width, canvas_height)
        self.setFixedSize(max(260, canvas_width + 16), canvas_height + 218)
        self.canvas.set_mirrored(settings.model_mirror)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.always_on_top)
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, settings.pass_through)
        if update_visibility:
            self.setVisible(settings.visible)
        elif was_visible:
            self.show()
        if settings.keep_in_screen:
            self.keep_inside_screen()

    def keep_inside_screen(self) -> None:
        screen = self.screen()
        if not screen:
            return
        bounds = screen.availableGeometry()
        x = min(max(self.x(), bounds.left()), max(bounds.left(), bounds.right() - self.width() + 1))
        y = min(max(self.y(), bounds.top()), max(bounds.top(), bounds.bottom() - self.height() + 1))
        self.move(x, y)

    def _set_key(self, key: str, pressed: bool) -> None:
        if self.pet_settings.keyboard_enabled:
            self.canvas.set_key(key, pressed)

    def _set_mouse_button(self, button: str, pressed: bool) -> None:
        if self.pet_settings.mouse_enabled:
            self.canvas.set_mouse_button(button, pressed)

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
            button.hide()
        self.canvas.react("left" if correct else "thinking")
        QTimer.singleShot(7000, self.bubble.hide)

    def _answer(self, selected_index: int) -> None:
        if self.current_question:
            self.answer_selected.emit(int(self.current_question["id"]), selected_index)

    def _follow_mouse(self, x: float, y: float) -> None:
        if not self.pet_settings.mouse_enabled:
            return
        screen = self.screen().geometry()
        normalized_x = (x - screen.center().x()) / max(screen.width() / 2, 1)
        normalized_y = (y - screen.center().y()) / max(screen.height() / 2, 1)
        if self.pet_settings.mouse_mirror:
            normalized_x = -normalized_x
        self.canvas.look_at(normalized_x, normalized_y)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.bubble.geometry().contains(event.position().toPoint()):
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and not self._drag_offset.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._drag_offset = QPoint()
        if self.pet_settings.keep_in_screen:
            self.keep_inside_screen()
        self.position_changed.emit(self.x(), self.y())
