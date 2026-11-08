import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitorbot.improve import (MIGRATION_DRIVER, TEST_DRIVER, VerificationBlocked, _allowed, _apply,
                                _migration_evidence, _profile, _proposal_changes, _snapshot, _source_files, _verify, _write, improve, recover_pending)
from monitorbot.store import Store


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "project").resolve()
        self.root.mkdir()
        (self.root / "monitorbot").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "monitorbot" / "example.py").write_text("VALUE = 1\n")
        (self.root / "tests" / "test_example.py").write_text(
            "import unittest\nclass Check(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n")
        self.data = Path(self.temp.name) / "data"
        self.store = Store(self.data)
        self.addCleanup(self.store.close)
        self.store.log("source_failure", {"source": "chrome", "error": "fixture"})
        self.config = {"data_dir": str(self.data), "improvement": {
            "enabled": True, "auto_apply": True, "project_dir": str(self.root)}}

    def proposal(self, content="VALUE = 2\n", reason="修正實際問題"):
        return {"summary": "修正來源處理", "evidence_log_ids": [], "changes": [
            {"path": "monitorbot/example.py", "content": content, "reason": reason}]}

    def runner(self, proposal=None, approved=True):
        runner = Mock()
        runner.run.side_effect = [proposal or self.proposal(), {"concerns": [], "constraints": []},
                                  {"approved": approved, "reason": "已檢查修改與驗證"}]
        return runner

    def test_paths_and_existing_baselines_are_protected(self):
        for name in ("../outside.py", "/tmp/outside.py", "monitorbot/../config.py", "config.json",
                     ".monitor/config.json", "monitorbot/improve.py", "monitorbot/nested/code.py"):
            self.assertFalse(_allowed(name, self.root), name)
        original = _source_files(self.root)
        proposal = self.proposal()
        proposal["changes"][0]["path"] = "tests/test_example.py"
        with self.assertRaisesRegex(VerificationBlocked, "immutable"):
            _proposal_changes(proposal, original, self.root)
        link = self.root / "monitorbot" / "link.py"
        link.symlink_to(Path(self.temp.name) / "outside")
        self.assertFalse(_allowed("monitorbot/link.py", self.root))

    def test_missing_os_sandbox_blocks_autoapply_and_records_issue(self):
        runner = self.runner()
        with patch("monitorbot.improve.shutil.which", return_value=None):
            result = improve(self.store, self.config, runner)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("sandbox-exec", result["reason"])
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 1\n")
        self.assertEqual(runner.run.call_count, 2)
        artifacts = Path(result["artifact_dir"])
        self.assertTrue((artifacts / "change.diff").exists())
        self.assertTrue((artifacts / "database-backup.sqlite3").exists())
        issue = self.store.get_meta("improvement_issues")[result["issue_id"]]
        self.assertEqual(issue["status"], "in_progress")

    def test_identical_failed_patch_is_not_retried_after_new_evidence(self):
        with patch("monitorbot.improve._verify", side_effect=VerificationBlocked("fixture failure")) as verify:
            first = improve(self.store, self.config, self.runner())
            self.store.log("new_evidence", {"source": "chrome"})
            second = improve(self.store, self.config, self.runner(self.proposal(reason="同修正不同文字")))
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(first["proposal_hash"], second["proposal_hash"])
        self.assertEqual(verify.call_count, 1)

    def test_original_problem_review_happens_without_patch(self):
        runner = self.runner()
        with patch("monitorbot.improve._verify", side_effect=VerificationBlocked("fixture failure")):
            improve(self.store, self.config, runner)
        review_payload = runner.run.call_args_list[1].args[1]
        self.assertIn("original_code", review_payload)
        self.assertNotIn("patch", review_payload)
        self.assertNotIn("changes", review_payload)

    def test_reviewer_rejection_does_not_apply_even_after_test_success(self):
        with patch("monitorbot.improve._verify", return_value={"tests": {"passed": True}}):
            result = improve(self.store, self.config, self.runner(approved=False))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("review rejected", result["reason"])
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 1\n")

    def test_autoapply_requires_verified_artifacts_and_keeps_rollback(self):
        evidence = {"sandbox": {"network_denied": True}, "tests": {"passed": True},
                    "migration": {"preserved": True}}
        with patch("monitorbot.improve._verify", return_value=evidence):
            result = improve(self.store, self.config, self.runner())
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 2\n")
        artifact = Path(result["artifact_dir"])
        self.assertEqual(json.loads((artifact / "rollback.json").read_text())["status"], "applied")
        self.assertEqual((artifact / "original/monitorbot/example.py").read_text(), "VALUE = 1\n")
        self.assertEqual(self.store.get_meta("improvement_issues")[result["issue_id"]]["status"], "verified")

    def test_stale_code_or_baseline_prevents_application(self):
        original = _source_files(self.root)
        (self.root / "tests/test_example.py").write_text("# concurrent edit\n")
        with self.assertRaisesRegex(VerificationBlocked, "changed during verification"):
            _apply(self.root, self.proposal()["changes"], original, self.data / "artifact")
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 1\n")

    def test_user_data_updates_during_review_are_preserved_and_block_stale_application(self):
        runner = self.runner()
        calls = []

        def run(role, payload, schema):
            calls.append(role)
            if len(calls) == 1:
                return self.proposal()
            if len(calls) == 2:
                return {"concerns": [], "constraints": []}
            self.store.log("concurrent_user_answer", {"answer": "synthetic"})
            return {"approved": True, "reason": "fixture"}

        runner.run.side_effect = run
        with patch("monitorbot.improve._verify", return_value={"tests": {"passed": True}}):
            result = improve(self.store, self.config, runner)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("live data changed", result["reason"])
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 1\n")
        self.assertIn("concurrent_user_answer", [item["kind"] for item in self.store.logs()])

    def test_snapshot_is_restorable_and_does_not_change_live_database(self):
        destination = self.data / "backup.sqlite3"
        _snapshot(self.store.path, destination)
        connection = sqlite3.connect(str(destination))
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT count(*) FROM logs").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertFalse(destination.with_suffix(".restore-check.sqlite3").exists())
        self.assertEqual(len(self.store.logs()), 1)

    def test_interrupted_update_rolls_back_but_never_overwrites_later_user_edits(self):
        artifact = self.data / "improvements" / "test"
        artifact.mkdir(parents=True)
        record = {"status": "prepared", "files": {"monitorbot/example.py": {
            "before": "VALUE = 1\n", "after": "VALUE = 2\n"}}}
        journal = artifact / "rollback.json"
        journal.write_text(json.dumps(record))
        (self.root / "monitorbot/example.py").write_text("VALUE = 2\n")
        self.assertEqual(recover_pending(self.root, self.data), ["test"])
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 1\n")
        journal.write_text(json.dumps(record))
        (self.root / "monitorbot/example.py").write_text("VALUE = 3\n")
        with self.assertRaisesRegex(VerificationBlocked, "subsequent edits"):
            recover_pending(self.root, self.data)
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 3\n")

    def test_partial_file_application_rolls_back_already_replaced_files(self):
        (self.root / "monitorbot/other.py").write_text("OTHER = 1\n")
        original = _source_files(self.root)
        changes = self.proposal()["changes"] + [
            {"path": "monitorbot/other.py", "content": "OTHER = 2\n", "reason": "fixture"}]
        artifact = self.data / "artifact"
        failed = []

        def write(path, content):
            if path == self.root / "monitorbot/other.py" and not failed:
                failed.append(True)
                raise OSError("synthetic replacement failure")
            return _write(path, content)

        with patch("monitorbot.improve._write", side_effect=write):
            with self.assertRaises(OSError):
                _apply(self.root, changes, original, artifact)
        self.assertEqual((self.root / "monitorbot/example.py").read_text(), "VALUE = 1\n")
        self.assertEqual((self.root / "monitorbot/other.py").read_text(), "OTHER = 1\n")
        self.assertEqual(json.loads((artifact / "rollback.json").read_text())["status"], "rolled_back")

    def test_trusted_migration_driver_detects_data_loss_on_a_synthetic_database(self):
        (self.root / "monitorbot/__init__.py").write_text("")
        code = ("import sqlite3\nclass Store:\n"
                "    def __init__(self,path):\n"
                "        self.db=sqlite3.connect(str(path/'monitor.sqlite3'))\n"
                "        {operation}\n"
                "    def close(self): self.db.close()\n")
        driver = Path(self.temp.name) / "migration-driver.py"
        driver.write_text(MIGRATION_DRIVER)
        copied_data = Path(self.temp.name) / "copied-data"
        copied_data.mkdir()
        _snapshot(self.store.path, copied_data / "monitor.sqlite3")
        for operation, expected in (("pass", True), ("self.db.execute('DELETE FROM logs'); self.db.commit()", False)):
            (self.root / "monitorbot/store.py").write_text(code.format(operation=operation))
            result = subprocess.run([sys.executable, "-I", "-B", str(driver), str(self.root), str(copied_data)],
                                    capture_output=True, check=True, text=True)
            self.assertEqual(json.loads(result.stdout)["preserved"], expected)
        self.assertEqual(len(self.store.logs()), 1)

    def test_trusted_test_driver_runs_unchanged_baseline_and_new_tests(self):
        baseline = Path(self.temp.name) / "baseline_tests"
        baseline.mkdir()
        (baseline / "test_example.py").write_text((self.root / "tests/test_example.py").read_text())
        (self.root / "tests/test_added.py").write_text(
            "import unittest\nclass Check(unittest.TestCase):\n    def test_added(self): self.assertTrue(False)\n")
        driver = Path(self.temp.name) / "test-driver.py"
        driver.write_text(TEST_DRIVER)
        for mode, passed in (("baseline", True), ("added", False)):
            result = subprocess.run([sys.executable, "-I", "-B", str(driver), str(self.root), str(baseline), mode],
                                    capture_output=True, check=True, text=True)
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence["passed"], passed)
            self.assertEqual(evidence["baseline_tests"], 1 if mode == "baseline" else 0)
            self.assertEqual(evidence["tests_run"], 1)
            self.assertTrue(evidence["oracle_intact"])

    def test_candidate_cannot_turn_a_failing_baseline_into_success_by_replacing_runner(self):
        (self.root / "monitorbot/__init__.py").write_text("")
        (self.root / "monitorbot/store.py").write_text(
            "import unittest\nfrom types import SimpleNamespace\n"
            "unittest.TextTestRunner.run=lambda *a,**k: SimpleNamespace("
            "wasSuccessful=lambda:True,testsRun=1,failures=[],errors=[])\n")
        baseline = Path(self.temp.name) / "baseline_tests"
        baseline.mkdir()
        failing = ("import unittest\nfrom monitorbot import store\n"
                   "class Check(unittest.TestCase):\n"
                   "    def test_must_fail(self): self.assertTrue(False)\n")
        (baseline / "test_example.py").write_text(failing)
        (self.root / "tests/test_example.py").write_text(failing)
        driver = Path(self.temp.name) / "test-driver.py"
        driver.write_text(TEST_DRIVER)
        result = subprocess.run([sys.executable, "-I", "-B", str(driver), str(self.root), str(baseline), "baseline"],
                                capture_output=True, check=True, text=True)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["oracle_intact"])
        self.assertEqual(evidence["tests_run"], 0)

    def test_failure_canary_blocks_candidate_that_hides_failures_during_test_execution(self):
        baseline = Path(self.temp.name) / "baseline_tests"
        baseline.mkdir()
        code = ("import unittest\nclass Check(unittest.TestCase):\n"
                "    def test_hide_failure(self):\n"
                "        unittest.TestResult.addFailure=lambda *args:None\n"
                "        self.fail('must be observed')\n")
        for directory in (baseline, self.root / "tests"):
            (directory / "test_example.py").write_text(code)
        driver = Path(self.temp.name) / "test-driver.py"
        driver.write_text(TEST_DRIVER)
        result = subprocess.run([sys.executable, "-I", "-B", str(driver), str(self.root), str(baseline), "baseline"],
                                capture_output=True, check=True, text=True)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["oracle_intact"])

    def test_parent_inspects_actual_migrated_data_even_when_child_claims_success(self):
        backup = self.data / "backup.sqlite3"
        _snapshot(self.store.path, backup)

        def fake_sandbox(candidate, control, script, arguments=(), **kwargs):
            if script.name == "probe.py":
                return {"outside_read_denied": True, "outside_write_denied": True, "network_denied": True}
            if script.name == "tests.py":
                return {"passed": True, "baseline_tests": 1}
            connection = sqlite3.connect(str(candidate / ".migration-data/monitor.sqlite3"))
            connection.execute("DELETE FROM logs")
            connection.commit()
            connection.close()
            return {"preserved": True, "integrity": True, "tables_checked": 100}

        with patch("monitorbot.improve._sandbox", side_effect=fake_sandbox):
            with self.assertRaisesRegex(VerificationBlocked, "did not preserve"):
                _verify(self.root, _source_files(self.root), [], backup)
        self.assertEqual(1, len(self.store.logs()))

    def test_parent_migration_check_allows_additive_schema_but_blocks_missing_columns(self):
        backup = self.data / "backup.sqlite3"
        migrated = self.data / "migrated.sqlite3"
        _snapshot(self.store.path, backup)
        _snapshot(self.store.path, migrated)
        connection = sqlite3.connect(str(migrated))
        connection.execute("ALTER TABLE logs ADD COLUMN extra TEXT")
        connection.execute("CREATE TABLE new_feature (id TEXT)")
        connection.commit()
        connection.close()
        evidence = _migration_evidence(backup, migrated)
        self.assertTrue(evidence["preserved"])
        self.assertTrue(evidence["integrity"])
        self.assertEqual("parent_sqlite", evidence["verified_by"])
        connection = sqlite3.connect(str(migrated))
        connection.execute("ALTER TABLE logs RENAME COLUMN data TO replaced_data")
        connection.commit()
        connection.close()
        self.assertFalse(_migration_evidence(backup, migrated)["preserved"])

    def test_profile_resolves_paths_and_only_allows_literal_root_data(self):
        link = Path(self.temp.name) / "candidate-link"
        link.symlink_to(self.root, target_is_directory=True)
        profile = _profile(link, [link / "tests"])
        self.assertIn('(allow file-read-data (literal "/"))', profile)
        self.assertIn('(subpath ' + json.dumps(str(self.root)) + ')', profile)
        self.assertNotIn(str(link), profile)
        self.assertNotIn('(subpath "/")', profile)
        self.assertNotIn('mach-lookup', profile)

    def test_no_changes_keeps_issue_discovered_without_pretending_fixed(self):
        proposal = {"summary": "缺登入，沒有可用程式修正", "evidence_log_ids": [], "changes": []}
        result = improve(self.store, self.config, self.runner(proposal))
        self.assertEqual(result["status"], "skipped")
        issue = self.store.get_meta("improvement_issues")[result["issue_id"]]
        self.assertEqual(issue["status"], "discovered")


if __name__ == "__main__":
    unittest.main()
