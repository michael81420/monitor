"""Configuration is local JSON; credentials belong in the source auth store."""

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULTS = {
    "timezone": "Asia/Taipei",
    "lookback_days": 0,
    "poll_seconds": 3600,
    "schedule": {
        "days": [0, 1, 2, 3, 4],
        "daily_time": "09:30",
        "work_end": "18:00",
        "weekly_day": 0,
        "weekly_time": "09:30",
        "question_limit": 10,
        "backlog_limit": 20,
        "popup": True,
    },
    "agents": {
        "default": {"backend": "codex", "model": None, "timeout": 240},
        "roles": {},
        "batch_size": 12,
        "max_context_tasks": 100,
    },
    "sources": {name: {"enabled": True, "include": [], "exclude": []}
                for name in ("chrome", "word", "excel", "powerpoint", "codex",
                             "claude", "chatgpt", "outlook", "teams")},
    "microsoft": {"client_id": "", "tenant": "organizations"},
    "cleanup": {"enabled": False, "interval_days": 30, "policy": None},
    "improvement": {"enabled": True, "auto_apply": True},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def data_path(value=None):
    return Path(value or os.environ.get("MONITOR_HOME") or
                Path(__file__).resolve().parent.parent / ".monitor").expanduser().resolve()


def _merge(base, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(data_dir):
    root = data_path(data_dir)
    path = root / "config.json"
    config = copy.deepcopy(DEFAULTS)
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("config.json must contain an object")
        _merge(config, value)
    validate_config(config)
    config["data_dir"] = str(root)
    config["microsoft"].setdefault("token_file", str(root / "microsoft-auth.json"))
    return config


def validate_config(config):
    from .filters import validate_filters
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
    for field in ("schedule", "agents", "sources", "microsoft", "cleanup", "improvement"):
        if not isinstance(config.get(field), dict):
            raise ValueError(field + " must be an object")
    if not isinstance(config["agents"].get("default"), dict) or not isinstance(config["agents"].get("roles", {}), dict):
        raise ValueError("agent default and roles must be objects")
    if any(not isinstance(settings, dict) for settings in config["agents"].get("roles", {}).values()):
        raise ValueError("each agent role must be an object")
    if not isinstance(config["timezone"], str):
        raise ValueError("timezone must be a string")
    ZoneInfo(config["timezone"])
    if type(config["lookback_days"]) is not int or config["lookback_days"] < 0:
        raise ValueError("lookback_days must be a nonnegative integer")
    if type(config["poll_seconds"]) is not int or config["poll_seconds"] < 60:
        raise ValueError("poll_seconds must be at least 60")
    schedule = config["schedule"]
    if not isinstance(schedule.get("days"), list):
        raise ValueError("schedule days must be a list")
    for key in ("daily_time", "weekly_time", "work_end"):
        if not isinstance(schedule[key], str):
            raise ValueError("schedule time must be an HH:MM string")
        datetime.strptime(schedule[key], "%H:%M")
    for day in schedule["days"] + [schedule["weekly_day"]]:
        if type(day) is not int or day not in range(7):
            raise ValueError("schedule days must be integers from 0 to 6")
    if (type(schedule["question_limit"]) is not int or type(schedule["backlog_limit"]) is not int
            or not 1 <= schedule["question_limit"] <= schedule["backlog_limit"]):
        raise ValueError("question limits must satisfy 1 <= normal <= backlog")
    if type(config["agents"]["batch_size"]) is not int or config["agents"]["batch_size"] < 1:
        raise ValueError("agent batch_size must be positive")
    if type(config["agents"]["max_context_tasks"]) is not int or not 1 <= config["agents"]["max_context_tasks"] <= 1000:
        raise ValueError("max_context_tasks must be between 1 and 1000")
    for settings in config["sources"].values():
        if not isinstance(settings, dict) or type(settings.get("enabled", True)) is not bool:
            raise ValueError("each source must be an object with a boolean enabled field")
        validate_filters(settings)
    if type(config["cleanup"]["interval_days"]) is not int or config["cleanup"]["interval_days"] < 1:
        raise ValueError("cleanup interval_days must be positive")
    for settings in [config["agents"]["default"]] + list(config["agents"].get("roles", {}).values()):
        if settings.get("backend", config["agents"]["default"]["backend"]) not in ("codex", "claude"):
            raise ValueError("agent backend must be codex or claude")


def save_config(data_dir, config):
    validate_config(config)
    root = data_path(data_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved = copy.deepcopy(config)
    saved.pop("data_dir", None)
    path = root / "config.json"
    temp = root / "config.json.tmp"
    temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def initialize(data_dir, lookback_days=None):
    config = load_config(data_dir)
    if lookback_days is not None:
        if lookback_days < 0:
            raise ValueError("lookback days cannot be negative")
        config["lookback_days"] = lookback_days
    save_config(data_dir, config)
    return config
