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
        with Store(root) as store:
            now = datetime.now(timezone.utc)
            local = now.astimezone(ZoneInfo(config["timezone"]))
            store.set_meta("started_at", now.isoformat())
            store.set_meta("weekly_done", (local.date() - timedelta(days=local.weekday())).isoformat())
            due = schedule_due(store, config, now + timedelta(minutes=1))
            assert not due["weekly"] and not due["daily"], "synthetic verification must not trigger reports or dialogs"
        prepared = windows.prepare(root, project_dir=PROJECT)
        assert Path(prepared["path"]).is_file(), "install did not create a service definition"
        results.append("service definition prepared without registration")
        if args.scheduler:
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
        completed = subprocess.run([sys.executable, "-m", "monitorbot", "--home", str(root),
                                    "add", "Synthetic Windows verification 待辦測試"],
                                   cwd=PROJECT, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, "synthetic add command failed"
        with Store(root) as store:
            assert any(task["title"] == "Synthetic Windows verification 待辦測試" for task in store.list_tasks())
        results.append("CLI add persisted a synthetic task in an isolated database")
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
