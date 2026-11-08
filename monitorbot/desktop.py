"""Native macOS dialogs keep the first daily interface small."""

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import AgentRunner
from .service import refresh_questions


def _osascript(script, args):
    result = subprocess.run(["osascript", "-e", script, "--"] + list(args),
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("native dialog cancelled or unavailable")
    return result.stdout.rstrip("\n")


def choose(title, prompt, choices):
    script = '''on run argv
        set options to items 3 thru -1 of argv
        set picked to choose from list options with title (item 1 of argv) with prompt (item 2 of argv) OK button name "確認" cancel button name "稍後"
        if picked is false then return ""
        return item 1 of picked
    end run'''
    return _osascript(script, [title, prompt] + choices)


def _text(title, prompt):
    script = '''on run argv
        set picked to display dialog (item 2 of argv) with title (item 1 of argv) default answer "" buttons {"取消", "儲存"} default button "儲存"
        return text returned of picked
    end run'''
    return _osascript(script, [title, prompt])


def todo_markdown(store, config):
    now = datetime.now(ZoneInfo(config["timezone"]))
    lines = ["# 每日待處理清單", "", "整理時間：" + now.isoformat(), ""]
    tasks = store.list_tasks()
    for title, waiting in (("輪到你處理", False), ("等待他人或前置條件", True)):
        lines += ["## " + title, ""]
        group = [task for task in tasks if (task["status"] == "waiting") == waiting]
        if not group:
            lines += ["目前沒有項目。", ""]
        for task in group:
            lines += ["- " + task["title"] + " (`" + task["id"] + "`)",
                      "  - 下一步：" + (task.get("next_action") or "尚未確定"),
                      "  - 負責人：" + ((task.get("waiting_for") if waiting else task.get("ownership")) or "未知")]
            if task.get("due_at"):
                expired = datetime.fromisoformat(task["due_at"]) < now
                lines.append("  - 交付期限：" + task["due_at"] + ("，已逾期" if expired else ""))
            if waiting and task.get("waiting_until"):
                expired = datetime.fromisoformat(task["waiting_until"]) < now
                lines.append("  - 前置工作預估完成：" + task["waiting_until"] +
                             ("，前置工作已超過預估" if expired else ""))
            lines += ["  - 最近實質進展：" + task["last_activity_at"], ""]
    lines += ["## 資料涵蓋", ""]
    for source, health in store.health().items():
        lines.append("- {}：{}，最後檢查 {}".format(source, health["status"], health.get("checked_at", "未知")))
    lines += ["", "尚未分析的活動：{} 筆。".format(store.count_events(unprocessed=True, excluded=False))]
    return "\n".join(lines) + "\n"


def daily_review(store, config):
    root = Path(config["data_dir"])
    path = root / "daily-todo.md"
    path.write_text(todo_markdown(store, config), encoding="utf-8")
    path.chmod(0o600)
    if sys.platform != "darwin":
        return {"path": str(path), "popup": False, "reason": "native dialogs currently require macOS"}
    runner = AgentRunner(config, root)
    questions = store.list_questions()
    if questions and not refresh_questions(store, config, runner):
        store.log("daily_questions_deferred", {"reason": "latest review did not complete"})
    reviewed = set(store.get_meta("daily_reviewed_questions", []))
    questions = [question for question in store.list_questions() if question["id"] in reviewed]
    today = datetime.now(ZoneInfo(config["timezone"])).date().isoformat()
    backlog = any(datetime.fromisoformat(question["created_at"]).astimezone(
        ZoneInfo(config["timezone"])).date().isoformat() < today for question in questions)
    cap = config["schedule"]["backlog_limit" if backlog else "question_limit"]
    answered = 0
    try:
        for question in questions[:cap]:
            latest = next((item for item in store.list_questions() if item["id"] == question["id"]), None)
            if not latest:
                continue
            labels = ["{}. {}".format(index + 1, choice) for index, choice in enumerate(latest["choices"])]
            suggestion = latest.get("suggested_index", 0)
            prompt = latest["text"]
            if 0 <= suggestion < len(latest["choices"]):
                prompt += "\n建議：" + latest["choices"][suggestion]
            picked = choose("工作確認 {}/{}".format(answered + 1, min(cap, len(questions))),
                            prompt, labels + ["其他，自行填寫"])
            if not picked:
                break
            answer = (_text("其他回應", latest["text"]) if picked == "其他，自行填寫"
                      else latest["choices"][labels.index(picked)])
            if not answer.strip():
                continue
            apply = False
            if latest.get("rule_key"):
                apply = choose("這項選擇的適用範圍", "是否將答案套用到往後同類情況？",
                               ["僅這次", "以後同類都如此處理"]) == "以後同類都如此處理"
            store.answer_question(latest["id"], answer, apply_to_similar=apply, scope=latest.get("scope"))
            answered += 1
        summary = "\n".join(task["title"] for task in store.list_tasks()[:10]) or "目前沒有待處理事項。"
        action = choose("每日待處理清單", summary, ["開啟完整清單", "關閉"])
        if action == "開啟完整清單":
            subprocess.Popen(["open", "-e", str(path)])
    except RuntimeError:
        pass
    return {"path": str(path), "answered": answered}
