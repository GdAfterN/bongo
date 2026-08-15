from __future__ import annotations

import ctypes
import json
import os
import threading
from collections import defaultdict
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable

from .application_names import display_application_name
from .database import StudyDatabase


class ForegroundApplicationResolver:
    """Resolve the foreground executable without reading its window title."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_window = 0
        self._last_application = "unknown"
        self._configure_windows_api()

    @staticmethod
    def _configure_windows_api() -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def __call__(self) -> str:
        if os.name != "nt":
            return "unknown"
        with self._lock:
            return self._resolve_windows_application()

    def _resolve_windows_application(self) -> str:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            window = int(user32.GetForegroundWindow() or 0)
            if not window:
                return "unknown"
            if window == self._last_window:
                return self._last_application

            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(window), ctypes.byref(process_id))
            process = kernel32.OpenProcess(0x1000, False, process_id.value)
            if not process:
                return "unknown"
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(
                    process, 0, buffer, ctypes.byref(size)
                ):
                    return "unknown"
                application = Path(buffer.value).name.lower() or "unknown"
            finally:
                kernel32.CloseHandle(process)
            self._last_window = window
            self._last_application = application[:260]
            return self._last_application
        except (AttributeError, OSError, ValueError):
            return "unknown"


class ActivityRecorder:
    """Aggregate anonymous input activity before periodically writing SQLite."""

    SESSION_STATE_SETTING = "activity_session_state"

    COUNT_FIELDS = {
        "keyboard": "key_press_count",
        "mouse_click": "mouse_click_count",
    }

    def __init__(
        self,
        database: StudyDatabase,
        *,
        enabled: bool = False,
        application_resolver: Callable[[], str] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        bucket_minutes: int = 5,
        idle_minutes: int = 10,
    ):
        self.database = database
        self.enabled = bool(enabled)
        self.application_resolver = application_resolver or ForegroundApplicationResolver()
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())
        self.bucket_minutes = max(1, int(bucket_minutes))
        self.idle_seconds = max(1, int(idle_minutes)) * 60
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str, str], dict] = defaultdict(self._empty_bucket)
        self._mouse_second = ""
        self._mouse_keys: set[tuple[str, str, str]] = set()
        self._session_started_at: datetime | None = None
        self._session_last_activity_at: datetime | None = None
        self._session_applications: dict[str, dict[str, int]] = defaultdict(
            self._empty_session_application
        )
        self._session_reminder_sent = False
        if self.enabled:
            self._restore_session_state()

    @staticmethod
    def _empty_bucket() -> dict:
        return {
            "key_press_count": 0,
            "mouse_active_seconds": 0,
            "foreground_seconds": 0,
            "mouse_click_count": 0,
            "first_activity_at": "",
            "last_activity_at": "",
        }

    @staticmethod
    def _empty_session_application() -> dict[str, int]:
        return {
            "key_press_count": 0,
            "mouse_active_seconds": 0,
            "foreground_seconds": 0,
            "mouse_click_count": 0,
        }

    def _reset_session_locked(self) -> None:
        self._session_started_at = None
        self._session_last_activity_at = None
        self._session_applications = defaultdict(self._empty_session_application)
        self._session_reminder_sent = False

    def _session_state_locked(self) -> dict | None:
        if (
            not self.enabled
            or self._session_started_at is None
            or self._session_last_activity_at is None
        ):
            return None
        return {
            "started_at": self._session_started_at.isoformat(timespec="seconds"),
            "last_activity_at": self._session_last_activity_at.isoformat(timespec="seconds"),
            "applications": {
                application: dict(counts)
                for application, counts in self._session_applications.items()
            },
            "reminder_sent": self._session_reminder_sent,
        }

    def _save_session_state(self) -> None:
        with self._lock:
            state = self._session_state_locked()
        value = json.dumps(state, ensure_ascii=False) if state is not None else ""
        self.database.set_setting(self.SESSION_STATE_SETTING, value)

    def _restore_session_state(self) -> None:
        raw_state = self.database.get_setting(self.SESSION_STATE_SETTING, "")
        if not raw_state:
            return
        try:
            state = json.loads(raw_state)
            started_at = datetime.fromisoformat(str(state["started_at"]))
            last_activity_at = datetime.fromisoformat(str(state["last_activity_at"]))
            current = self.now_provider()
            if current.tzinfo is None:
                current = current.astimezone()
            if started_at.tzinfo is None:
                started_at = started_at.astimezone()
            if last_activity_at.tzinfo is None:
                last_activity_at = last_activity_at.astimezone()
            idle_seconds = (current - last_activity_at).total_seconds()
            if (
                started_at > last_activity_at
                or idle_seconds < 0
                or idle_seconds >= self.idle_seconds
            ):
                raise ValueError("saved work session is no longer active")

            applications = defaultdict(self._empty_session_application)
            for application, saved_counts in state.get("applications", {}).items():
                if not isinstance(application, str) or not isinstance(saved_counts, dict):
                    raise ValueError("invalid saved application activity")
                counts = applications[application[:260] or "unknown"]
                for field in counts:
                    value = saved_counts.get(field, 0)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError("invalid saved activity count")
                    counts[field] = value

            with self._lock:
                self._session_started_at = started_at
                self._session_last_activity_at = last_activity_at
                self._session_applications = applications
                self._session_reminder_sent = bool(state.get("reminder_sent", False))
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.database.set_setting(self.SESSION_STATE_SETTING, "")

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._lock:
            was_enabled = self.enabled
            self.enabled = enabled
        if was_enabled and not enabled:
            self.flush()
            with self._lock:
                self._reset_session_locked()
            self._save_session_state()

    def record(self, event_type: str) -> None:
        if event_type not in {*self.COUNT_FIELDS, "mouse_move"}:
            return
        with self._lock:
            if not self.enabled:
                return
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.astimezone()
        bucket_minute = now.minute - now.minute % self.bucket_minutes
        bucket_start = now.replace(minute=bucket_minute, second=0, microsecond=0)
        try:
            application = Path(str(self.application_resolver() or "unknown")).name.lower()
        except Exception:
            application = "unknown"
        application = (application or "unknown")[:260]
        key = (
            now.date().isoformat(),
            bucket_start.isoformat(timespec="seconds"),
            application,
        )
        timestamp = now.isoformat(timespec="seconds")
        with self._lock:
            if not self.enabled:
                return
            previous_activity_at = self._session_last_activity_at
            if (
                previous_activity_at is not None
                and (now - previous_activity_at).total_seconds() >= self.idle_seconds
            ):
                self._reset_session_locked()
                previous_activity_at = None
            if self._session_started_at is None:
                self._session_started_at = now
            self._session_last_activity_at = now
            bucket = self._pending[key]
            session_application = self._session_applications[application]
            if event_type == "mouse_move":
                if self._mouse_second != timestamp:
                    self._mouse_second = timestamp
                    self._mouse_keys.clear()
                if key in self._mouse_keys:
                    return
                self._mouse_keys.add(key)
                bucket["mouse_active_seconds"] += 1
                session_application["mouse_active_seconds"] += 1
            else:
                bucket[self.COUNT_FIELDS[event_type]] += 1
                session_application[self.COUNT_FIELDS[event_type]] += 1
            if not bucket["first_activity_at"]:
                bucket["first_activity_at"] = timestamp
            bucket["last_activity_at"] = timestamp

    def sample_foreground(self) -> None:
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.astimezone()
        with self._lock:
            last_activity_at = self._session_last_activity_at
            if (
                not self.enabled
                or last_activity_at is None
                or (now - last_activity_at).total_seconds() >= self.idle_seconds
            ):
                return
        try:
            application = Path(str(self.application_resolver() or "unknown")).name.lower()
        except Exception:
            application = "unknown"
        application = (application or "unknown")[:260]
        bucket_minute = now.minute - now.minute % self.bucket_minutes
        bucket_start = now.replace(minute=bucket_minute, second=0, microsecond=0)
        key = (
            now.date().isoformat(),
            bucket_start.isoformat(timespec="seconds"),
            application,
        )
        timestamp = now.isoformat(timespec="seconds")
        with self._lock:
            last_activity_at = self._session_last_activity_at
            if (
                not self.enabled
                or last_activity_at is None
                or (now - last_activity_at).total_seconds() >= self.idle_seconds
            ):
                return
            bucket = self._pending[key]
            bucket["foreground_seconds"] += 1
            if not bucket["first_activity_at"]:
                bucket["first_activity_at"] = timestamp
            bucket["last_activity_at"] = timestamp
            self._session_applications[application]["foreground_seconds"] += 1

    def flush(self) -> int:
        with self._lock:
            pending = self._pending
            self._pending = defaultdict(self._empty_bucket)
        rows = [
            {
                "activity_date": activity_date,
                "bucket_start": bucket_start,
                "application": application,
                **counts,
            }
            for (activity_date, bucket_start, application), counts in pending.items()
        ]
        try:
            if rows:
                self.database.add_activity_buckets(rows)
        except Exception:
            with self._lock:
                for key, counts in pending.items():
                    bucket = self._pending[key]
                    for field in (
                        "key_press_count",
                        "mouse_active_seconds",
                        "foreground_seconds",
                        "mouse_click_count",
                    ):
                        bucket[field] += counts[field]
                    values = [value for value in (bucket["first_activity_at"], counts["first_activity_at"]) if value]
                    bucket["first_activity_at"] = min(values) if values else ""
                    bucket["last_activity_at"] = max(
                        bucket["last_activity_at"], counts["last_activity_at"]
                    )
            raise
        self._save_session_state()
        return len(rows)

    def get_current_work_session(self, now: datetime | None = None) -> dict | None:
        current = now or self.now_provider()
        if current.tzinfo is None:
            current = current.astimezone()
        expired = False
        with self._lock:
            if not self.enabled or self._session_started_at is None:
                return None
            last_activity = self._session_last_activity_at
            if last_activity is None:
                return None
            idle_seconds = max(0, int((current - last_activity).total_seconds()))
            if idle_seconds >= self.idle_seconds:
                self._reset_session_locked()
                expired = True
                result = None
            else:
                applications = [
                    {"application": application, **counts}
                    for application, counts in self._session_applications.items()
                ]
                applications.sort(
                    key=lambda item: (
                        -item["key_press_count"],
                        -item["mouse_active_seconds"],
                        item["application"],
                    )
                )
                total_keys = sum(item["key_press_count"] for item in applications)
                total_mouse_seconds = sum(item["mouse_active_seconds"] for item in applications)
                total_foreground_seconds = sum(item["foreground_seconds"] for item in applications)
                total_clicks = sum(item["mouse_click_count"] for item in applications)
                duration_seconds = max(
                    0, int((current - self._session_started_at).total_seconds())
                )
                result = {
                    "started_at": self._session_started_at.isoformat(timespec="seconds"),
                    "last_activity_at": last_activity.isoformat(timespec="seconds"),
                    "duration_seconds": duration_seconds,
                    "idle_seconds": idle_seconds,
                    "key_press_count": total_keys,
                    "mouse_active_seconds": total_mouse_seconds,
                    "foreground_seconds": total_foreground_seconds,
                    "mouse_click_count": total_clicks,
                    "applications": applications,
                    "reminder_sent": self._session_reminder_sent,
                }
        if expired:
            self._save_session_state()
        return result

    def claim_break_reminder(self, minimum_minutes: int = 40) -> dict | None:
        session = self.get_current_work_session()
        if session is None or session["duration_seconds"] < max(1, minimum_minutes) * 60:
            return None
        with self._lock:
            if (
                self._session_reminder_sent
                or self._session_started_at is None
                or self._session_started_at.isoformat(timespec="seconds")
                != session["started_at"]
            ):
                return None
            self._session_reminder_sent = True
        self._save_session_state()
        session["reminder_sent"] = True
        return session


class WorkSessionTool:
    """Read-only tool exposed to the bounded break-reminder ReAct loop."""

    name = "get_current_work_session"
    description = "获取当前连续工作会话的匿名应用活动、键盘和鼠标聚合数据。"

    def __init__(self, recorder: ActivityRecorder, snapshot: dict | None = None):
        self.recorder = recorder
        self.snapshot = snapshot

    def execute(self) -> dict:
        result = self.snapshot or self.recorder.get_current_work_session() or {
            "active": False,
            "reason": "当前没有连续工作会话",
        }
        payload = dict(result)
        payload["applications"] = [
            {**item, "application": display_application_name(item.get("application", ""))}
            for item in result.get("applications", [])
        ]
        return payload
