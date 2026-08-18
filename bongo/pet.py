from __future__ import annotations

import ctypes
import html
import json
import os
import re
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    Property,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .application_names import display_application_name
from .widgets import WrappedPushButton


def _windows_native_window_rect(
    hwnd: int,
) -> tuple[int, int, int, int] | None:
    if os.name != "nt" or not hwnd:
        return None

    user32 = ctypes.windll.user32
    get_window_rect = user32.GetWindowRect
    get_window_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    get_window_rect.restype = wintypes.BOOL
    set_thread_dpi_context = getattr(
        user32,
        "SetThreadDpiAwarenessContext",
        None,
    )
    previous_dpi_context = None
    if set_thread_dpi_context is not None:
        set_thread_dpi_context.argtypes = [wintypes.HANDLE]
        set_thread_dpi_context.restype = wintypes.HANDLE
        try:
            previous_dpi_context = set_thread_dpi_context(ctypes.c_void_p(-4))
        except (ctypes.ArgumentError, OSError, ValueError):
            previous_dpi_context = None

    rect = wintypes.RECT()
    try:
        if not get_window_rect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
    except (ctypes.ArgumentError, OSError, ValueError):
        return None
    finally:
        if set_thread_dpi_context is not None and previous_dpi_context:
            try:
                set_thread_dpi_context(previous_dpi_context)
            except (ctypes.ArgumentError, OSError, ValueError):
                pass

    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _map_position_between_screens(
    position: QPoint,
    previous_bounds: QRect,
    target_bounds: QRect,
    window_width: int,
    window_height: int,
) -> QPoint:
    """Preserve a window's relative desktop position across screen changes."""
    previous_x_span = max(0, previous_bounds.width() - window_width)
    previous_y_span = max(0, previous_bounds.height() - window_height)
    target_x_span = max(0, target_bounds.width() - window_width)
    target_y_span = max(0, target_bounds.height() - window_height)

    relative_x = (
        (position.x() - previous_bounds.x()) / previous_x_span
        if previous_x_span
        else 0.5
    )
    relative_y = (
        (position.y() - previous_bounds.y()) / previous_y_span
        if previous_y_span
        else 0.5
    )
    relative_x = max(0.0, min(1.0, relative_x))
    relative_y = max(0.0, min(1.0, relative_y))
    return QPoint(
        target_bounds.x() + round(relative_x * target_x_span),
        target_bounds.y() + round(relative_y * target_y_span),
    )


def _clamp_position_to_screen(
    position: QPoint,
    bounds: QRect,
    window_width: int,
    window_height: int,
) -> QPoint:
    maximum_x = bounds.x() + max(0, bounds.width() - window_width)
    maximum_y = bounds.y() + max(0, bounds.height() - window_height)
    return QPoint(
        min(max(position.x(), bounds.x()), maximum_x),
        min(max(position.y(), bounds.y()), maximum_y),
    )


class InputSignals(QObject):
    key_changed = Signal(str, bool)
    mouse_button_changed = Signal(str, bool)
    mouse_moved = Signal(float, float)
    context_requested = Signal(float, float)
    global_click = Signal(str, bool, float, float)


class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class WorkSessionBubble(QWidget):
    """Compact speech bubble displayed above the Live2D canvas."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedHeight(122)
        self._tail_tip_ratio = 0.88
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setStyleSheet(
            "QLabel{background:transparent;color:#202429;border:none;"
            "font-size:12px;}"
        )

    def setText(self, text: str) -> None:
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def set_tail_tip_ratio(self, ratio: float) -> None:
        self._tail_tip_ratio = max(0.24, min(0.92, float(ratio)))
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.label.setGeometry(16, 7, max(1, self.width() - 32), 85)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#202429"), 2.5))
        painter.setBrush(QColor("#fffdf8"))

        width = float(self.width())
        body_bottom = 96.0
        tail_tip = width * self._tail_tip_ratio
        tail_right = width * (self._tail_tip_ratio - 0.08)
        tail_left = width * (self._tail_tip_ratio - 0.20)
        path = QPainterPath()
        path.moveTo(16.0, 4.0)
        path.lineTo(width - 16.0, 4.0)
        path.quadTo(width - 4.0, 4.0, width - 4.0, 16.0)
        path.lineTo(width - 4.0, body_bottom - 12.0)
        path.quadTo(width - 4.0, body_bottom, width - 16.0, body_bottom)
        path.lineTo(tail_right, body_bottom)
        path.lineTo(tail_tip, 118.0)
        path.lineTo(tail_left, body_bottom)
        path.lineTo(16.0, body_bottom)
        path.quadTo(4.0, body_bottom, 4.0, body_bottom - 12.0)
        path.lineTo(4.0, 16.0)
        path.quadTo(4.0, 4.0, 16.0, 4.0)
        path.closeSubpath()
        painter.drawPath(path)


class CircleActionButton(QPushButton):
    """Circular action with an animated hover surface."""

    def __init__(self, symbol: str, label: str, accent: str, font_family: str, parent=None):
        super().__init__(parent)
        self.symbol = symbol
        self.label = label
        self.accent = QColor(accent)
        self.font_family = font_family
        self._hover_progress = 0.0
        self.setFixedSize(80, 80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("QPushButton{background:transparent;border:none;}")
        self.animation = QPropertyAnimation(self, b"hoverProgress", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_hover_progress(self) -> float:
        return self._hover_progress

    def set_hover_progress(self, value: float) -> None:
        self._hover_progress = max(0.0, min(1.0, float(value)))
        self.update()

    hoverProgress = Property(float, get_hover_progress, set_hover_progress)

    def _animate(self, target: float) -> None:
        self.animation.stop()
        self.animation.setStartValue(self._hover_progress)
        self.animation.setEndValue(target)
        self.animation.start()

    def enterEvent(self, event) -> None:
        self._animate(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._animate(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        progress = self._hover_progress
        inset = 6.0 - progress * 3.0
        circle = self.rect().adjusted(round(inset), round(inset), -round(inset), -round(inset))
        fill = QColor(self.accent)
        fill.setAlpha(round(14 + progress * 38))
        border = QColor(self.accent)
        border.setAlpha(round(150 + progress * 105))
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.5 + progress * 1.2))
        painter.drawEllipse(circle)

        symbol_font = QFont(self.font_family, round(17 + progress * 2))
        symbol_font.setWeight(QFont.Weight.Bold)
        painter.setFont(symbol_font)
        painter.setPen(self.accent)
        painter.drawText(self.rect().adjusted(0, 11, 0, -30), Qt.AlignmentFlag.AlignCenter, self.symbol)

        label_font = QFont("Microsoft YaHei UI", 9)
        label_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(label_font)
        painter.setPen(QColor("#303735"))
        painter.drawText(self.rect().adjusted(0, 40, 0, -8), Qt.AlignmentFlag.AlignCenter, self.label)


class ActionSpeechBubble(QWidget):
    """Square 2×2 launcher bubble displayed above the cat."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(232, 256)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(22, 18, 22, 38)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(12)

    def add_action(self, button: CircleActionButton, row: int, column: int) -> None:
        self.grid.addWidget(button, row, column, Qt.AlignmentFlag.AlignCenter)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#202429"), 2.5))
        painter.setBrush(QColor("#fffdf8"))
        width = float(self.width())
        body_bottom = 228.0
        path = QPainterPath()
        path.moveTo(20.0, 4.0)
        path.lineTo(width - 20.0, 4.0)
        path.quadTo(width - 4.0, 4.0, width - 4.0, 20.0)
        path.lineTo(width - 4.0, body_bottom - 16.0)
        path.quadTo(width - 4.0, body_bottom, width - 20.0, body_bottom)
        path.lineTo(width * 0.84, body_bottom)
        path.lineTo(width * 0.92, 252.0)
        path.lineTo(width * 0.72, body_bottom)
        path.lineTo(20.0, body_bottom)
        path.quadTo(4.0, body_bottom, 4.0, body_bottom - 16.0)
        path.lineTo(4.0, 20.0)
        path.quadTo(4.0, 4.0, 20.0, 4.0)
        path.closeSubpath()
        painter.drawPath(path)


class QuestionSpeechBubble(QFrame):
    """Large speech bubble used for questions, feedback and messages."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("questionBubble")
        self._tail_tip_ratio = 0.88

    def set_tail_tip_ratio(self, ratio: float) -> None:
        self._tail_tip_ratio = max(0.24, min(0.92, float(ratio)))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#202429"), 2.5))
        painter.setBrush(QColor("#fffdf8"))

        width = float(self.width())
        body_bottom = float(max(24, self.height() - 22))
        tail_tip = width * self._tail_tip_ratio
        tail_right = width * (self._tail_tip_ratio - 0.08)
        tail_left = width * (self._tail_tip_ratio - 0.20)
        path = QPainterPath()
        path.moveTo(16.0, 3.0)
        path.lineTo(width - 16.0, 3.0)
        path.quadTo(width - 3.0, 3.0, width - 3.0, 16.0)
        path.lineTo(width - 3.0, body_bottom - 13.0)
        path.quadTo(width - 3.0, body_bottom, width - 16.0, body_bottom)
        path.lineTo(tail_right, body_bottom)
        path.lineTo(tail_tip, float(self.height() - 2))
        path.lineTo(tail_left, body_bottom)
        path.lineTo(16.0, body_bottom)
        path.quadTo(3.0, body_bottom, 3.0, body_bottom - 13.0)
        path.lineTo(3.0, 16.0)
        path.quadTo(3.0, 3.0, 16.0, 3.0)
        path.closeSubpath()
        painter.drawPath(path)
        super().paintEvent(event)


class BubbleScrollArea(QScrollArea):
    """Keeps bubble content narrower than the real scroll viewport."""

    _right_safe_area = 12

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.verticalScrollBar().rangeChanged.connect(self._schedule_width_sync)

    def setWidget(self, widget: QWidget) -> None:
        super().setWidget(widget)
        self._schedule_width_sync()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_width_sync()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_width_sync()

    def _schedule_width_sync(self, *_args) -> None:
        QTimer.singleShot(0, self.sync_content_width)

    def sync_content_width(self) -> None:
        content = self.widget()
        if content is None:
            return
        target_width = max(120, self.viewport().width() - self._right_safe_area)
        if content.minimumWidth() == target_width and content.maximumWidth() == target_width:
            return
        content.setMinimumWidth(target_width)
        content.setMaximumWidth(target_width)
        content.resize(target_width, content.height())
        if content.layout() is not None:
            content.layout().invalidate()
            content.layout().activate()
        content.updateGeometry()


class GlobalInputMonitor:
    """Forward input to the pet and optionally emit content-free activity events."""

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

    def __init__(
        self,
        signals: InputSignals,
        activity_callback: Callable[[str], None] | None = None,
    ):
        self.signals = signals
        self.activity_callback = activity_callback
        self.keyboard_listener = None
        self.mouse_listener = None
        self._last_move = 0.0
        self._pressed_keys: set[object] = set()
        self._key_lock = threading.Lock()
        self._mouse_filter_lock = threading.Lock()
        self._intercept_right_click = False
        self._pet_rect = (0, 0, 0, 0)
        self._pet_hwnd = 0
        self._suppress_right_release = False

    def set_right_click_interception(
        self,
        enabled: bool,
        rect: tuple[int, int, int, int],
        native_hwnd: int = 0,
    ) -> None:
        with self._mouse_filter_lock:
            self._intercept_right_click = bool(enabled)
            self._pet_rect = tuple(int(value) for value in rect)
            self._pet_hwnd = int(native_hwnd)

    def _filter_windows_mouse_event(self, message: int, data) -> bool:
        right_down = message == 0x0204
        right_up = message == 0x0205
        if not (right_down or right_up):
            return True

        x, y = int(data.pt.x), int(data.pt.y)
        with self._mouse_filter_lock:
            if right_down:
                current_rect = (
                    _windows_native_window_rect(self._pet_hwnd)
                    if self._intercept_right_click and self._pet_hwnd
                    else None
                )
                if current_rect is not None:
                    self._pet_rect = current_rect
                if self._pet_hwnd:
                    left, top, right, bottom = current_rect or (0, 0, 0, 0)
                else:
                    left, top, right, bottom = self._pet_rect
                suppress = (
                    self._intercept_right_click
                    and left <= x < right
                    and top <= y < bottom
                )
                self._suppress_right_release = suppress
            else:
                suppress = self._suppress_right_release
                self._suppress_right_release = False
        if not suppress:
            return True

        listener = self.mouse_listener
        try:
            self._dispatch_mouse_click(
                float(x),
                float(y),
                "right",
                right_down,
                request_context=True,
            )
        finally:
            if listener is not None:
                listener.suppress_event()
        return False

    def _dispatch_mouse_click(
        self,
        x: float,
        y: float,
        button,
        pressed: bool,
        *,
        request_context: bool,
    ) -> None:
        if pressed:
            self._record_activity("mouse_click")
        name = "right" if str(button).endswith("right") else "left"
        self.signals.mouse_button_changed.emit(name, bool(pressed))
        self.signals.global_click.emit(name, bool(pressed), float(x), float(y))
        if request_context and name == "right" and pressed:
            self.signals.context_requested.emit(float(x), float(y))

    def _handle_listener_mouse_click(
        self,
        x: float,
        y: float,
        button,
        pressed: bool,
    ) -> None:
        self._dispatch_mouse_click(
            x,
            y,
            button,
            pressed,
            request_context=os.name != "nt",
        )

    @staticmethod
    def _key_token(key) -> object:
        try:
            hash(key)
            return key
        except TypeError:
            return id(key)

    def _register_key_press(self, key) -> bool:
        token = self._key_token(key)
        with self._key_lock:
            if token in self._pressed_keys:
                return False
            self._pressed_keys.add(token)
            return True

    def _register_key_release(self, key) -> None:
        with self._key_lock:
            self._pressed_keys.discard(self._key_token(key))

    def _record_activity(self, event_type: str) -> None:
        if not self.activity_callback:
            return
        try:
            self.activity_callback(event_type)
        except Exception:
            # Input listeners must remain alive if activity persistence fails.
            return

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
            if self._register_key_press(key):
                self._record_activity("keyboard")
            if name := self._key_name(key):
                self.signals.key_changed.emit(name, True)

        def on_release(key):
            self._register_key_release(key)
            if name := self._key_name(key):
                self.signals.key_changed.emit(name, False)

        def on_click(_x, _y, button, pressed):
            self._handle_listener_mouse_click(_x, _y, button, pressed)

        def on_move(x, y):
            import time

            now = time.monotonic()
            if now - self._last_move < 0.05:
                return
            self._last_move = now
            self._record_activity("mouse_move")
            self.signals.mouse_moved.emit(float(x), float(y))

        self.keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener_options = {}
        if os.name == "nt":
            listener_options["win32_event_filter"] = self._filter_windows_mouse_event
        self.mouse_listener = mouse.Listener(
            on_click=on_click,
            on_move=on_move,
            **listener_options,
        )
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def stop(self) -> None:
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                listener.stop()
        self.keyboard_listener = None
        self.mouse_listener = None
        with self._key_lock:
            self._pressed_keys.clear()


class BongoCatView(QWebEngineView):
    """Hosts the MIT-licensed ayangweb/BongoCat Live2D model."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ready = False
        self.mirrored = False
        self.setFixedSize(374, 187)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # The Live2D canvas is visual-only. Let PetWindow receive drag and
        # context-menu events across the full visible cat area.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
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
            self.sync_display()

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

    def sync_display(self) -> None:
        if not self.ready:
            return
        window = self.window().windowHandle()
        screen = window.screen() if window is not None else self.screen()
        resolution = float(screen.devicePixelRatio()) if screen is not None else 1.0
        self._call("syncViewport", max(1.0, resolution))

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
    question_timeout: int = 45
    display_profile: str = "laptop_2880_200"


class PetWindow(QWidget):
    answer_selected = Signal(int, int)
    question_unanswered = Signal(int)
    position_changed = Signal(int, int)
    open_panel_requested = Signal()
    show_statistics_requested = Signal()
    show_ai_news_requested = Signal()
    open_dashboard_requested = Signal()
    news_detail_requested = Signal(int)
    news_read_requested = Signal(int)

    def __init__(
        self,
        question_loader: Callable[[], dict | None] | None = None,
        activity_callback: Callable[[str], None] | None = None,
    ):
        super().__init__()
        self.question_loader = question_loader
        self.current_question: dict | None = None
        self._question_pending = False
        self._feedback_pending = False
        self._message_pending = False
        self._action_menu_pending = False
        self._explanation_dialog: QDialog | None = None
        self._explanation_question: dict | None = None
        self._last_answered_question: dict | None = None
        self._current_news_id: int | None = None
        self._work_session_text = "当前没有连续工作计时"
        self._last_pointer_position: tuple[float, float] | None = None
        self._screen_signal_connected = False
        self._connected_screen = None
        self._placement_screen = None
        self._last_screen_geometry: QRect | None = None
        self._pending_previous_screen_geometry: QRect | None = None
        self._screen_topology_signals_connected = False
        self.pet_settings = PetSettings()
        self._drag_offset = QPoint()
        self._global_drag_offset = QPoint()
        self.setObjectName("petWindow")
        self.setWindowTitle("Bongo Study Pet")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(430, 423)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(3)
        self.bubble = QuestionSpeechBubble(self)
        self.bubble.setStyleSheet(
            "QFrame#questionBubble{background:transparent;border:none;}"
            "QLabel{color:#202429;font-size:13px;background:transparent;border:none;}"
            "QLabel#questionText{font-size:14px;}"
            "QPushButton{background:#eef3f1;color:#203d34;border:1px solid #8eb6a7;"
            "border-radius:5px;padding:5px 7px;text-align:left;font-size:12px;}"
            "QPushButton:hover{background:#d8e9e3;}"
            "QScrollArea#bubbleScroll{background:transparent;border:none;}"
            "QScrollArea#bubbleScroll QWidget#bubbleContent{background:transparent;}"
            "QScrollArea#bubbleScroll QScrollBar:vertical{width:12px;background:#e5e5e2;"
            "border:none;margin:0;}"
            "QScrollArea#bubbleScroll QScrollBar::handle:vertical{background:#8f9493;"
            "border-radius:4px;min-height:28px;}"
            "QScrollArea#bubbleScroll QScrollBar::add-line:vertical,"
            "QScrollArea#bubbleScroll QScrollBar::sub-line:vertical{height:0;}"
        )
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(8, 8, 8, 25)
        bubble_layout.setSpacing(6)
        self.bubble_scroll = BubbleScrollArea()
        self.bubble_scroll.setObjectName("bubbleScroll")
        self.bubble_scroll.setWidgetResizable(True)
        self.bubble_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.bubble_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.bubble_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.bubble_scroll.setFixedHeight(166)
        bubble_content = QWidget()
        bubble_content.setObjectName("bubbleContent")
        bubble_content.setMinimumWidth(0)
        content_layout = QVBoxLayout(bubble_content)
        content_layout.setContentsMargins(4, 2, 6, 2)
        content_layout.setSpacing(4)
        self.question_label = ClickableLabel()
        self.question_label.setObjectName("questionText")
        self.question_label.setTextFormat(Qt.TextFormat.PlainText)
        self.question_label.setWordWrap(True)
        self.question_label.setMinimumWidth(0)
        self.question_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.question_label.clicked.connect(self._request_news_detail)
        content_layout.addWidget(self.question_label)
        self.news_read_button = QPushButton("朕已阅")
        self.news_read_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.news_read_button.setStyleSheet(
            "QPushButton{background:#167a55;color:white;border:none;border-radius:7px;"
            "padding:7px 18px;text-align:center;font-size:13px;font-weight:700;}"
            "QPushButton:hover{background:#126746;}"
        )
        self.news_read_button.clicked.connect(self._mark_current_news_read)
        self.news_read_button.hide()
        content_layout.addWidget(self.news_read_button, 0, Qt.AlignmentFlag.AlignRight)
        self.break_panel = QWidget()
        break_layout = QVBoxLayout(self.break_panel)
        break_layout.setContentsMargins(2, 2, 2, 2)
        break_layout.setSpacing(8)
        break_badge = QLabel("休息提醒")
        break_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        break_badge.setFixedWidth(74)
        break_badge.setStyleSheet(
            "background:#fff0dc;color:#bd5b18;border:1px solid #efc28f;"
            "border-radius:8px;padding:3px 7px;font-size:11px;font-weight:700;"
        )
        break_layout.addWidget(break_badge, 0, Qt.AlignmentFlag.AlignLeft)
        break_title = QLabel("该休息一下啦")
        break_title.setStyleSheet(
            "color:#2c3532;font-size:18px;font-weight:700;"
        )
        break_layout.addWidget(break_title)
        self.break_summary_label = QLabel()
        self.break_summary_label.setWordWrap(True)
        self.break_summary_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.break_summary_label.setStyleSheet(
            "color:#68716e;font-size:12px;line-height:1.45;"
        )
        break_layout.addWidget(self.break_summary_label)
        break_metrics = QHBoxLayout()
        break_metrics.setContentsMargins(0, 2, 0, 2)
        break_metrics.setSpacing(8)

        def break_metric(caption: str, accent: str) -> QLabel:
            frame = QFrame()
            frame.setStyleSheet(
                "QFrame{background:#f8f4ec;border:1px solid #e8dfd1;"
                "border-radius:9px;}"
            )
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(8, 7, 8, 7)
            frame_layout.setSpacing(1)
            value = QLabel("-")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setStyleSheet(
                f"color:{accent};font-size:17px;font-weight:700;border:none;"
                "background:transparent;"
            )
            label = QLabel(caption)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(
                "color:#7b827f;font-size:10px;border:none;background:transparent;"
            )
            frame_layout.addWidget(value)
            frame_layout.addWidget(label)
            break_metrics.addWidget(frame, 1)
            return value

        self.break_duration_value = break_metric("连续工作", "#c76520")
        self.break_key_value = break_metric("键盘敲击 / 次", "#315f7c")
        break_layout.addLayout(break_metrics)
        self.break_ai_panel = QFrame()
        self.break_ai_panel.setStyleSheet(
            "QFrame{background:#f2f6f4;border:1px solid #d7e3de;"
            "border-radius:9px;}"
        )
        break_ai_layout = QVBoxLayout(self.break_ai_panel)
        break_ai_layout.setContentsMargins(10, 8, 10, 8)
        break_ai_layout.setSpacing(3)
        break_ai_title = QLabel("Bongo 的工作小结")
        break_ai_title.setStyleSheet(
            "color:#167a55;font-size:11px;font-weight:700;border:none;"
            "background:transparent;"
        )
        self.break_ai_text = QLabel()
        self.break_ai_text.setWordWrap(True)
        self.break_ai_text.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.break_ai_text.setStyleSheet(
            "color:#46504d;font-size:12px;border:none;background:transparent;"
        )
        break_ai_layout.addWidget(break_ai_title)
        break_ai_layout.addWidget(self.break_ai_text)
        break_layout.addWidget(self.break_ai_panel)
        self.break_suggestion_label = QLabel()
        self.break_suggestion_label.setWordWrap(True)
        self.break_suggestion_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.break_suggestion_label.setStyleSheet(
            "background:#fff3e4;color:#9b4d18;border:1px solid #f0d0aa;"
            "border-radius:8px;padding:8px;font-size:12px;font-weight:600;"
        )
        break_layout.addWidget(self.break_suggestion_label)
        self.break_panel.hide()
        content_layout.addWidget(self.break_panel)
        self.statistics_panel = QWidget()
        statistics_layout = QVBoxLayout(self.statistics_panel)
        statistics_layout.setContentsMargins(0, 0, 0, 0)
        statistics_layout.setSpacing(3)
        statistics_title = QLabel("今日使用统计")
        statistics_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        statistics_title.setStyleSheet(
            "font-size:16px;font-weight:700;color:#2d3734;"
        )
        statistics_layout.addWidget(statistics_title)
        metric_layout = QHBoxLayout()
        metric_layout.setContentsMargins(0, 6, 0, 2)
        metric_layout.setSpacing(12)

        def statistic_metric(label: str, color: str) -> QLabel:
            frame = QWidget()
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(0, 0, 0, 0)
            frame_layout.setSpacing(0)
            value = QLabel("-")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setStyleSheet(
                f"font-size:20px;font-weight:700;color:{color};"
            )
            caption = QLabel(label)
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setStyleSheet("font-size:10px;color:#77817e;")
            frame_layout.addWidget(value)
            frame_layout.addWidget(caption)
            metric_layout.addWidget(frame, 1)
            return value

        self.statistics_duration = statistic_metric("有效工作时长", "#167a55")
        self.statistics_keys = statistic_metric("键盘敲击 / 次", "#d56a1f")
        statistics_layout.addLayout(metric_layout)
        application_caption = QLabel("最活跃应用")
        application_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        application_caption.setStyleSheet(
            "font-size:16px;font-weight:700;color:#2d3734;"
        )
        statistics_layout.addWidget(application_caption)
        self.statistics_application = QLabel("暂无")
        self.statistics_application.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statistics_application.setStyleSheet(
            "font-size:17px;font-weight:700;color:#365d7a;"
        )
        statistics_layout.addWidget(self.statistics_application)
        self.statistics_panel.hide()
        self.news_read_button.hide()
        content_layout.addWidget(self.statistics_panel)
        self.option_buttons = []
        for index in range(4):
            button = WrappedPushButton()
            button.clicked.connect(lambda _checked=False, choice=index: self._answer(choice))
            content_layout.addWidget(button)
            self.option_buttons.append(button)
        self.feedback_label = ClickableLabel()
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setMinimumWidth(0)
        self.feedback_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.feedback_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.feedback_label.clicked.connect(self.show_explanation_card)
        content_layout.addWidget(self.feedback_label)
        content_layout.addStretch(1)
        self.bubble_scroll.setWidget(bubble_content)
        bubble_layout.addWidget(self.bubble_scroll)
        self.break_ack_button = QPushButton("我收到！")
        self.break_ack_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.break_ack_button.setMinimumHeight(38)
        self.break_ack_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.break_ack_button.setStyleSheet(
            "QPushButton{background:#d8732a;color:white;border:none;border-radius:9px;"
            "padding:8px 18px;text-align:center;font-size:13px;font-weight:700;}"
            "QPushButton:hover{background:#bd5b18;}"
            "QPushButton:pressed{background:#a94c12;}"
        )
        self.break_ack_button.clicked.connect(self._acknowledge_break_reminder)
        self.break_ack_button.hide()
        bubble_layout.addWidget(self.break_ack_button)
        self.bubble.hide()
        self.question_timer = QTimer(self)
        self.question_timer.setSingleShot(True)
        self.question_timer.timeout.connect(self._expire_question)
        self.message_timer = QTimer(self)
        self.message_timer.setSingleShot(True)
        self.message_timer.timeout.connect(self._hide_message_bubble)
        # Keep the cat at its existing coordinates while the speech bubble is
        # positioned independently in global screen space.
        layout.addStretch(1)
        self.canvas = BongoCatView()
        self.canvas.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self.work_session_badge = WorkSessionBubble(self)
        self.work_session_badge.hide()
        self.action_bubble = ActionSpeechBubble(self)
        action_specs = (
            ("Q", "来一题", "#167a55", "Georgia", self.show_next_question),
            ("Σ", "今日统计", "#d56a1f", "Segoe UI Symbol", lambda: self.show_statistics_requested.emit()),
            ("AI", "AI资讯", "#765a9b", "Consolas", lambda: self.show_ai_news_requested.emit()),
            ("▦", "仪表盘", "#315f7c", "Segoe UI Symbol", lambda: self.open_dashboard_requested.emit()),
        )
        self.action_buttons: list[CircleActionButton] = []
        for index, (symbol, label, accent, family, callback) in enumerate(action_specs):
            button = CircleActionButton(symbol, label, accent, family, self.action_bubble)
            button.clicked.connect(lambda _checked=False, action=callback: self._run_action_bubble_action(action))
            self.action_bubble.add_action(button, index // 2, index % 2)
            self.action_buttons.append(button)
        self.action_bubble.hide()

        self.signals = InputSignals()
        self.signals.key_changed.connect(self._set_key)
        self.signals.mouse_button_changed.connect(self._set_mouse_button)
        self.signals.mouse_moved.connect(self._follow_native_mouse)
        self.signals.context_requested.connect(self._show_context_menu_at_native)
        self.signals.global_click.connect(self._handle_native_global_click)
        self.monitor = GlobalInputMonitor(self.signals, activity_callback)
        self._connect_application_screen_signals()

    def start_input_monitor(self) -> None:
        self.monitor.start()

    def stop_input_monitor(self) -> None:
        self.monitor.stop()

    def show_next_question(self) -> None:
        if not self.question_loader:
            self.show_message("题库里还没有可以练习的题目。")
            return
        question = self.question_loader()
        if question:
            self.show_question(question)
        else:
            self.show_message("题库里还没有可以练习的题目。")

    def can_show_break_reminder(self) -> bool:
        return (
            not self._question_pending
            and not self._feedback_pending
            and not self._message_pending
            and not self._action_menu_pending
        )

    def set_work_session_tooltip(self, session: dict | None) -> None:
        if not session:
            tooltip = "当前没有连续工作计时"
            content = (
                "<div style='color:#66706d;font-size:16px;font-weight:700;'>专注状态</div>"
                "<div style='margin-top:4px;color:#303735;font-size:14px;"
                "font-weight:600;'>等待键鼠活动</div>"
            )
        else:
            duration = int(session.get("duration_seconds", 0))
            hours, remainder = divmod(duration, 3600)
            minutes = remainder // 60
            duration_text = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"
            key_count = int(session.get("key_press_count", 0))
            tooltip = f"已连续工作 {duration_text}\n本次键盘敲击 {key_count:,} 次"
            content = (
                "<div style='color:#66706d;font-size:16px;font-weight:700;'>专注进行中</div>"
                "<table width='100%' cellspacing='0' cellpadding='2'><tr>"
                f"<td align='center'><span style='color:#167a55;font-size:18px;"
                f"font-weight:700;'>{duration_text}</span><br>"
                "<span style='color:#77817e;font-size:10px;'>连续工作</span></td>"
                f"<td align='center'><span style='color:#d56a1f;font-size:18px;"
                f"font-weight:700;'>{key_count:,}</span><br>"
                "<span style='color:#77817e;font-size:10px;'>键盘敲击 / 次</span></td>"
                "</tr></table>"
            )
        self._work_session_text = content
        self.setToolTip(tooltip)
        self.canvas.setToolTip(tooltip)
        self.work_session_badge.setText(content)
        self._position_work_session_badge()
        if self._last_pointer_position is not None:
            self._update_work_session_hover(*self._last_pointer_position)

    def _position_work_session_badge(self) -> None:
        badge_width = min(310, max(230, self.width() - 20))
        self.work_session_badge.setFixedWidth(badge_width)
        self._position_left_overlay(
            self.work_session_badge,
            tail_ratio=0.88,
            bottom_offset=8,
        )
        self.work_session_badge.raise_()

    def _canvas_screen_geometry(self):
        canvas_top_left = self.canvas.mapToGlobal(QPoint(0, 0))
        canvas_center = QPoint(
            canvas_top_left.x() + self.canvas.width() // 2,
            canvas_top_left.y() + self.canvas.height() // 2,
        )
        screen = QGuiApplication.screenAt(canvas_center) or self.screen()
        return canvas_top_left, screen.availableGeometry() if screen is not None else None

    def _position_left_overlay(
        self,
        overlay: QWidget,
        *,
        tail_ratio: float,
        bottom_offset: int,
    ) -> None:
        canvas_top_left, bounds = self._canvas_screen_geometry()
        anchor_x = canvas_top_left.x() + round(self.canvas.width() * 0.58)
        x = anchor_x - round(overlay.width() * tail_ratio)
        y = canvas_top_left.y() - overlay.height() + bottom_offset
        if bounds is not None:
            x = min(
                max(x, bounds.left() + 4),
                bounds.right() - overlay.width() - 3,
            )
            y = min(
                max(y, bounds.top() + 4),
                bounds.bottom() - overlay.height() - 3,
            )
        set_tail_tip_ratio = getattr(overlay, "set_tail_tip_ratio", None)
        if set_tail_tip_ratio is not None and overlay.width() > 0:
            set_tail_tip_ratio((anchor_x - x) / overlay.width())
        overlay.move(x, y)

    def _position_primary_bubble(self) -> None:
        _, bounds = self._canvas_screen_geometry()
        available_width = bounds.width() - 8 if bounds is not None else 414
        bubble_width = max(280, min(414, available_width))
        footer_height = (
            max(
                self.break_ack_button.minimumHeight(),
                self.break_ack_button.sizeHint().height(),
            ) + 6
            if not self.break_ack_button.isHidden()
            else 0
        )
        bubble_height = self.bubble_scroll.height() + 33 + footer_height
        self.bubble.setFixedSize(bubble_width, bubble_height)
        self._position_left_overlay(
            self.bubble,
            tail_ratio=0.88,
            bottom_offset=-3,
        )
        self.bubble.raise_()

    def _show_primary_bubble(self) -> None:
        self._position_primary_bubble()
        self.bubble.setWindowOpacity(self.windowOpacity())
        self.bubble.show()
        self.bubble.raise_()

    def _position_action_bubble(self) -> None:
        self._position_left_overlay(
            self.action_bubble,
            tail_ratio=0.92,
            bottom_offset=-6,
        )
        self.action_bubble.raise_()

    def _update_work_session_hover(self, x: float, y: float) -> None:
        self._last_pointer_position = (x, y)
        if not self.isVisible():
            self.work_session_badge.hide()
            return
        top_left = self.canvas.mapToGlobal(QPoint(0, 0))
        inside = (
            top_left.x() <= x < top_left.x() + self.canvas.width()
            and top_left.y() <= y < top_left.y() + self.canvas.height()
        )
        if inside:
            self.work_session_badge.setText(self._work_session_text)
            self._position_work_session_badge()
            if self.bubble.isHidden() and self.action_bubble.isHidden():
                self.work_session_badge.show()
            else:
                self.work_session_badge.hide()
        else:
            self.work_session_badge.hide()

    def _show_context_menu_at_global(self, x: float, y: float) -> None:
        position = QPoint(round(x), round(y))
        if not self.isVisible() or not self.frameGeometry().contains(position):
            return
        self._popup_context_menu(position)

    def _show_context_menu_at_native(self, x: float, y: float) -> None:
        if not self.isVisible():
            return
        left, top, right, bottom = self._native_canvas_rect()
        if not (left <= x < right and top <= y < bottom):
            return
        self._popup_context_menu(QCursor.pos())

    def _popup_context_menu(self, position: QPoint) -> None:
        self.work_session_badge.hide()
        if not self.bubble.isHidden():
            self._dismiss_visible_bubbles()
        self._action_menu_pending = True
        self._position_action_bubble()
        self.action_bubble.setWindowOpacity(self.windowOpacity())
        self.action_bubble.show()
        self.action_bubble.raise_()

    def _hide_action_bubble(self) -> None:
        if not self._action_menu_pending and self.action_bubble.isHidden():
            return
        self._action_menu_pending = False
        self.action_bubble.hide()

    def _run_action_bubble_action(self, callback: Callable[[], None]) -> None:
        self._hide_action_bubble()
        callback()

    def _handle_global_click(
        self,
        button: str,
        pressed: bool,
        x: float,
        y: float,
    ) -> None:
        if button != "left":
            return
        position = QPoint(round(x), round(y))
        self._last_pointer_position = (float(position.x()), float(position.y()))
        if not pressed:
            if not self._global_drag_offset.isNull():
                self._global_drag_offset = QPoint()
                if self.pet_settings.keep_in_screen:
                    self.keep_inside_screen()
                else:
                    self._remember_current_screen_placement()
                self.position_changed.emit(self.x(), self.y())
            return
        if self.action_bubble.isVisible():
            if self.action_bubble.rect().contains(self.action_bubble.mapFromGlobal(position)):
                return
            self._hide_action_bubble()
        if self.canvas.rect().contains(self.canvas.mapFromGlobal(position)):
            if not self.pet_settings.pass_through and self.bubble.isHidden():
                self._global_drag_offset = position - self.frameGeometry().topLeft()
            return
        if (
            not self.bubble.isHidden()
            and self.bubble.rect().contains(self.bubble.mapFromGlobal(position))
        ):
            return
        self._dismiss_visible_bubbles()

    def _dismiss_visible_bubbles(self) -> None:
        self.work_session_badge.hide()
        self._hide_action_bubble()
        if self._question_pending:
            self._expire_question()
        elif not self.bubble.isHidden():
            self.message_timer.stop()
            self._hide_message_bubble()

    def _handle_native_global_click(
        self,
        button: str,
        pressed: bool,
        x: float,
        y: float,
    ) -> None:
        position = QCursor.pos()
        self._handle_global_click(button, pressed, position.x(), position.y())

    def _native_canvas_rect(self) -> tuple[int, int, int, int]:
        canvas_geometry = self.canvas.geometry()
        if os.name != "nt":
            top_left = self.canvas.mapToGlobal(QPoint(0, 0))
            return (
                top_left.x(),
                top_left.y(),
                top_left.x() + canvas_geometry.width(),
                top_left.y() + canvas_geometry.height(),
            )

        return _windows_native_window_rect(self._native_canvas_handle()) or (0, 0, 0, 0)

    def _native_canvas_handle(self) -> int:
        if os.name != "nt":
            return 0
        try:
            return int(self.canvas.winId())
        except (RuntimeError, TypeError, ValueError):
            return 0

    def apply_settings(self, settings: PetSettings, update_visibility: bool = True) -> None:
        was_visible = self.isVisible()
        self.pet_settings = settings
        if settings.pass_through:
            self._global_drag_offset = QPoint()
        self.setWindowOpacity(max(10, min(100, settings.opacity)) / 100)
        for overlay_name in ("bubble", "work_session_badge", "action_bubble"):
            overlay = getattr(self, overlay_name, None)
            if overlay is not None:
                overlay.setWindowOpacity(self.windowOpacity())
        scale = max(50, min(200, settings.scale)) / 100
        canvas_width = round(374 * scale)
        canvas_height = round(187 * scale)
        self.canvas.setFixedSize(canvas_width, canvas_height)
        self.setFixedSize(max(430, canvas_width + 16), canvas_height + 236)
        self.canvas.set_mirrored(settings.model_mirror)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.always_on_top)
        self._sync_input_transparency()
        if self._question_pending:
            self.question_timer.start(max(1, settings.question_timeout) * 1000)
        if update_visibility:
            self.setVisible(settings.visible)
        elif was_visible:
            self.show()
        if settings.keep_in_screen:
            self.keep_inside_screen()
        QTimer.singleShot(0, self._position_visible_overlays)

    def _position_visible_overlays(self) -> None:
        if hasattr(self, "bubble") and self.bubble.isVisible():
            self._position_primary_bubble()
        if hasattr(self, "work_session_badge") and self.work_session_badge.isVisible():
            self._position_work_session_badge()
        if hasattr(self, "action_bubble") and self.action_bubble.isVisible():
            self._position_action_bubble()

    def show_on_active_screen(self) -> None:
        self.ensure_on_active_screen(preserve_relative=True)
        self.show()
        self.ensure_on_active_screen(preserve_relative=True)
        self.raise_()
        QTimer.singleShot(0, self._finish_show_on_active_screen)

    def _finish_show_on_active_screen(self) -> None:
        self.ensure_on_active_screen(preserve_relative=True)
        self.raise_()

    def keep_inside_screen(self) -> None:
        self.ensure_on_active_screen(
            preserve_relative=False,
            prefer_previous_screen=False,
        )

    def ensure_on_active_screen(
        self,
        *,
        preserve_relative: bool = True,
        prefer_previous_screen: bool = True,
    ) -> bool:
        screens = list(QGuiApplication.screens())
        if not screens:
            return False

        frame = self.frameGeometry()
        target_screen = None
        if prefer_previous_screen and self._placement_screen in screens:
            target_screen = self._placement_screen
        elif prefer_previous_screen and self._connected_screen in screens:
            target_screen = self._connected_screen
        if target_screen not in screens:
            target_screen = QGuiApplication.screenAt(frame.center())
        if target_screen not in screens:
            intersections = []
            for screen in screens:
                intersection = frame.intersected(screen.availableGeometry())
                intersections.append(
                    (max(0, intersection.width()) * max(0, intersection.height()), screen)
                )
            intersection_area, intersecting_screen = max(
                intersections,
                key=lambda item: item[0],
            )
            target_screen = intersecting_screen if intersection_area > 0 else None
        if target_screen not in screens:
            target_screen = QGuiApplication.primaryScreen() or screens[0]

        target_bounds = QRect(target_screen.availableGeometry())
        previous_bounds = (
            self._pending_previous_screen_geometry
            or self._last_screen_geometry
        )
        desired_position = self.pos()
        if (
            preserve_relative
            and previous_bounds is not None
            and previous_bounds != target_bounds
        ):
            desired_position = _map_position_between_screens(
                desired_position,
                previous_bounds,
                target_bounds,
                self.width(),
                self.height(),
            )
        desired_position = _clamp_position_to_screen(
            desired_position,
            target_bounds,
            self.width(),
            self.height(),
        )

        moved = desired_position != self.pos()
        if moved:
            self.move(desired_position)
        self._pending_previous_screen_geometry = None
        self._last_screen_geometry = target_bounds
        self._placement_screen = target_screen
        self._bind_screen(target_screen)
        if moved:
            self.position_changed.emit(self.x(), self.y())
        return moved

    def _remember_current_screen_placement(self) -> None:
        screens = list(QGuiApplication.screens())
        if not screens:
            return
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen not in screens and self._connected_screen in screens:
            screen = self._connected_screen
        if screen not in screens:
            screen = QGuiApplication.primaryScreen() or screens[0]
        self._placement_screen = screen
        self._last_screen_geometry = QRect(screen.availableGeometry())


    def _set_key(self, key: str, pressed: bool) -> None:
        if self.pet_settings.keyboard_enabled:
            self.canvas.set_key(key, pressed)

    def _set_mouse_button(self, button: str, pressed: bool) -> None:
        if self.pet_settings.mouse_enabled:
            self.canvas.set_mouse_button(button, pressed)

    @staticmethod
    def _algorithm_statement(question: dict) -> str:
        statement = str(question.get("problem_statement") or "").strip()
        title = str(question.get("problem_title") or "").strip()
        if title:
            statement = re.sub(
                rf"^题目名称\s*[：:]\s*{re.escape(title)}\s*",
                "",
                statement,
            )
        statement = re.sub(r"^算法题简要摘要\s*[：:]\s*", "", statement).strip()
        return statement

    @staticmethod
    def _algorithm_prompt(question: dict) -> str:
        prompt = str(question.get("prompt") or "").strip()
        title = str(question.get("problem_title") or "").strip()
        if not title:
            return prompt
        title_prefix = re.compile(
            rf"^\s*(?:在|对于|关于)\s*[《「]\s*{re.escape(title)}\s*[》」]\s*"
            rf"(?:这道题)?(?:中|里)?\s*[，,:：]?\s*"
        )
        previous = None
        while prompt != previous:
            previous = prompt
            prompt = title_prefix.sub("", prompt, count=1).strip()
        return prompt or str(question.get("prompt") or "").strip()

    @classmethod
    def _algorithm_question_html(cls, question: dict) -> str:
        title = html.escape(str(question.get("problem_title") or "算法题").strip())
        statement = html.escape(cls._algorithm_statement(question))
        prompt = html.escape(cls._algorithm_prompt(question))
        topic = html.escape(str(question.get("topic") or "专项复习").strip())
        statement_block = (
            "<div style='margin-top:5px;color:#586360;font-size:13px;"
            "line-height:1.45;'>"
            f"{statement}</div>"
            if statement
            else ""
        )
        return (
            "<div style='margin-bottom:5px;'>"
            "<span style='background-color:#e8f3ef;color:#167a55;font-size:11px;"
            "font-weight:700;'> 算法题 </span>"
            f" <span style='color:#315f7c;font-size:16px;font-weight:700;'>{title}</span>"
            "</div>"
            f"{statement_block}"
            "<div style='margin-top:8px;color:#b85b1d;font-size:11px;"
            f"font-weight:700;'>本题考点 · {topic}</div>"
            "<div style='margin-top:3px;color:#202429;font-size:14px;"
            f"font-weight:600;line-height:1.45;'>{prompt}</div>"
        )

    def show_question(self, question: dict) -> None:
        if self._question_pending:
            self._expire_question()
        self.message_timer.stop()
        self.current_question = question
        self._current_news_id = None
        self._feedback_pending = False
        self._message_pending = False
        self._explanation_question = None
        self._question_pending = True
        self.statistics_panel.hide()
        self.news_read_button.hide()
        self.break_panel.hide()
        self.break_ack_button.hide()
        self.question_label.unsetCursor()
        self.question_label.show()
        self.bubble_scroll.setFixedHeight(184)
        self.bubble_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        if question.get("knowledge_type") == "code" and question.get("problem_title"):
            self.question_label.setTextFormat(Qt.TextFormat.RichText)
            self.question_label.setText(self._algorithm_question_html(question))
        else:
            self.question_label.setTextFormat(Qt.TextFormat.PlainText)
            self.question_label.setText(question["prompt"])
        for index, option in enumerate(question["options"]):
            self.option_buttons[index].set_wrapped_text(f"{chr(65 + index)}. {option}")
            self.option_buttons[index].show()
            self.option_buttons[index].setEnabled(True)
        self.feedback_label.clear()
        self.work_session_badge.hide()
        self._sync_input_transparency()
        self._show_primary_bubble()
        self.bubble_scroll.sync_content_width()
        QTimer.singleShot(0, self.bubble_scroll.sync_content_width)
        self.question_timer.start(max(1, self.pet_settings.question_timeout) * 1000)
        self.canvas.react("thinking")

    def show_message(
        self,
        message: str,
        timeout_ms: int = 5000,
        *,
        rich_text: bool = False,
    ) -> None:
        if self._question_pending:
            self._expire_question()
        self.question_timer.stop()
        self.message_timer.stop()
        self.current_question = None
        self._current_news_id = None
        self._feedback_pending = False
        self._message_pending = True
        self._explanation_question = None
        self.question_label.setTextFormat(
            Qt.TextFormat.RichText if rich_text else Qt.TextFormat.PlainText
        )
        self.question_label.setText(message)
        self.statistics_panel.hide()
        self.news_read_button.hide()
        self.break_panel.hide()
        self.break_ack_button.hide()
        self.question_label.unsetCursor()
        self.question_label.show()
        self.bubble_scroll.setFixedHeight(166)
        self.bubble_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        for button in self.option_buttons:
            button.hide()
        self.feedback_label.clear()
        self.work_session_badge.hide()
        self._sync_input_transparency()
        self._show_primary_bubble()
        self.bubble_scroll.sync_content_width()
        QTimer.singleShot(0, self.bubble_scroll.sync_content_width)
        self.message_timer.start(max(1, timeout_ms))

    def show_statistics(
        self,
        duration_text: str,
        key_count: int,
        application: str,
        timeout_ms: int = 10_000,
    ) -> None:
        if self._question_pending:
            self._expire_question()
        self.question_timer.stop()
        self.message_timer.stop()
        self.current_question = None
        self._current_news_id = None
        self._feedback_pending = False
        self._message_pending = True
        self._explanation_question = None
        self.question_label.hide()
        self.statistics_duration.setText(duration_text)
        self.statistics_keys.setText(f"{int(key_count):,}")
        display_application = self._application_display_name(application)
        self.statistics_application.setText(display_application)
        self.statistics_application.setToolTip(display_application)
        self.statistics_panel.show()
        self.news_read_button.hide()
        self.break_panel.hide()
        self.break_ack_button.hide()
        self.question_label.unsetCursor()
        for button in self.option_buttons:
            button.hide()
        self.feedback_label.clear()
        self.work_session_badge.hide()
        self.bubble_scroll.setFixedHeight(132)
        self.bubble_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._sync_input_transparency()
        self._show_primary_bubble()
        self.bubble_scroll.verticalScrollBar().setValue(0)
        self.bubble_scroll.sync_content_width()
        QTimer.singleShot(0, self.bubble_scroll.sync_content_width)
        self.message_timer.start(max(1, timeout_ms))

    def show_break_reminder(
        self,
        duration_text: str,
        key_count: int,
        report: dict | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        if self._question_pending:
            self._expire_question()
        self.question_timer.stop()
        self.message_timer.stop()
        self.current_question = None
        self._current_news_id = None
        self._feedback_pending = False
        self._message_pending = True
        self._explanation_question = None
        self.question_label.hide()
        self.news_read_button.hide()
        self.statistics_panel.hide()
        for button in self.option_buttons:
            button.hide()
        self.feedback_label.clear()
        self.work_session_badge.hide()

        self.break_duration_value.setText(str(duration_text))
        self.break_key_value.setText(f"{int(key_count):,}")
        if report:
            self.break_summary_label.setText(str(report.get("summary") or ""))
            self.break_ai_text.setText(str(report.get("activity") or ""))
            self.break_suggestion_label.setText(
                str(report.get("suggestion") or "站起来活动几分钟，让眼睛和手腕休息一下。")
            )
            self.break_ai_panel.show()
        else:
            self.break_summary_label.setText(
                "你已经保持了较长时间的连续工作，给身体留一点切换节奏的时间吧。"
            )
            self.break_ai_text.clear()
            self.break_ai_panel.hide()
            self.break_suggestion_label.setText(
                "站起来走动几分钟，看看远处，也让眼睛和手腕休息一下。"
            )
        self.break_panel.show()
        self.break_ack_button.show()
        self.bubble_scroll.setFixedHeight(238)
        self.bubble_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._sync_input_transparency()
        self._show_primary_bubble()
        self.bubble_scroll.verticalScrollBar().setValue(0)
        self.bubble_scroll.sync_content_width()
        QTimer.singleShot(0, self.bubble_scroll.sync_content_width)
        self.message_timer.start(max(1, timeout_ms))

    def _acknowledge_break_reminder(self) -> None:
        self.message_timer.stop()
        self._hide_message_bubble()

    def show_ai_news(self, item: dict, timeout_ms: int = 30_000) -> None:
        if not item:
            self.show_message("当前还没有可展示的 AI 简讯。", 8000)
            return
        title = html.escape(str(item.get("title") or "无标题"))
        summary = html.escape(str(item.get("summary") or ""))
        author = html.escape(str(item.get("author") or "未知作者"))
        published_at = html.escape(str(item.get("published_at_display") or item.get("published_at") or ""))
        self.show_message(
            "<div style='color:#167a55;font-size:10px;font-weight:700;'>AI 简讯</div>"
            f"<div style='margin-top:4px;color:#203d34;font-size:15px;font-weight:700;'>{title}</div>"
            f"<div style='margin-top:7px;color:#3d4542;font-size:14px;line-height:1.5;'>{summary}</div>"
            f"<div style='margin-top:7px;color:#77817e;font-size:10px;'>{published_at} · {author}</div>"
            "<div style='margin-top:7px;color:#315f7c;font-size:10px;'>点击气泡查看详情与原文链接</div>",
            timeout_ms,
            rich_text=True,
        )
        self._current_news_id = int(item["id"])
        self.question_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.news_read_button.show()
        self.bubble_scroll.setFixedHeight(184)
        self.bubble_scroll.sync_content_width()
        self._show_primary_bubble()
        self._reset_news_scroll_position()
        QTimer.singleShot(0, self._reset_news_scroll_position)

    def _reset_news_scroll_position(self) -> None:
        scroll_bar = self.bubble_scroll.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.minimum())

    def _request_news_detail(self) -> None:
        if self._current_news_id is not None:
            self.news_detail_requested.emit(self._current_news_id)

    def _mark_current_news_read(self) -> None:
        if self._current_news_id is None:
            return
        news_id = self._current_news_id
        self.news_read_requested.emit(news_id)
        self._hide_message_bubble()


    @staticmethod
    def _application_display_name(application: str) -> str:
        return display_application_name(application)

    def set_answer_feedback(self, correct: bool, explanation: str) -> None:
        self.message_timer.stop()
        self._feedback_pending = True
        self._message_pending = False
        color = "#176b4d" if correct else "#a33d39"
        self.feedback_label.setStyleSheet(
            f"color:{color};font-size:14px;font-weight:600;"
        )
        question = self.current_question
        # _answer clears current_question before the signal is handled, so retain
        # the question payload from the last displayed item for the detail card.
        if question is None:
            question = getattr(self, "_last_answered_question", None)
        self._explanation_question = question
        self.feedback_label.setText(
            ("答对了。" if correct else "再想一想。")
            + explanation
            + "\n\n点击解析查看完整题目"
        )
        for button in self.option_buttons:
            button.hide()
        self.canvas.react("left" if correct else "thinking")
        self._sync_input_transparency()
        self.message_timer.start(7000)

    def _hide_message_bubble(self) -> None:
        self._feedback_pending = False
        self._message_pending = False
        self.news_read_button.hide()
        self.break_panel.hide()
        self.break_ack_button.hide()
        self.bubble.hide()
        self._sync_input_transparency()
        if self._last_pointer_position is not None:
            self._update_work_session_hover(*self._last_pointer_position)

    def show_explanation_card(self) -> None:
        question = self._explanation_question
        if not question:
            return
        if self._explanation_dialog is not None:
            self._explanation_dialog.raise_()
            self._explanation_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("题目解析")
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.resize(520, 620)
        layout = QVBoxLayout(dialog)
        title = question.get("problem_title") or question.get("source_name") or "学习题目"
        heading = QLabel(str(title))
        heading.setStyleSheet("font-size:16px;font-weight:700;color:#203d34;")
        layout.addWidget(heading)
        details = QTextBrowser()
        details.setOpenExternalLinks(False)
        options = question.get("options") or []
        correct_index = int(question.get("correct_index", 0))
        answer = options[correct_index] if 0 <= correct_index < len(options) else ""
        statement = question.get("problem_statement") or "题干摘要见下方专项问题。"
        details.setPlainText(
            f"题目名：{title}\n\n"
            f"题干：{statement}\n\n"
            f"专项问题：{question.get('prompt', '')}\n\n"
            f"选项：\n" + "\n".join(
                f"{chr(65 + i)}. {option}" for i, option in enumerate(options)
            ) + "\n\n"
            f"正确答案：{chr(65 + correct_index)}. {answer}\n\n"
            f"解析：{self._explanation_text(question)}"
        )
        layout.addWidget(details, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)
        layout.addWidget(close_button)
        self._explanation_dialog = dialog
        dialog.finished.connect(lambda _result: self._clear_explanation_dialog(dialog))
        dialog.show()

    def _explanation_text(self, question: dict) -> str:
        return str(question.get("explanation") or "暂无解析")

    def _clear_explanation_dialog(self, dialog: QDialog) -> None:
        if self._explanation_dialog is dialog:
            self._explanation_dialog = None

    def _answer(self, selected_index: int) -> None:
        if not self.current_question or not self._question_pending:
            return
        question_id = int(self.current_question["id"])
        self._last_answered_question = self.current_question
        self._question_pending = False
        self.current_question = None
        self.question_timer.stop()
        for button in self.option_buttons:
            button.setEnabled(False)
        self.answer_selected.emit(question_id, selected_index)
        self._sync_input_transparency()

    def _expire_question(self) -> None:
        if not self.current_question or not self._question_pending:
            return
        question_id = int(self.current_question["id"])
        self._question_pending = False
        self.current_question = None
        self.question_timer.stop()
        self.bubble.hide()
        self._sync_input_transparency()
        if self._last_pointer_position is not None:
            self._update_work_session_hover(*self._last_pointer_position)
        self.question_unanswered.emit(question_id)

    def _sync_input_transparency(self) -> None:
        transparent = (
            self.pet_settings.pass_through
            and not self._question_pending
            and not self._feedback_pending
            and not self._message_pending
        )
        current = bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        if current == transparent:
            self._sync_right_click_interception()
            return
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, transparent)
        if was_visible:
            self.show()
        self._sync_right_click_interception()

    def _sync_right_click_interception(self) -> None:
        monitor = getattr(self, "monitor", None)
        if monitor is None:
            return
        monitor.set_right_click_interception(
            self.isVisible(),
            self._native_canvas_rect(),
            self._native_canvas_handle(),
        )

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._sync_right_click_interception()
        self._position_visible_overlays()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_canvas_display)
        self._sync_right_click_interception()
        if hasattr(self, "work_session_badge"):
            self._position_work_session_badge()
        if hasattr(self, "bubble") and self.bubble.isVisible():
            self._position_primary_bubble()
        if hasattr(self, "action_bubble"):
            self._position_action_bubble()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_signal_connected:
            handle.screenChanged.connect(self._screen_changed)
            self._screen_signal_connected = True
        self._bind_screen(handle.screen() if handle is not None else self.screen())
        self._sync_right_click_interception()
        if self._question_pending or self._feedback_pending or self._message_pending:
            QTimer.singleShot(0, self._show_primary_bubble)

    def _connect_application_screen_signals(self) -> None:
        if self._screen_topology_signals_connected:
            return
        application = QGuiApplication.instance()
        if application is None:
            return
        application.screenAdded.connect(self._screen_added)
        application.screenRemoved.connect(self._screen_removed)
        application.primaryScreenChanged.connect(self._primary_screen_changed)
        self._screen_topology_signals_connected = True

    def _screen_added(self, _screen) -> None:
        self._schedule_display_sync()

    def _screen_removed(self, screen) -> None:
        if screen is self._placement_screen and self._last_screen_geometry is not None:
            self._pending_previous_screen_geometry = QRect(
                self._last_screen_geometry
            )
        self._schedule_display_sync()

    def _primary_screen_changed(self, _screen) -> None:
        self._schedule_display_sync()

    def _bind_screen(self, screen) -> None:
        if screen is self._connected_screen:
            return
        previous = self._connected_screen
        if previous is not None:
            for signal_name in (
                "logicalDotsPerInchChanged",
                "physicalDotsPerInchChanged",
                "geometryChanged",
                "availableGeometryChanged",
            ):
                try:
                    getattr(previous, signal_name).disconnect(self._screen_metrics_changed)
                except (RuntimeError, TypeError):
                    pass
        self._connected_screen = screen
        if screen is None:
            return
        if self._placement_screen is None:
            self._placement_screen = screen
        if self._last_screen_geometry is None:
            self._last_screen_geometry = QRect(screen.availableGeometry())
        for signal_name in (
            "logicalDotsPerInchChanged",
            "physicalDotsPerInchChanged",
            "geometryChanged",
            "availableGeometryChanged",
        ):
            getattr(screen, signal_name).connect(self._screen_metrics_changed)

    def _screen_changed(self, screen) -> None:
        self._bind_screen(screen)
        self._schedule_display_sync()

    def _screen_metrics_changed(self, *_args) -> None:
        if (
            self._pending_previous_screen_geometry is None
            and self._last_screen_geometry is not None
        ):
            self._pending_previous_screen_geometry = QRect(
                self._last_screen_geometry
            )
        self._schedule_display_sync()

    def _schedule_display_sync(self) -> None:
        for delay in (0, 100, 300):
            QTimer.singleShot(delay, self._sync_after_screen_change)

    def _sync_canvas_display(self) -> None:
        sync_display = getattr(self.canvas, "sync_display", None)
        if sync_display is not None:
            sync_display()

    def _sync_after_screen_change(self) -> None:
        self._sync_canvas_display()
        self.ensure_on_active_screen(preserve_relative=True)
        if hasattr(self, "work_session_badge"):
            self._position_work_session_badge()
        if hasattr(self, "bubble") and self.bubble.isVisible():
            self._position_primary_bubble()
        if hasattr(self, "action_bubble") and self.action_bubble.isVisible():
            self._position_action_bubble()
        self._sync_right_click_interception()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if hasattr(self, "work_session_badge"):
            self.work_session_badge.hide()
        if hasattr(self, "bubble"):
            self.bubble.hide()
        if hasattr(self, "action_bubble"):
            self.action_bubble.hide()
        monitor = getattr(self, "monitor", None)
        if monitor is not None:
            monitor.set_right_click_interception(False, (0, 0, 0, 0))

    def _follow_mouse(self, x: float, y: float) -> None:
        self._update_work_session_hover(x, y)
        if not self.pet_settings.mouse_enabled:
            return
        screen = self.screen().geometry()
        normalized_x = (x - screen.center().x()) / max(screen.width() / 2, 1)
        normalized_y = (y - screen.center().y()) / max(screen.height() / 2, 1)
        if self.pet_settings.mouse_mirror:
            normalized_x = -normalized_x
        self.canvas.look_at(normalized_x, normalized_y)

    def _follow_native_mouse(self, x: float, y: float) -> None:
        position = QCursor.pos()
        if not self._global_drag_offset.isNull() and not self.pet_settings.pass_through:
            self.move(position - self._global_drag_offset)
            return
        self._follow_mouse(position.x(), position.y())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._popup_context_menu(event.globalPosition().toPoint())
            event.accept()
            return
        clicked_visible_bubble = (
            self.bubble.isVisible()
            and self.bubble.geometry().contains(event.position().toPoint())
        )
        if event.button() == Qt.MouseButton.LeftButton and not clicked_visible_bubble:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and not self._drag_offset.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_dragging = not self._drag_offset.isNull()
        self._drag_offset = QPoint()
        if self.pet_settings.keep_in_screen:
            self.keep_inside_screen()
        else:
            self._remember_current_screen_placement()
        self.position_changed.emit(self.x(), self.y())
        if was_dragging:
            event.accept()
            return
        super().mouseReleaseEvent(event)
