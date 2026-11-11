"""Synthetic Windows checks. --scheduler briefly registers an isolated user task."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from monitorbot import desktop, install, windows
from monitorbot.config import initialize, save_config
from monitorbot.store import Store
from monitorbot.service import schedule_due


def cli(root, *arguments):
    completed = subprocess.run(
        [sys.executable, "-m", "monitorbot", "--home", str(root), *arguments],
        cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 0, "synthetic command failed: " + " ".join(arguments[:2]) + "\n" + completed.stderr
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduler", action="store_true", help="on Windows, register/run/remove an isolated test task")
    parser.add_argument("--dialogs", action="store_true", help="on Windows, open a synthetic confirmation dialog")
    args = parser.parse_args()
    if (args.scheduler or args.dialogs) and sys.platform != "win32":
        parser.error("--scheduler and --dialogs require Windows")
    try:
        ZoneInfo("Asia/Taipei")
    except ZoneInfoNotFoundError:
        parser.exit(1, "Timezone database missing. Run python -m pip install tzdata, then retry.\n")
    results = []
    with tempfile.TemporaryDirectory(prefix="monitor-windows-check-") as temporary:
        root = Path(temporary) / "synthetic data 中文"
        config = initialize(root)
        config["schedule"]["popup"] = False
        config["schedule"]["days"] = []
        for settings in config["sources"].values():
            settings["enabled"] = False
        config["improvement"]["enabled"] = False
        save_config(root, config)
        initialized = cli(root, "init")
        assert cli(root, "init")["started_at"] == initialized["started_at"], "init reset the collection start time"
        assert cli(root, "config", "show")["schedule"]["popup"] is False, "init reset saved configuration"
        results.append("CLI init is idempotent and preserves disabled synthetic sources and dialogs")
        with Store(root) as store:
            now = datetime.now(timezone.utc)
            local = now.astimezone(ZoneInfo(config["timezone"]))
            store.set_meta("started_at", now.isoformat())
            store.set_meta("weekly_done", (local.date() - timedelta(days=local.weekday())).isoformat())
            due = schedule_due(store, config, now + timedelta(minutes=1))
            assert not due["weekly"] and not due["daily"], "synthetic verification must not trigger reports or dialogs"
        for command in (("poll",), ("run", "--once")):
            cycle = cli(root, *command)
            assert cycle["processed"] == 0 and cycle["pending"] == 0, "empty collection unexpectedly processed content"
            assert all(cycle["health"][source]["status"] == "excluded" for source in config["sources"]), "a source was not disabled"
        assert cli(root, "tick")["idle"], "tick did not respect the polling interval"
        assert cli(root, "doctor")["data_dir"] == str(root.resolve()), "doctor inspected the wrong installation"
        results.append("CLI poll and run completed without source access; tick respected the interval; doctor inspected the isolated installation")
        prepared = windows.prepare(root, project_dir=PROJECT)
        assert Path(prepared["path"]).is_file(), "install did not create a service definition"
        results.append("service definition prepared without registration")
        if args.scheduler:
            with Store(root) as store:
                store.set_meta("last_poll", None)
            try:
                install.start(root, login=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    with Store(root) as store:
                        ran = bool(store.get_meta("last_poll"))
                    current = install.status(root)
                    if ran and current["loaded"] and current["login_enabled"]:
                        break
                    time.sleep(0.5)
                else:
                    raise AssertionError("scheduled process did not record a synthetic collection cycle within 30 seconds")
                results.append("interactive scheduled process ran a collection cycle with all sources disabled")
                install.start(root)
                assert install.status(root)["login_enabled"], "idempotent start lost login startup"
                install.stop(root)
                deadline = time.monotonic() + 10
                while install.status(root)["loaded"] and time.monotonic() < deadline:
                    time.sleep(0.2)
                stopped = install.status(root)
                assert not stopped["loaded"] and stopped["login_enabled"], "stop did not preserve login startup"
                results.append("start is idempotent; stop preserves login startup")
            finally:
                removed = install.stop(root, login=True)
                assert not removed["registered"], "test task remains registered"
            results.append("test task unregistered; data never moved to the real installation")
        title = "Synthetic Windows verification 待辦測試"
        task = cli(root, "add", title, "--kind", "assigned", "--project", "Windows 中文")
        ident = task["id"]
        assert task["title"] == title and task["kind"] == "assigned", "add did not preserve Unicode task data"
        assert any(item["id"] == ident for item in cli(root, "task", "list")), "task list omitted the new task"
        updated = cli(root, "task", "update", ident, "--json", json.dumps({"next_action": "確認中文輸入與儲存"}))
        assert updated["next_action"] == "確認中文輸入與儲存", "task update lost Unicode content"
        cli(root, "task", "transition", ident, "completed", "--reason", "合成驗證完成")
        assert cli(root, "task", "get", ident)["status"] == "completed", "task transition was not persisted"
        assert not any(item["id"] == ident for item in cli(root, "task", "list")), "completed task remains in the active list"
        assert any(item["id"] == ident for item in cli(root, "task", "list", "--all")), "completed task was lost"
        assert len(cli(root, "task", "history", ident)) >= 3, "task history omitted changes"
        with Store(root) as store:
            assert store.get_task(ident)["title"] == title, "CLI task data did not reach the database"
            markdown = desktop.todo_markdown(store, config)
            assert "每日待處理清單" in markdown and title not in markdown, "daily list did not reflect completion"
        results.append("CLI Unicode task add/list/update/complete/history persisted correctly; daily list excludes completed work")
        if args.dialogs:
            assert desktop.choose("Monitor Windows test", "Select the confirmation to verify the dialog.",
                                  ["確認 Windows 對話框正常"]) == "確認 Windows 對話框正常", "dialog was not confirmed"
            results.append("Windows dialog confirmed")
    print(json.dumps({"platform": sys.platform, "passed": results,
                      "not_checked": ([] if args.scheduler else ["real Windows Task Scheduler lifecycle; rerun on Windows with --scheduler"])
                      + ([] if args.dialogs else ["Windows confirmation dialog; rerun on Windows with --dialogs"]),
                      "sources": "Synthetic data only; no model or account access."}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
