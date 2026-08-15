from __future__ import annotations

import re
from pathlib import Path


PROCESS_NAME_MAP = {
    "chatgpt.exe": "ChatGPT",
    "chargpt.exe": "ChatGPT",
    "weixin.exe": "微信",
    "wechat.exe": "微信",
    "wechatappex.exe": "微信小程序",
    "typora.exe": "Typora",
    "idea64.exe": "IntelliJ IDEA",
    "idea.exe": "IntelliJ IDEA",
    "code.exe": "Visual Studio Code",
    "qq.exe": "QQ",
    "windowsterminal.exe": "Windows Terminal",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "cmd.exe": "命令提示符",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Firefox",
    "dingtalk.exe": "钉钉",
    "python.exe": "Python",
    "pythonw.exe": "Python",
    "codex.exe": "Codex",
    "claude.exe": "Claude Code",
    "explorer.exe": "文件资源管理器",
    "shellexperiencehost.exe": "Windows 桌面",
    "shellhost.exe": "Windows Shell",
    "applicationframehost.exe": "Windows 应用",
    "snippingtool.exe": "截图工具",
    "lockapp.exe": "锁屏界面",
    "kugou.exe": "酷狗音乐",
    "哔哩哔哩.exe": "哔哩哔哩",
    "clash-verge.exe": "Clash Verge",
    "cc-switch.exe": "CC Switch",
    "unknown": "未知应用",
}


def display_application_name(application: str) -> str:
    value = str(application or "").strip()
    if not value:
        return "暂无"
    process_name = Path(value).name
    mapped = PROCESS_NAME_MAP.get(process_name.lower())
    if mapped:
        return mapped
    return re.sub(r"\.(?:exe|com|bat|cmd)$", "", process_name, flags=re.IGNORECASE) or process_name
