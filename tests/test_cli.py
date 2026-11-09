import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from monitorbot import __main__ as cli
from monitorbot.config import load_config
from monitorbot.store import Store


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def call(self, *args, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main(["--home", str(self.home)] + list(args))
        output = stdout.getvalue() if code == 0 else stderr.getvalue()
        self.assertNotIn("Traceback", output)
        return code, json.loads(output)

    def ok(self, *args, **kwargs):
        code, result = self.call(*args, **kwargs)
        self.assertEqual(code, 0, result)
        return result

    def init(self):
        return self.ok("init")

    def test_init_starts_now_without_reading_sources_and_preserves_start(self):
        with patch.object(cli.sources, "collect", side_effect=AssertionError("must not read sources")):
            initial = self.init()
            repeated = self.ok("init", "--lookback-days", "3")
        self.assertEqual(initial["lookback_days"], 0)
        self.assertEqual(repeated["lookback_days"], 3)
        self.assertEqual(repeated["started_at"], initial["started_at"])
        with Store(self.home) as store:
            self.assertEqual(store.health()["manual"]["status"], "ok")

    def test_add_is_immediate_and_does_not_call_agent(self):
        self.init()
        with patch.object(cli.AgentRunner, "run", side_effect=AssertionError("must not call model")):
            task = self.ok("add", "有空研究 HDR", "--project", "Display")
        self.assertEqual(task["kind"], "optional")
        self.assertEqual(self.ok("task", "list")[0]["id"], task["id"])
        self.assertEqual(self.ok("task", "get", task["id"])["project"], "Display")
        with Store(self.home) as store:
            self.assertEqual(store.get_event(task["evidence_ids"][0])["source"], "manual")

    def test_menu_snapshot_reads_tasks_health_and_backlog_without_model(self):
        self.init()
        task = self.ok("add", "Read snapshot")
        with Store(self.home) as store:
            store.set_meta("last_poll", "2026-11-09T00:00:00+00:00")
        with patch.object(cli.AgentRunner, "run", side_effect=AssertionError("must not call model")), \
             patch.object(cli.install, "status", return_value={"supported": True, "loaded": False}) as status:
            snapshot = self.ok("menubar", "snapshot")
        self.assertEqual([task["id"]], [item["id"] for item in snapshot["tasks"]])
        self.assertEqual(0, snapshot["question_count"])
        self.assertEqual("ok", snapshot["source_health"]["manual"]["status"])
        self.assertIn("generated_at", snapshot)
        self.assertEqual("2026-11-09T00:00:00+00:00", snapshot["last_poll"])
        self.assertFalse(snapshot["background"]["loaded"])
        status.assert_called_once_with(self.home.resolve())

    def test_multilingual_cli_result_survives_legacy_windows_pipe_encoding(self):
        result = subprocess.run([sys.executable, "-m", "monitorbot", "--home", str(self.home),
                                 "add", "測試 Windows 任務"], env=dict(os.environ, PYTHONIOENCODING="cp1252"),
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("測試 Windows 任務", json.loads(result.stdout)["title"])

    def test_deploy_command_dispatches_without_opening_the_store(self):
        with patch.object(cli.install, "deploy", return_value={"deployed": True}) as deploy, \
             patch.object(cli, "Store", side_effect=AssertionError("must not open store before moving it")):
            self.assertTrue(self.ok("deploy")["deployed"])
        deploy.assert_called_once_with(self.home.resolve())

    def test_workspace_cli_uses_installed_runtime_and_one_database(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        shutil.copytree(Path(cli.__file__).parent, workspace / "monitorbot", ignore=shutil.ignore_patterns("__pycache__"))
        data = workspace / ".monitor"
        cli.initialize(data)
        user = self.home / "user"
        with patch.object(cli.install.Path, "home", return_value=user), \
             patch.object(cli.install.sys, "platform", "darwin"), \
             patch.object(cli.install, "stop", return_value={}):
            result = cli.install.deploy(data, project_dir=workspace)
        (workspace / "monitorbot/config.py").write_text("raise RuntimeError('workspace config must not load')\n")
        (workspace / "monitorbot/service.py").write_text("raise RuntimeError('workspace service must not load')\n")
        env = dict(os.environ, HOME=str(user), PYTHONDONTWRITEBYTECODE="1")
        env.pop("MONITOR_HOME", None)
        added = subprocess.run([sys.executable, "-m", "monitorbot", "--home=" + str(data), "add", "synthetic deployed task"],
                               cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(added.returncode, 0, added.stderr)
        task = json.loads(added.stdout)
        listed = subprocess.run([sys.executable, "-m", "monitorbot", "task", "list"],
                                cwd=result["project_dir"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([item["id"] for item in json.loads(listed.stdout)], [task["id"]])
        workspace_list = subprocess.run([sys.executable, "-m", "monitorbot", "task", "list"],
                                        cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(workspace_list.returncode, 0, workspace_list.stderr)
        self.assertEqual(json.loads(workspace_list.stdout), json.loads(listed.stdout))
        self.assertEqual(data.resolve(), Path(result["data_dir"]))
        self.assertTrue((Path(result["project_dir"]) / ".monitor").is_symlink())

    def test_invalid_add_does_not_leave_a_pending_event(self):
        self.init()
        code, error = self.call("add", "Task", "--due-at", "2026-12-01T10:00:00")
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["type"], "ValueError")
        with Store(self.home) as store:
            self.assertEqual(store.count_events(), 0)

    def test_task_updates_accept_stdin_json_and_reject_stale_revisions(self):
        self.init()
        task = self.ok("add", "Write report")
        updated = self.ok("task", "update", task["id"], "--json", "-", "--expected-revision", "1",
                          stdin='{"next_action":"Collect results","kind":"assigned"}')
        self.assertEqual(updated["revision"], 2)
        code, error = self.call("task", "update", task["id"], "--json", '{"priority":90}',
                                "--expected-revision", "1")
        self.assertEqual(code, 2)
        self.assertIn("stale", error["error"]["message"])

    def test_agent_state_changes_require_review_and_user_changes_are_explicit(self):
        self.init()
        task = self.ok("add", "Deliver report")
        code, _ = self.call("task", "transition", task["id"], "completed", "--reason", "sent", "--actor", "agent")
        self.assertEqual(code, 2)
        finished = self.ok("task", "transition", task["id"], "completed", "--reason", "I sent it")
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(self.ok("task", "list"), [])
        self.assertEqual(len(self.ok("task", "list", "--all")), 1)
        self.assertEqual(self.ok("task", "history", task["id"])[-1]["actor"], "user")

    def test_knowledge_upsert_accepts_file_and_preserves_versions(self):
        self.init()
        path = self.home / "input.json"
        path.write_text(json.dumps({"topic": "HDR", "content": "Mechanism X", "stage": "implemented"}))
        record = self.ok("knowledge", "upsert", "--json", "@" + str(path))
        self.assertEqual(self.ok("knowledge", "list", "--topic", "hdr")[0]["id"], record["id"])
        self.assertEqual(len(self.ok("knowledge", "history", record["id"])), 1)

    def test_answer_records_choice_and_preference_without_closing_task(self):
        self.init()
        task = self.ok("add", "Maybe investigate")
        with Store(self.home) as store:
            question = store.upsert_question({"text": "Keep?", "choices": ["Keep", "Cancel"],
                "task_id": task["id"], "rule_key": "investigation", "scope": "Display"})
        answer = self.ok("question", "answer", question["id"], "--choice", "2", "--always")
        self.assertEqual(answer["answer"], "Cancel")
        self.assertEqual(self.ok("question", "list"), [])
        self.assertEqual(self.ok("task", "get", task["id"])["status"], "active")

    def test_invalid_config_patch_does_not_replace_valid_file(self):
        self.init()
        original = (self.home / "config.json").read_bytes()
        code, error = self.call("config", "set", "--json", '{"poll_seconds":0}')
        self.assertEqual(code, 2)
        self.assertIn("poll_seconds", error["error"]["message"])
        self.assertEqual((self.home / "config.json").read_bytes(), original)
        updated = self.ok("config", "set", "--json", '{"schedule":{"question_limit":8}}')
        self.assertEqual(updated["schedule"]["question_limit"], 8)
        self.assertEqual(updated["schedule"]["backlog_limit"], 20)

    def test_source_filters_apply_to_existing_tasks_immediately(self):
        self.init()
        with Store(self.home) as store:
            event = store.add_event({"source": "teams", "source_id": "m1", "title": "Notice",
                "occurred_at": "2026-10-01T09:00:00Z", "metadata": {"channel_id": "noise"}})
            task = store.upsert_task({"title": "Noise task", "evidence_ids": [event["id"]]})
        self.ok("filter", "add", "teams", "exclude", "--json", '{"channel_id":"noise"}')
        preview = self.ok("filter", "preview", "teams")
        self.assertEqual(preview["excluded_event_ids"], [event["id"]])
        self.assertEqual(self.ok("task", "list"), [])
        self.assertEqual(self.ok("task", "get", task["id"])["excluded_by"], "source")
        self.ok("filter", "remove", "teams", "exclude", "1")
        with Store(self.home) as store:
            self.assertFalse(store.get_event(event["id"])["excluded"])
        self.assertEqual(self.ok("task", "list"), [])

    def test_config_source_patch_also_applies_filters(self):
        self.init()
        with Store(self.home) as store:
            event = store.add_event({"source": "outlook", "source_id": "m1", "title": "Notice",
                "occurred_at": "2026-10-01T09:00:00Z", "metadata": {}})
            store.upsert_task({"title": "Mail task", "evidence_ids": [event["id"]]})
        self.ok("config", "set", "--json", '{"sources":{"outlook":{"enabled":false}}}')
        self.assertEqual(self.ok("task", "list"), [])

    def test_doctor_does_not_read_token_contents(self):
        self.init()
        token = self.home / "microsoft-auth.json"
        token.write_text("THIS IS NOT JSON AND MUST NOT BE READ")
        token.chmod(0o600)
        with patch.object(cli.shutil, "which", return_value=None):
            result = self.ok("doctor")
        self.assertTrue(result["microsoft"]["token_file_exists"])
        self.assertTrue(result["microsoft"]["token_file_private"])
        self.assertFalse(result["agents"]["codex"]["available"])

    def test_poll_run_and_tick_dispatch_without_real_collection(self):
        self.init()
        with patch.object(cli, "run_cycle", return_value={"processed": 0}) as run:
            self.assertEqual(self.ok("poll"), {"processed": 0})
            self.assertEqual(self.ok("run", "--once"), {"processed": 0})
        self.assertEqual(run.call_count, 2)
        self.assertTrue((self.home / "service.lock").is_file())
        with patch.object(cli, "tick", return_value={"idle": True}):
            self.assertEqual(self.ok("tick"), {"idle": True})

    def test_report_refreshes_collection_before_report(self):
        self.init()
        calls = []
        with patch.object(cli, "run_cycle", side_effect=lambda *args: calls.append("poll")), \
             patch.object(cli, "write_reports", side_effect=lambda *args: calls.append("report") or {"weekly": "file.md"}):
            self.assertEqual(self.ok("report"), {"weekly": "file.md"})
        self.assertEqual(calls, ["poll", "report"])

    def test_agent_check_saves_only_returned_audit_without_real_model(self):
        self.init()
        result = {"ok": True, "privacy_verified": True, "backend": "codex", "fingerprint": "test-fingerprint"}
        with patch.object(cli.AgentRunner, "smoke_test", return_value=result):
            self.assertEqual(self.ok("agent-check", "--backend", "codex"), result)
        self.assertEqual(load_config(self.home)["agents"]["privacy_verified"]["codex"], result)

    def test_ask_uses_the_query_service(self):
        self.init()
        with patch("monitorbot.service.answer_query", return_value={"answer": "Mechanism X"}) as answer:
            self.assertEqual(self.ok("ask", "HDR目前怎麼做？"), {"answer": "Mechanism X"})
        self.assertEqual(answer.call_args.args[2], "HDR目前怎麼做？")

    def test_agent_proposals_go_through_service_review(self):
        self.init()
        plan = {"tasks": [], "knowledge": [], "questions": []}
        with patch("monitorbot.service.apply_proposal", return_value={"applied": True}) as apply:
            result = self.ok("propose", "--json", json.dumps(plan))
        self.assertTrue(result["applied"])
        self.assertEqual(apply.call_args.args[2], plan)

    def test_agent_errors_do_not_echo_sensitive_exception_text(self):
        self.init()
        with patch.object(cli, "run_cycle", side_effect=cli.AgentError("SECRET_RAW_BODY")):
            code, result = self.call("poll")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["type"], "AgentError")
        self.assertNotIn("SECRET_RAW_BODY", json.dumps(result))

    def test_schema_is_available_without_initializing_data_directory(self):
        result = self.ok("schema")
        self.assertEqual(set(result["properties"]), {"tasks", "knowledge", "questions"})
        self.assertFalse((self.home / "monitor.sqlite3").exists())

    def test_source_read_obeys_current_exclusions(self):
        self.init()
        with Store(self.home) as store:
            event = store.add_event({"source": "teams", "source_id": "m1", "title": "Message",
                "occurred_at": "2026-10-01T09:00:00Z", "metadata": {}})
            store.reconcile_exclusions([event["id"]])
        with patch.object(cli.sources, "read_content", side_effect=AssertionError("must not read excluded source")):
            code, error = self.call("source", "read", event["id"])
        self.assertEqual(code, 2)
        self.assertIn("excluded", error["error"]["message"])

    def test_microsoft_device_flow_is_two_commands_without_wait_loop(self):
        self.init()
        response = {"device_code": "PRIVATE_DEVICE_CODE", "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 600, "interval": 5}
        with patch.object(cli.sources, "start_microsoft_login", return_value=response), patch.object(cli.time, "time", return_value=1000):
            result = self.ok("source", "microsoft", "login-start")
        self.assertNotIn("device_code", result)
        pending = self.home / "microsoft-login.json"
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        with patch.object(cli.time, "time", return_value=1001), patch.object(cli.sources, "finish_microsoft_login") as finish:
            result = self.ok("source", "microsoft", "login-poll")
        self.assertEqual(result["status"], "waiting_interval")
        finish.assert_not_called()
        with patch.object(cli.time, "time", return_value=1006), \
             patch.object(cli.sources, "finish_microsoft_login", return_value={"authenticated": True}):
            self.assertTrue(self.ok("source", "microsoft", "login-poll")["authenticated"])
        self.assertFalse(pending.exists())

    def test_cleanup_is_preview_then_explicit_confirmed_apply(self):
        self.init()
        with Store(self.home) as store:
            store.log("old", "Old issue", now="2026-01-01T00:00:00Z")
        preview = self.ok("cleanup", "preview", "--before", "2026-02-01T00:00:00Z", "--types", "logs")
        self.assertEqual(preview["counts"]["logs"], 1)
        result = self.ok("cleanup", "apply", "--before", "2026-02-01T00:00:00Z", "--types", "logs",
                         "--confirmation", preview["confirmation_token"])
        self.assertFalse(result["dry_run"])

    def test_parse_errors_are_structured_and_nonzero(self):
        code, result = self.call("not-a-command")
        self.assertEqual(code, 2)
        self.assertIn("error", result)
        code, result = self.call("init", "--lookback-days", "-1")
        self.assertEqual(code, 2)
        self.assertIn("negative", result["error"]["message"])


if __name__ == "__main__":
    unittest.main()
