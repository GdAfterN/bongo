from __future__ import annotations

import json
import os
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
os.environ["BONGO_DISABLE_GLOBAL_INPUT"] = "1"


def _news_item(news_id: int) -> dict:
    return {
        "id": news_id,
        "title": f"简讯 {news_id}",
        "summary": f"这是第 {news_id} 条简讯的摘要。",
        "published_at": "2026-08-15T10:00:00+08:00",
        "author": f"author-{news_id}",
        "original_url": f"https://example.com/{news_id}",
        "discussion_url": f"https://news.ycombinator.com/item?id={news_id}",
        "original_title": f"News {news_id}",
    }


def test_pet_read_button_advances_to_an_unread_news_item(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module
    from bongo.activity import ActivityRecorder
    from bongo.qml_bridge import BongoBridge
    from bongo.service import LearningService

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    service = LearningService(tmp_path / "data")
    recorder = ActivityRecorder(service.database, enabled=False)
    pet = pet_module.PetWindow()
    bridge = BongoBridge(
        service,
        pet,
        recorder,
        Path(),
        pet_enabled=False,
        start_background_tasks=False,
    )
    digest = {
        "source": "Hacker News",
        "fetched_at": 1_786_766_400,
        "mode": "direct",
        "items": [_news_item(news_id) for news_id in (101, 102, 103)],
        "processed": 3,
        "total": 3,
        "failures": [],
        "complete": True,
    }
    service.database.set_setting(
        "ai_news_digest",
        json.dumps(digest, ensure_ascii=False),
    )

    emitted_read_ids = []
    pet.news_read_requested.connect(emitted_read_ids.append)

    try:
        pet.show()
        pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
        pet.action_buttons[2].click()
        application.processEvents()
        first_id = pet._current_news_id
        assert first_id == 101

        pet.news_read_button.click()
        application.processEvents()
        assert emitted_read_ids == [first_id]
        assert first_id in service.read_ai_news_ids()

        pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
        pet.action_buttons[2].click()
        application.processEvents()
        assert pet._current_news_id != first_id
        assert pet._current_news_id == 102
    finally:
        bridge.tray.hide()
        bridge.shutdown()
        pet.close()
        service.close()
