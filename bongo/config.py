"""持久化配置管理。

将用户的 API key、base URL、provider、model 等配置保存到 ~/.bongo/config.json，
这样只需设置一次，之后每次启动 bongo 都会自动加载。
"""

import json
import os
from pathlib import Path


def _config_dir():
    return Path.home() / ".bongo"


def _config_path():
    return _config_dir() / "config.json"


def load_config():
    """读取持久化配置，文件不存在则返回空字典。"""
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(data):
    """将配置写入磁盘，自动创建目录。"""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
