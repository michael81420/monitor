import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from monitorbot import desktop, service
from monitorbot.agents import AgentRunner, validate_json
from monitorbot.config import initialize, load_config, save_config
from monitorbot.reports import previous_week
from monitorbot.store import Store


class CheckedRunner:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def run(self, role, payload, schema):
        if len(json.dumps(payload, ensure_ascii=False)) > 120000:
            raise AssertionError("agent received an oversized payload")
        self.calls.append((role, copy.deepcopy(payload)))
        result = self.handler(role, payload)
        validate_json(result, schema)
        return result


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = initialize(self.root)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)

    def questions(self):
        return [self.store.upsert_question({
            "text": str(index) + "q" * 1000,
            "choices": [str(choice) + "c" * 1000 for choice in range(10)],
        }) for index in range(60)]

    def clarifier(self, role, payload):
        self.assertEqual("clarifier", role)
        self.assertLessEqual(len(payload["questions"]), 5)
        return {"questions": [
            {"id": q["id"], "status": "open", "text": q["text"],
             "choices": q["choices"], "suggested_index": 0, "reason": "Still needs an answer"}
            for q in payload["questions"]]}

    def test_long_question_backlog_is_bounded_and_only_twenty_are_reviewed(self):
        self.questions()
        context = service._context(self.store, self.config, [])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 120000)
        runner = CheckedRunner(self.clarifier)
        self.assertTrue(service.refresh_questions(self.store, self.config, runner))
        reviewed = self.store.get_meta("daily_reviewed_questions")
        requested = [q["id"] for _, payload in runner.calls for q in payload["questions"]]
        self.assertEqual(4, len(runner.calls))
        self.assertEqual(20, len(set(requested)))
        self.assertEqual(set(requested), set(reviewed))
        self.assertEqual(60, len(self.store.list_questions()))

    def test_daily_dialog_never_presents_a_question_missing_from_latest_review(self):
        self.questions()
        runner = CheckedRunner(self.clarifier)
        prompts = []

        def choose(title, prompt, choices):
            if title.startswith("工作確認"):
                prompts.append(prompt)
                return choices[0]
            return "關閉"

        with patch.object(desktop.sys, "platform", "darwin"), \
             patch.object(desktop, "AgentRunner", return_value=runner), \
             patch.object(desktop, "choose", side_effect=choose):
            result = desktop.daily_review(self.store, self.config)
        reviewed = set(self.store.get_meta("daily_reviewed_questions"))
        answered = [q for q in self.store.list_questions(include_closed=True) if q["status"] == "answered"]
        self.assertEqual(20, len(reviewed))
        self.assertEqual(result["answered"], len(prompts))
        self.assertGreater(len(answered), 0)
        self.assertTrue({q["id"] for q in answered} <= reviewed)
        self.assertEqual({q["text"] for q in answered}, {p.split("\n建議：")[0] for p in prompts})

    def test_weekly_large_history_partitions_and_merges_without_losing_events(self):
        now = datetime(2026, 11, 9, 2, tzinfo=timezone.utc)
        start, _ = previous_week(now)
        expected = set()
        for index in range(601):
            event = self.store.add_event({"source": "manual", "source_id": str(index),
                "occurred_at": (start + timedelta(minutes=index)).isoformat(),
                "title": "work-" + str(index) + "x" * 1000, "metadata": {}})
            expected.add(event["id"])
        covered = {"weekly_work": set(), "weekly_system_check": set()}
        merged = set()

        def report(role, payload):
            self.assertEqual("reporter", role)
            kind, data = payload["report_kind"], payload["data"]
            self.assertEqual(601, data["coverage_counts"]["events"])
            ids = [event["id"] for event in data.get("events", [])]
            covered[kind].update(ids)
            if "section_drafts" in data:
                merged.add(kind)
                markdown = "\n".join(item["markdown"] for item in data["section_drafts"])
            else:
                self.assertIn("partition", data)
                markdown = "\n".join(ids) or "No events in this partition"
            return {"markdown": markdown, "conflicts": []}

        runner = CheckedRunner(report)
        paths = service.write_reports(self.store, self.config, runner, now=now)
        self.assertEqual(set(covered), merged)
        for kind, ids in covered.items():
            self.assertEqual(expected, ids, kind)
        for path in paths.values():
            text = Path(path).read_text(encoding="utf-8")
            self.assertTrue(all(identifier in text for identifier in expected))

    def test_smoke_audit_streams_large_logs_and_finds_marker_across_chunk_boundary(self):
        audit = self.root / "sessions"
        audit.mkdir()
        log = audit / "new.jsonl"
        runner = AgentRunner(self.config, self.root)
        reads = []
        original_open = Path.open

        class LimitedReader:
            def __enter__(self):
                self.handle = original_open(log, "rb")
                return self

            def __exit__(self, *args):
                self.handle.close()

            def read(self, size=-1):
                self_test.assertGreater(size, 0)
                self_test.assertLessEqual(size, 1024 * 1024)
                reads.append(size)
                return self.handle.read(size)

        self_test = self

        def fake_call(role, payload, schema):
            marker = payload["return_exact_marker"].encode()
            with original_open(log, "wb") as handle:
                handle.write(b"x" * (17 * 1024 * 1024 - 10))
                handle.write(marker)
                handle.write(b"x" * 100)
            return {"marker": marker.decode()}

        def open_file(path, *args, **kwargs):
            if path == log and args == ("rb",):
                return LimitedReader()
            return original_open(path, *args, **kwargs)

        with patch.object(runner, "_audit_roots", return_value=[audit]), \
             patch.object(runner, "fingerprint", return_value="synthetic"), \
             patch.object(runner, "run", side_effect=fake_call), \
             patch.object(Path, "open", open_file), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("must stream log")):
            result = runner.smoke_test()
        self.assertGreater(log.stat().st_size, 16 * 1024 * 1024)
        self.assertGreater(len(reads), 16)
        self.assertFalse(result["privacy_verified"])
        self.assertEqual([str(log)], result["persisted_marker_paths"])
        self.assertEqual([], result["unreadable_or_large_paths"])

    def test_cli_recovers_interrupted_service_edit_before_importing_it(self):
        copied = self.root / "checkout"
        copied.mkdir()
        source = Path(service.__file__).resolve().parent
        shutil.copytree(source, copied / "monitorbot", ignore=shutil.ignore_patterns("__pycache__"))
        target = copied / "monitorbot" / "service.py"
        before = target.read_text(encoding="utf-8")
        broken = "raise SyntaxError('synthetic interrupted service edit')\n"
        target.write_text(broken)
        home = self.root / "recovery-home"
        artifact = home / "improvements" / "synthetic"
        artifact.mkdir(parents=True)
        rollback = artifact / "rollback.json"
        rollback.write_text(json.dumps({"status": "prepared", "files": {
            "monitorbot/service.py": {"before": before, "after": broken}}}))
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        environment.pop("PYTHONPATH", None)
        for option in (["--home", str(home)], ["--home=" + str(home)]):
            with self.subTest(option=option):
                target.write_text(broken)
                record = json.loads(rollback.read_text(encoding="utf-8"))
                record["status"] = "prepared"
                rollback.write_text(json.dumps(record))
                result = subprocess.run([sys.executable, "-m", "monitorbot", *option, "schema"],
                    cwd=copied, env=environment, capture_output=True, text=True, timeout=20)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("tasks", json.loads(result.stdout)["properties"])
                self.assertEqual(before, target.read_text(encoding="utf-8"))
                self.assertEqual("rolled_back", json.loads(rollback.read_text(encoding="utf-8"))["status"])

    def test_invalid_filters_preserve_config_and_cleanup_starts_disabled(self):
        original = (self.root / "config.json").read_bytes()
        for rule in ({}, {"sender": []}, {"sender": 7}, {"": "*"}):
            with self.subTest(rule=rule):
                invalid = copy.deepcopy(self.config)
                invalid["sources"]["teams"]["exclude"] = [rule]
                with self.assertRaises(ValueError):
                    save_config(self.root, invalid)
                self.assertEqual(original, (self.root / "config.json").read_bytes())
        self.assertFalse(load_config(self.root)["cleanup"]["enabled"])
        self.store.log("synthetic", "keep this", now="2020-01-01T00:00:00Z")
        with patch.object(self.store, "cleanup", side_effect=AssertionError("cleanup must stay disabled")):
            self.assertIsNone(service.scheduled_cleanup(self.store, self.config, datetime.now(timezone.utc)))
        self.assertEqual(1, len(self.store.logs()))


if __name__ == "__main__":
    unittest.main()
