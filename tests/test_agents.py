import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitorbot.agents import AgentError, AgentRunner, validate_json


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = AgentRunner({}, Path(self.temp.name))

    def process(self, stdout='{"answer":"ok"}', stderr="", returncode=0):
        process = Mock()
        process.returncode = returncode
        process.communicate.return_value = (stdout, stderr)
        return process

    def test_codex_input_is_stdin_and_transient_directory_has_no_prompt(self):
        process = self.process()
        observed = {}

        def launch(command, **kwargs):
            observed.update(command=command, kwargs=kwargs)
            files = list(Path(kwargs["cwd"]).iterdir())
            self.assertEqual([p.name for p in files], ["schema.json"])
            self.assertNotIn("PRIVATE_SOURCE", files[0].read_text())
            return process

        with patch("monitorbot.agents.subprocess.Popen", side_effect=launch):
            self.assertEqual(self.runner.run("tracker", {"content": "PRIVATE_SOURCE"}, SCHEMA), {"answer": "ok"})
        self.assertIn("PRIVATE_SOURCE", process.communicate.call_args.args[0])
        self.assertNotIn("PRIVATE_SOURCE", " ".join(observed["command"]))
        self.assertIn("--ephemeral", observed["command"])
        self.assertIn("read-only", observed["command"])
        self.assertFalse(Path(observed["kwargs"]["cwd"]).exists())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_api_keys_are_not_forwarded_or_replaced_with_another_auth_mode(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret",
                                     "ANTHROPIC_AUTH_TOKEN": "secret"}):
            with patch("monitorbot.agents.subprocess.Popen", return_value=self.process()) as launch:
                self.runner.run("tracker", {}, SCHEMA)
        env = launch.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_claude_structure_and_no_tools(self):
        runner = AgentRunner({"agents": {"default": {"backend": "claude"}}}, self.temp.name)
        response = json.dumps({"is_error": False, "structured_output": {"answer": "ok"}})
        with patch("monitorbot.agents.subprocess.Popen", return_value=self.process(response)) as launch:
            self.assertEqual(runner.run("reviewer", {}, SCHEMA), {"answer": "ok"})
        args = launch.call_args.args[0]
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--safe-mode", args)
        self.assertNotIn("--bare", args)

    def test_gerrit_source_token_is_not_inherited_by_model_cli(self):
        self.runner.config["sources"] = {"gerrit": {"token_env": "monitor_test_gerrit_token"}}
        with patch.dict(os.environ, MONITOR_TEST_GERRIT_TOKEN="source-secret"), \
             patch("monitorbot.agents.subprocess.Popen", return_value=self.process()) as launch:
            self.runner.run("tracker", {}, SCHEMA)
        self.assertNotIn("MONITOR_TEST_GERRIT_TOKEN", launch.call_args.kwargs["env"])

    def test_failed_cli_does_not_leak_stdout_or_stderr(self):
        with patch("monitorbot.agents.subprocess.Popen", return_value=self.process("SECRET", "SECRET", 1)):
            with self.assertRaises(AgentError) as error:
                self.runner.run("tracker", {"content": "SECRET"}, SCHEMA)
        self.assertNotIn("SECRET", str(error.exception))

    def test_rejects_fences_trailing_text_unknown_fields_and_nonfinite_numbers(self):
        for output in ('```json\n{"answer":"ok"}\n```', '{"answer":"ok"} trailing',
                       '{"answer":"ok","unknown":1}', '{"answer":NaN}'):
            with self.subTest(output=output):
                with patch("monitorbot.agents.subprocess.Popen", return_value=self.process(output)):
                    with self.assertRaises(AgentError):
                        self.runner.run("tracker", {}, SCHEMA)

    def test_timeout_terminates_process_group(self):
        process = self.process()
        process.pid = 123456
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 1), ("", "")]
        with patch("monitorbot.agents.subprocess.Popen", return_value=process):
            with patch("monitorbot.agents.os.killpg") as kill:
                with self.assertRaisesRegex(AgentError, "timed out"):
                    self.runner.run("tracker", {}, SCHEMA)
        if os.name == "posix":
            kill.assert_called_once()

    def test_role_config_overrides_default_without_model_guessing(self):
        runner = AgentRunner({"agents": {"default": {"backend": "codex"},
                             "roles": {"reviewer": {"backend": "claude", "model": "chosen-model"}}}}, self.temp.name)
        self.assertEqual(runner.settings("tracker")["backend"], "codex")
        self.assertEqual(runner.settings("reviewer")["model"], "chosen-model")

    def test_source_content_is_blocked_before_launch_until_current_backend_is_verified(self):
        payload = {"events": [{"source": "outlook", "content": "private source"}]}
        with patch("monitorbot.agents.subprocess.Popen") as launch:
            with self.assertRaisesRegex(AgentError, "persistence audit"):
                self.runner.run("tracker", payload, SCHEMA)
            launch.assert_not_called()
        self.runner.config["agents"] = {"privacy_verified": {
            "codex": {"privacy_verified": True, "fingerprint": "verified-binary"}}}
        with patch.object(self.runner, "fingerprint", return_value="changed-binary"):
            with self.assertRaisesRegex(AgentError, "persistence audit"):
                self.runner.run("tracker", payload, SCHEMA)
        with patch.object(self.runner, "fingerprint", return_value="verified-binary"):
            with patch("monitorbot.agents.subprocess.Popen", return_value=self.process()):
                self.assertEqual(self.runner.run("tracker", payload, SCHEMA), {"answer": "ok"})

    def test_schema_handles_nullable_nested_and_required_fields(self):
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"due": {"type": ["string", "null"]}},
            "required": ["due"], "additionalProperties": False}}}, "required": ["items"]}
        validate_json({"items": [{"due": None}]}, schema)
        with self.assertRaises(AgentError):
            validate_json({"items": [{}]}, schema)

    @unittest.skipUnless(os.name == "posix", "Executable fixture uses a POSIX shebang")
    def test_real_subprocess_receives_stdin_and_returns_validated_json(self):
        executable = Path(self.temp.name) / "synthetic-cli"
        executable.write_text("#!" + sys.executable + "\n"
                              "import json,sys\n"
                              "payload=json.loads(sys.stdin.read().split('INPUT_JSON\\n',1)[1])\n"
                              "print(json.dumps({'answer': 'received' if payload['content']=='synthetic' else 'wrong'}))\n")
        executable.chmod(0o700)
        runner = AgentRunner({"agents": {"default": {"executable": str(executable)}}}, self.temp.name)
        self.assertEqual(runner.run("tracker", {"content": "synthetic"}, SCHEMA), {"answer": "received"})

    def test_smoke_audit_detects_persisted_marker_without_reading_unchanged_files(self):
        root = Path(self.temp.name) / "sessions"
        root.mkdir()
        old = root / "old.jsonl"
        old.write_text("existing private transcript")

        def synthetic_run(role, payload, schema):
            marker = payload["return_exact_marker"]
            (root / "new.jsonl").write_text(marker)
            return {"marker": marker}

        original = Path.read_bytes
        reads = []

        def read(path):
            reads.append(path)
            return original(path)

        with patch.object(self.runner, "_audit_roots", return_value=[root]):
            with patch.object(self.runner, "fingerprint", return_value="test-binary"):
                with patch.object(self.runner, "run", side_effect=synthetic_run):
                    with patch.object(Path, "read_bytes", read):
                        result = self.runner.smoke_test()
        self.assertFalse(result["privacy_verified"])
        self.assertNotIn(old, reads)
        self.assertEqual(result["persisted_marker_paths"], [str(root / "new.jsonl")])


if __name__ == "__main__":
    unittest.main()
