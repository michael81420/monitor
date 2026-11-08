"""Calendar-week reports generated independently and stored as immutable versions."""

import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import AgentError


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "markdown": {"type": "string", "maxLength": 100000},
        "conflicts": {"type": "array", "maxItems": 100, "items": {
            "type": "object", "properties": {
                "task_id": {"type": "string"}, "message": {"type": "string", "maxLength": 2000}},
            "required": ["task_id", "message"], "additionalProperties": False}},
    },
    "required": ["markdown", "conflicts"],
    "additionalProperties": False,
}


def _aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Report timestamps must include a timezone")
    return value


def previous_week(now, timezone="Asia/Taipei"):
    local = _aware(now).astimezone(ZoneInfo(timezone))
    end = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=7), end


def _payload(data, start, end, generated_at):
    start, end, generated_at = _aware(start), _aware(end), _aware(generated_at)
    if start >= end:
        raise ValueError("Report period must be increasing")
    return {"period_start_inclusive": start.isoformat(), "period_end_exclusive": end.isoformat(),
            "current_state_as_of": generated_at.isoformat(), "data": data}


def generate_weekly(runner, data, start, end, generated_at):
    payload = _payload(data, start, end, generated_at)
    payload["report_kind"] = "weekly_work"
    payload["requirements"] = [
        "整理指定期間的完成事項、實際產出、重要決策與新增知識，附來源索引。",
        "依事件發生時間判斷期間歸屬，本週活動不可列為上週成果。",
        "另列整理時點的未完成、等待他人、下一步、責任與期限及超時。",
        "保留期間內的完成與之後重開事件；若完成判斷被推翻，明確註記更正。",
        "列出轉交、閒置註銷與來源排除原因，不把它們當成完成成果。",
        "呈現資料更新時間、缺口與無法核對的來源；不可掩蓋未接通來源。",
        "遇到需追蹤agent解決的矛盾，回傳conflicts的task_id與message，勿擅自修改任務。",
        "使用繁體中文Markdown，不逐字複製原始郵件或聊天。",
    ]
    return runner.run("reporter", payload, REPORT_SCHEMA)


def generate_system_report(runner, data, start, end, generated_at):
    payload = _payload(data, start, end, generated_at)
    payload["report_kind"] = "weekly_system_check"
    payload["requirements"] = [
        "整理來源同步缺口、判斷被推翻、重複詢問、低效處理及失敗嘗試。",
        "列出實際變更、版本與驗證證據；建議不等於修正，agent同意不等於驗證通過。",
        "問題區分已發現、處理中與已驗證修正；沒有新方法不建議重複相同失敗嘗試。",
        "以紀錄識別引用詳細依據，不複製整份日誌或來源原文。",
        "沒有提供變更或測試證據時明確表示，不能宣稱系統已自動修復。",
        "使用繁體中文Markdown；需要追蹤agent釐清的任務矛盾回傳conflicts。",
    ]
    return runner.run("reporter", payload, REPORT_SCHEMA)


def save_report(data_dir, kind, start, end, result):
    if kind not in ("weekly", "system"):
        raise ValueError("Unknown report kind")
    if not isinstance(result, dict) or not isinstance(result.get("markdown"), str):
        raise ValueError("Report result must contain Markdown")
    if result.get("conflicts"):
        raise AgentError("Report has unresolved conflicts and was not published")
    if not result["markdown"].strip():
        raise AgentError("Empty report was not published")
    start, end = _aware(start), _aware(end)
    if start >= end:
        raise ValueError("Report period must be increasing")
    directory = Path(data_dir) / "reports" / kind / start.strftime("%Y")
    directory.mkdir(parents=True, exist_ok=True)
    text = result["markdown"].rstrip() + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    stem = start.strftime("%Y-%m-%d") + "_" + end.strftime("%Y-%m-%d")
    target = directory / (stem + "_" + digest + ".md")
    if target.exists():
        if target.read_text(encoding="utf-8") != text:
            raise AgentError("Report version collision")
        return target
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=str(directory))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
