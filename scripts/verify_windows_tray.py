"""Exercise the real notification icon with isolated synthetic snapshots."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from monitorbot import windows_tray
from monitorbot.config import initialize, save_config
from monitorbot.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=PROJECT / ".audit/windows-tray-ui.json")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("requires an interactive Windows desktop")
    evidence = args.evidence.resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="monitor-tray-check-") as temporary:
        root = Path(temporary) / "合成資料 中文"
        root.mkdir()
        config = initialize(root)
        for settings in config["sources"].values():
            settings["enabled"] = False
        config["schedule"]["days"] = []
        config["schedule"]["popup"] = False
        config["improvement"]["enabled"] = False
        save_config(root, config)
        today = datetime.now(ZoneInfo(config["timezone"])).date()
        with Store(root) as store:
            store.set_meta("weekly_done", (today - timedelta(days=today.weekday())).isoformat())
        fixture = root / "snapshot.json"
        shutil.copyfile(PROJECT / "tests/fixtures/menubar.json", fixture)
        before = windows_tray.status(root)
        assert not before["loaded"]
        host = subprocess.Popen(windows_tray._command(root, "Run", snapshot=fixture, evidence=evidence),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        collector = subprocess.Popen([sys.executable, "-m", "monitorbot", "--home", str(root), "run", "--loop"],
                                     cwd=PROJECT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _, errors = host.communicate(timeout=30)
            assert host.returncode == 0, errors.decode("utf-8", "replace")
            result = json.loads(evidence.read_text(encoding="utf-8-sig"))
            assert result["completed"] and result["error"] is None, result
            assert not windows_tray.status(root)["loaded"], "quit left icon host alive"
            assert collector.poll() is None, "quitting icon stopped independent collector"
            with Store(root) as store:
                assert store.get_meta("last_poll"), "independent synthetic collector never ran"
            result["passed"].append("quitting icon preserves a running independent synthetic collector")
            result["passed"].append("quit closes the owned icon host")
            first = windows_tray.start(root, snapshot=fixture)
            second = windows_tray.start(root, snapshot=fixture)
            assert first["loaded"] and second["loaded"] and "pid" not in second
            result["passed"].append("duplicate start reuses one owned icon")
            assert not windows_tray.stop(root)["loaded"]
            assert not windows_tray.stop(root)["loaded"]
            result["passed"].append("graceful stop is idempotent")
            result["screenshot"] = str(evidence) + ".png"
            result["scope"] = "Synthetic snapshots only; no sources, model, or scheduled tasks accessed."
            evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=True, indent=2))
        finally:
            if host.poll() is None:
                host.terminate()
                host.wait(timeout=5)
            collector.terminate()
            collector.wait(timeout=5)
            windows_tray.stop(root)


if __name__ == "__main__":
    main()
