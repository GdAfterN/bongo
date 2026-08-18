from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
os.environ["BONGO_DISABLE_GLOBAL_INPUT"] = "1"

@pytest.mark.skipif(os.name != "nt", reason="Win32 activation is Windows-specific")
def test_native_window_restore_uses_win32_restore_and_foreground(monkeypatch):
    import ctypes

    from bongo.qml_bridge import BongoBridge

    class FakeUser32:
        def __init__(self):
            self.calls = []

        def ShowWindow(self, handle, command):
            self.calls.append(("show", handle.value, command))

        def BringWindowToTop(self, handle):
            self.calls.append(("top", handle.value))

        def SetForegroundWindow(self, handle):
            self.calls.append(("foreground", handle.value))

    class FakeWindow:
        @staticmethod
        def winId():
            return 0x1234

    user32 = FakeUser32()
    monkeypatch.setattr(ctypes.windll, "user32", user32)

    BongoBridge._restore_native_window(FakeWindow())

    assert user32.calls == [
        ("show", 0x1234, 9),
        ("top", 0x1234),
        ("foreground", 0x1234),
    ]


def test_pet_dashboard_action_restores_hidden_qml_window(tmp_path):
    from PySide6.QtCore import QPoint, QPointF, QRect, QUrl, Slot
    from PySide6.QtGui import QWindow
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from bongo.activity import ActivityRecorder
    from bongo.app import APP_ICON_PATH
    from bongo.pet import PetWindow
    from bongo.qml_bridge import BongoBridge
    from bongo.service import LearningService

    class CountingBridge(BongoBridge):
        def __init__(self, *args, **kwargs):
            self.dashboard_calls = 0
            super().__init__(*args, **kwargs)

        @Slot(result="QVariantMap")
        def dashboard(self):
            self.dashboard_calls += 1
            return super().dashboard()

    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    service = LearningService(tmp_path / "data")
    recorder = ActivityRecorder(service.database, enabled=False)
    pet = PetWindow()
    bridge = CountingBridge(
        service,
        pet,
        recorder,
        APP_ICON_PATH,
        pet_enabled=False,
        start_background_tasks=False,
    )
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    qml_path = Path(__file__).parents[1] / "bongo" / "qml" / "Main.qml"

    try:
        engine.load(QUrl.fromLocalFile(str(qml_path)))
        assert engine.rootObjects(), "Main.qml did not create an ApplicationWindow"
        window = engine.rootObjects()[0]
        assert isinstance(window, QWindow)

        # Match app.main(): QML attaches during Component.onCompleted, then Python
        # explicitly attaches the root once more after engine.load() returns.
        bridge.attachWindow(window)
        assert bridge.window is window

        window.setProperty("currentPage", 5)
        dashboard_calls_before_return = bridge.dashboard_calls
        window.setPosition(-10_000, -10_000)
        # Match the user flow: the QML onClosing handler rejects destruction
        # and hides the management window in the tray instead.
        assert window.close() is False
        application.processEvents()
        assert window.isVisible() is False

        pet.show()
        pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
        application.processEvents()
        assert pet.action_bubble.isVisible() is True
        pet.action_buttons[3].click()
        QTest.qWait(50)
        application.processEvents()

        assert pet.action_bubble.isVisible() is False
        assert window.isVisible() is True
        assert window.visibility() == QWindow.Visibility.Windowed
        assert window.property("currentPage") == 0
        assert bridge.dashboard_calls == dashboard_calls_before_return + 1
        assert any(
            screen.availableGeometry().intersects(window.frameGeometry())
            for screen in application.screens()
        ), "restored management window remained outside every active screen"

        chart = window.findChild(QQuickItem, "applicationKeystrokeChart")
        assert chart is not None
        chart.setProperty(
            "points",
            [
                {"label": "Typora", "value": 571, "valueLabel": "571"},
                {"label": "Python", "value": 34, "valueLabel": "34"},
            ],
        )
        QTest.qWait(1200)
        def find_visual_item(parent, object_name):
            for child in parent.childItems():
                if child.objectName() == object_name:
                    return child
                found = find_visual_item(child, object_name)
                if found is not None:
                    return found
            return None

        # ListView delegates belong to its visual tree, not necessarily the
        # QObject ownership tree traversed by findChild().
        first_row = find_visual_item(chart, "keystrokeRow0")
        second_row = find_visual_item(chart, "keystrokeRow1")
        assert first_row is not None
        assert second_row is not None

        QTest.mouseMove(window, QPoint(2, 2))
        QTest.qWait(220)
        assert chart.property("hoveredIndex") == -1
        capture_pixels = os.environ.get("QT_QPA_PLATFORM", "").lower() != "offscreen"
        # The offscreen software renderer does not draw AppCard layer effects,
        # so pixel assertions are reserved for the real Windows renderer.
        before_hover = window.grabWindow().copy() if capture_pixels else None

        center = first_row.mapToScene(QPointF(first_row.width() / 2, first_row.height() / 2))
        QTest.mouseMove(window, QPoint(round(center.x()), round(center.y())))
        QTest.qWait(350)
        after_hover = window.grabWindow().copy() if capture_pixels else None

        assert chart.property("hoveredIndex") == 0
        assert first_row.property("isHovered") is True
        assert second_row.property("isHovered") is False

        # The hover target must remain visibly distinct after all transitions finish.
        hovered_color = first_row.property("color")
        resting_color = second_row.property("color")
        assert hovered_color.alpha() > 0
        assert resting_color.alpha() == 0

        if capture_pixels:
            top_left = first_row.mapToScene(QPointF(0, 0))
            device_scale = before_hover.devicePixelRatio()
            row_rect = QRect(
                round(top_left.x() * device_scale),
                round(top_left.y() * device_scale),
                round(first_row.width() * device_scale),
                round(first_row.height() * device_scale),
            )
            before_row = before_hover.copy(row_rect)
            after_row = after_hover.copy(row_rect)
            changed_pixels = sum(
                before_row.pixel(x, y) != after_row.pixel(x, y)
                for y in range(before_row.height())
                for x in range(before_row.width())
            )
            assert changed_pixels > 200, (
                "hover state changed internally but was not visibly rendered: "
                f"before={before_hover.width()}x{before_hover.height()}, "
                f"after={after_hover.width()}x{after_hover.height()}, "
                f"row={row_rect.x()},{row_rect.y()} {row_rect.width()}x{row_rect.height()}, "
                f"crop={before_row.width()}x{before_row.height()}"
            )
    finally:
        bridge.tray.hide()
        bridge.shutdown()
        window = engine.rootObjects()[0] if engine.rootObjects() else None
        if window is not None:
            window.setProperty("allowClose", True)
            window.close()
        pet.close()
        service.close()
