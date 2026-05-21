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


# ── 多层级模型配置 ──────────────────────────────────────

TIER_KEYS = ("provider", "model", "base_url", "api_key")


def load_tier_config():
    """加载多层级模型配置，返回 {1: {...}, 2: {...}, 3: {...}}。"""
    config = load_config()
    tiers = {}
    for level in (1, 2, 3):
        tier_data = config.get(f"tier{level}", {})
        if tier_data:
            tiers[level] = tier_data
        else:
            tiers[level] = {}
    return tiers


def save_tier_config(tiers):
    """保存多层级模型配置到磁盘。"""
    config = load_config()
    for level, tier_data in tiers.items():
        config[f"tier{level}"] = tier_data
    save_config(config)


def get_tier_setting(level, key, fallback_config=None):
    """获取指定层级的某个配置项，支持回退到全局配置。"""
    tiers = load_tier_config()
    tier = tiers.get(level, {})
    value = tier.get(key)
    if value:
        return value
    if fallback_config:
        return fallback_config.get(key, "")
    return ""
