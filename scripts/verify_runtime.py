"""Run explicit synthetic CLI and OS-isolation checks without production content."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parent.parent
manifest = PROJECT / ".monitor" / "runtime.json"
if manifest.is_file():
    PROJECT = Path(json.loads(manifest.read_text(encoding="utf-8"))["project_dir"]).resolve()
sys.path.insert(0, str(PROJECT))

from monitorbot.agents import AgentRunner
from monitorbot.config import initialize, load_config
from monitorbot.improve import _source_files, _snapshot, _verify
from monitorbot.service import run_cycle
from monitorbot.store import Store


def isolation_check():
    original = _source_files(PROJECT)
    with tempfile.TemporaryDirectory(prefix="monitor-runtime-") as directory:
        root = Path(directory).resolve()
        candidate = root / "candidate"
        candidate.mkdir()
        for name, content in original.items():
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        with Store(root / "synthetic-data") as store:
            store.upsert_task({"title": "Synthetic waiting task", "status": "waiting", "kind": "assigned",
                               "waiting_for": "Synthetic developer"})
            _snapshot(store.path, root / "backup.sqlite3")
        return _verify(candidate, original, [], root / "backup.sqlite3")


def workflow_check(home):
    existing = load_config(home)
    with tempfile.TemporaryDirectory(prefix="monitor-workflow-") as directory:
        config = initialize(directory)
        config["agents"] = existing["agents"]
        for settings in config["sources"].values():
            settings["enabled"] = False
        with Store(directory) as store:
            store.add_event({"source": "manual", "source_id": "synthetic-workflow",
                "occurred_at": datetime.now(timezone.utc).isoformat(), "title":
                "合成測試任務：我負責 HDR IT 驗證，必須等開發負責人完成，預估兩個月後才能開始，尚無交付期限。"})
            result = run_cycle(store, config, AgentRunner(config, directory))
            tasks = store.list_tasks()
            if result["pending"] or not tasks or not any(task["status"] == "waiting" and
                    task["kind"] == "assigned" and task["due_at"] is None for task in tasks):
                raise RuntimeError("synthetic workflow did not establish the required waiting state")
            if store.idle_candidates():
                raise RuntimeError("waiting task was incorrectly eligible for idle cancellation")
            return {"passed": True, "tasks": len(tasks), "waiting_protected": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--isolation", action="store_true")
    parser.add_argument("--workflow", action="store_true", help="makes a real CLI model call with synthetic input")
    parser.add_argument("--home", default=str(PROJECT / ".monitor"))
    args = parser.parse_args()
    result = {}
    if args.isolation:
        result["isolation"] = isolation_check()
    if args.workflow:
        result["workflow"] = workflow_check(args.home)
    if not result:
        parser.error("choose --isolation or --workflow")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
