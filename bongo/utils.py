"""通用工具函数。

从原 workspace.py 迁移的通用函数，供各模块使用。
"""

import hashlib
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
PERSIST_THRESHOLD = 16000
PERSIST_PREVIEW_SIZE = 5000
IGNORED_PATH_NAMES = {".git", ".bongo", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


def persist_large_output(result, tool_use_id=None):
    if len(result) <= PERSIST_THRESHOLD:
        return result, None
    cache_dir = Path.home() / ".bongo" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    file_id = tool_use_id or hashlib.sha256(result.encode("utf-8")).hexdigest()[:12]
    cache_path = cache_dir / f"{file_id}.txt"
    cache_path.write_text(result, encoding="utf-8")
    preview = result[:PERSIST_PREVIEW_SIZE]
    persisted_text = (
        f"Output too large ({len(result)} chars). "
        f"Full output saved to: {cache_path}\n\n"
        f"Preview (first {PERSIST_PREVIEW_SIZE} chars):\n{preview}\n..."
    )
    return persisted_text, str(cache_path)


def load_persisted_output(cache_path):
    path = Path(cache_path)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


class Spinner:
    """终端 spinner 动画，在阻塞调用期间显示，让用户知道程序没卡死。

    用法：
        with Spinner("正在生成题目"):
            result = blocking_call()
    """

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message=""):
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        # 清除当前行
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r  {frame} {self.message}")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.1)
