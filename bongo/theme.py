"""Bongo CLI 配色方案和样式常量。

所有 UI 组件共用这套主题，方便统一调整。
"""

from rich.console import Console
from rich.theme import Theme
from rich.style import Style

# 颜色常量
PRIMARY = "cyan"
ACCENT = "magenta"
SUCCESS = "green"
WARNING = "yellow"
ERROR = "red"
MUTED = "dim white"
TOKEN_COLOR = "dim cyan"

# 预定义样式
STYLE_PROMPT = Style(bold=True, color=PRIMARY)
STYLE_TOOL_NAME = Style(bold=True, color=ACCENT)
STYLE_STEP_NUM = Style(dim=True, color="white")
STYLE_TOKEN = Style(dim=True, color=PRIMARY)
STYLE_ERROR = Style(bold=True, color=ERROR)
STYLE_WARNING = Style(bold=True, color=WARNING)
STYLE_SUCCESS = Style(color=SUCCESS)
STYLE_MUTED = Style(dim=True, color="white")
STYLE_USER_INPUT = Style(bold=True, color="white")
STYLE_BANNER = Style(bold=True, color=PRIMARY)
STYLE_BANNER_SUB = Style(dim=True, color="white")

# Rich Console 实例（全局共用）
bongo_theme = Theme({
    "prompt": STYLE_PROMPT,
    "tool": STYLE_TOOL_NAME,
    "step": STYLE_STEP_NUM,
    "token": STYLE_TOKEN,
    "error": STYLE_ERROR,
    "warning": STYLE_WARNING,
    "success": STYLE_SUCCESS,
    "muted": STYLE_MUTED,
})
console = Console(theme=bongo_theme)
