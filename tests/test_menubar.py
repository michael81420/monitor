import contextlib
import io
import json
from pathlib import Path
import plistlib
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from monitorbot import __main__ as cli
from monitorbot import install, menubar


class MenuBarTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.project = self.root / "app"
        self.data = self.root / "data"
        self.data.mkdir()
        (self.project / "native").mkdir(parents=True)
        (self.project / "native/MenuBar.swift").write_text("// synthetic compiler input\n")
        home = patch.object(menubar.Path, "home", return_value=self.root / "user")
        platform = patch.object(menubar.sys, "platform", "darwin")
        home.start()
        platform.start()
        self.addCleanup(home.stop)
        self.addCleanup(platform.stop)
        self.missing = subprocess.CompletedProcess([], 113, "", "")
        self.success = subprocess.CompletedProcess([], 0, "", "")
        self.running = subprocess.CompletedProcess([], 0, "state = running\npid = 123\n", "")

    def compile(self, arguments, **kwargs):
        self.assertEqual(arguments[1:4], ["-O", "-framework", "Cocoa"])
        self.assertEqual(arguments[4], "-module-cache-path")
        self.assertEqual(Path(arguments[5]).parent.parent, self.project / "native")
        Path(arguments[5]).mkdir()
        (Path(arguments[5]) / "synthetic.cache").write_bytes(b"compiler cache")
        Path(arguments[-1]).write_bytes(b"synthetic native binary")
        return self.success

    def prepare(self):
        with patch.object(menubar.shutil, "which", return_value="/synthetic/swiftc"), \
             patch.object(menubar.subprocess, "run", side_effect=self.compile):
            return menubar.prepare(self.data, project_dir=self.project)

    def test_prepare_builds_native_binary_and_separate_gui_plist_without_starting(self):
        prepared = self.prepare()
        definition = plistlib.loads(Path(prepared["path"]).read_bytes())
        self.assertEqual(prepared["label"], install._identity(self.data)[1] + ".menubar")
        self.assertEqual(definition["ProgramArguments"], [str(self.project / "native/Monitor.app/Contents/MacOS/MonitorMenuBar"),
            "--python", str(Path(menubar.sys.executable).resolve()), "--project", str(self.project), "--home", str(self.data)])
        self.assertEqual(definition["WorkingDirectory"], str(self.project))
        self.assertEqual(definition["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(definition["RunAtLoad"])
        self.assertNotIn("StartInterval", definition)
        self.assertEqual(definition["StandardOutPath"], "/dev/null")
        self.assertEqual(definition["StandardErrorPath"], str(self.data / "menubar-errors.log"))
        self.assertEqual(Path(prepared["path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(prepared["binary"]).stat().st_mode & 0o777, 0o700)
        bundle = plistlib.loads((Path(prepared["app"]) / "Contents/Info.plist").read_bytes())
        self.assertEqual(bundle["CFBundleIdentifier"], prepared["label"])
        self.assertTrue(bundle["LSUIElement"])
        self.assertEqual(bundle["CFBundleName"], "Monitor")
        self.assertFalse(prepared["started"])
        self.assertEqual(list((self.project / "native").glob(".menubar-build-*")), [])

    def test_missing_compiler_fails_without_creating_fake_binary_or_plist(self):
        with patch.object(menubar.shutil, "which", return_value=None), \
             patch.object(menubar.subprocess, "run", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(RuntimeError, "Swift compiler is unavailable"):
                menubar.prepare(self.data, project_dir=self.project)
        self.assertFalse((self.project / "native/Monitor.app").exists())
        self.assertFalse((self.data / "menubar-launchagent.plist").exists())

    def test_failed_compile_keeps_previous_binary(self):
        binary = Path(self.prepare()["binary"])
        binary.write_bytes(b"previous binary")
        with patch.object(menubar.shutil, "which", return_value="/synthetic/swiftc"), \
             patch.object(menubar.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "compile error")):
            with self.assertRaisesRegex(RuntimeError, "compilation failed"):
                menubar.build(self.data, self.project)
        self.assertEqual(binary.read_bytes(), b"previous binary")
        self.assertEqual(list((self.project / "native").glob(".menubar-build-*")), [])

    def test_foreign_app_is_not_overwritten(self):
        contents = self.project / "native/Monitor.app/Contents"
        contents.mkdir(parents=True)
        info = plistlib.dumps({"CFBundleIdentifier": "someone.else.app"})
        (contents / "Info.plist").write_bytes(info)
        with patch.object(menubar.shutil, "which", return_value="/synthetic/swiftc"), \
             patch.object(menubar.subprocess, "run", side_effect=AssertionError("must not compile")):
            with self.assertRaisesRegex(ValueError, "not owned"):
                menubar.build(self.data, self.project)
        self.assertEqual((contents / "Info.plist").read_bytes(), info)

    def test_start_bootstraps_own_gui_label_and_enables_login(self):
        prepared = self.prepare()
        with patch.object(menubar, "prepare", return_value=prepared) as prepare, \
             patch.object(menubar.subprocess, "run", side_effect=[self.missing, self.success, self.running]) as run:
            result = menubar.start(self.data)
        self.assertTrue(result["loaded"])
        self.assertTrue(result["login_enabled"])
        self.assertEqual(run.call_args_list[1].args[0], ["launchctl", "bootstrap", prepared["domain"], prepared["path"]])
        self.assertTrue(menubar._login_path(self.data).is_symlink())
        prepare.assert_called_once()

    def test_running_icon_is_not_recompiled_or_restarted(self):
        self.prepare()
        with patch.object(menubar, "prepare", side_effect=AssertionError("must not rebuild running icon")), \
             patch.object(menubar.subprocess, "run", return_value=self.running) as run:
            self.assertTrue(menubar.start(self.data)["loaded"])
        self.assertEqual(run.call_count, 1)

    def test_start_reopens_icon_after_successful_user_quit(self):
        prepared = self.prepare()
        waiting = subprocess.CompletedProcess([], 0, "state = not running\nlast exit code = 0\n", "")
        with patch.object(menubar, "prepare", side_effect=AssertionError("already prepared")), \
             patch.object(menubar.subprocess, "run", side_effect=[waiting, self.success, self.running]) as run:
            result = menubar.start(self.data)
        self.assertEqual(run.call_args_list[1].args[0], ["launchctl", "kickstart", prepared["domain"] + "/" + prepared["label"]])
        self.assertEqual(result["pid"], "123")

    def test_stop_only_stops_gui_and_optionally_removes_its_login_link(self):
        prepared = self.prepare()
        menubar._set_login(self.data, True)
        with patch.object(install, "stop", side_effect=AssertionError("must not stop collection")), \
             patch.object(menubar.subprocess, "run", side_effect=[self.running, self.success, self.missing]) as run:
            result = menubar.stop(self.data, login=True)
        self.assertFalse(result["loaded"])
        self.assertFalse(result["login_enabled"])
        self.assertEqual(run.call_args_list[1].args[0], ["launchctl", "bootout", prepared["domain"] + "/" + prepared["label"]])

    def test_login_does_not_overwrite_foreign_files(self):
        self.prepare()
        path = menubar._login_path(self.data)
        path.parent.mkdir(parents=True)
        path.write_text("foreign login item")
        with self.assertRaisesRegex(ValueError, "not owned"):
            menubar._set_login(self.data, True)
        self.assertEqual(path.read_text(), "foreign login item")

    def test_unconfigured_or_unsupported_installation_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "Run deploy"):
            menubar.build(self.data)
        with patch.object(menubar.sys, "platform", "win32"), \
             patch.object(menubar.subprocess, "run", side_effect=AssertionError("must not launch")):
            self.assertFalse(menubar.status(self.data)["supported"])
            with self.assertRaisesRegex(RuntimeError, "macOS"):
                menubar.start(self.data)

    def test_cli_menu_commands_do_not_open_store_or_call_model(self):
        for action in ("build", "install", "start", "stop", "status"):
            target = "prepare" if action == "install" else action
            output = io.StringIO()
            with patch.object(menubar, target, return_value={"action": action}) as operation, \
                 patch.object(cli, "Store", side_effect=AssertionError("must not read DB directly")), \
                 patch.object(cli.AgentRunner, "run", side_effect=AssertionError("must not call model")), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--home", str(self.data), "menubar", action]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"action": action})
            if action == "stop":
                operation.assert_called_once_with(self.data, login=False)
            else:
                operation.assert_called_once_with(self.data)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "native menu checks require macOS and Swift")
class NativeMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.binary = cls.root / "menu"
        source = Path(__file__).resolve().parents[1] / "native/MenuBar.swift"
        result = subprocess.run([shutil.which("swiftc"), "-framework", "Cocoa", "-module-cache-path",
                                 str(cls.root / "cache"), str(source), "-o", str(cls.binary)],
                                capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError(result.stderr)

    def inspect(self, tasks=(), health=None, questions=0, background_enabled=True, last_poll=None):
        path = self.root / "snapshot.json"
        path.write_text(json.dumps({"tasks": list(tasks), "question_count": questions, "pending_events": 12,
            "source_health": health or {}, "generated_at": "2026-11-09T00:00:00+00:00",
            "background": {"supported": True, "loaded": background_enabled}, "last_poll": last_poll}))
        result = subprocess.run([str(self.binary), "--snapshot", str(path), "--dump-menu"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def task(self, index, waiting=False):
        return dict(id=str(index), title="Task " + str(index), status="waiting" if waiting else "active",
                    project="Display", next_action="Review results", waiting_for="Colleague" if waiting else "",
                    due_at="2020-01-01T00:00:00+00:00" if index == 0 else None,
                    waiting_until="2020-01-01T00:00:00+00:00" if waiting else None)

    def test_native_menu_keeps_summary_and_five_tasks_visible_with_remaining_tasks_available(self):
        rows = self.inspect([self.task(i, waiting=i == 8) for i in range(9)], questions=3,
            health={"gerrit": {"status": "unconfigured", "detail": "Configure Gerrit URL", "checked_at": None}})
        titles = [row["title"] for row in rows]
        self.assertEqual(titles[0], "輪到你 8 項  ·  等待 1 項")
        self.assertIn("1 項交付已逾期", titles)
        self.assertIn("待你確認 3 題…", titles)
        self.assertEqual(len([title for title in titles if title.startswith("Task ")]), 5)
        remaining = next(row for row in rows if row["title"] == "其餘 3 項待辦")
        self.assertEqual([row["title"] for row in remaining["items"]], ["Task 5", "Task 6", "Task 7"])
        waiting = next(row for row in rows if row["title"] == "等待他人（1）")
        self.assertEqual(waiting["items"][0]["title"], "Task 8  ·  前置超時")
        source = next(row for row in rows if row["title"] == "1 個來源需處理")
        self.assertEqual(source["items"][0]["title"], "gerrit  ·  尚未連接")
        self.assertEqual(source["items"][0]["items"][0]["title"], "Configure Gerrit URL")

    def test_native_empty_menu_does_not_claim_unchecked_sources_are_healthy(self):
        rows = self.inspect()
        titles = [row["title"] for row in rows]
        self.assertEqual(titles[0], "輪到你 0 項  ·  等待 0 項")
        self.assertIn("目前沒有待辦", titles)
        self.assertIn("來源尚未檢查", titles)
        self.assertNotIn("已檢查來源正常", titles)
        waiting = next(row for row in rows if row["title"] == "等待他人（0）")
        self.assertEqual(waiting["items"][0]["title"], "目前沒有等待事項")

    def test_saved_preview_fixture_matches_the_native_snapshot_contract(self):
        path = Path(__file__).parent / "fixtures/menubar.json"
        result = subprocess.run([str(self.binary), "--snapshot", str(path), "--dump-menu"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        titles = [row["title"] for row in json.loads(result.stdout)]
        self.assertIn("輪到你 6 項  ·  等待 1 項", titles)
        self.assertIn("其餘 1 項待辦", titles)
        self.assertIn("1 個來源需處理", titles)

    def test_native_invalid_snapshot_fails_without_claiming_an_empty_queue(self):
        path = self.root / "invalid.json"
        path.write_text('{"tasks": []}')
        result = subprocess.run([str(self.binary), "--snapshot", str(path), "--dump-menu"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Could not read menu snapshot", result.stderr)

    def test_native_menu_distinguishes_database_reads_from_collection_and_stopped_background(self):
        rows = self.inspect(background_enabled=False, last_poll="2020-01-01T00:00:00+00:00")
        titles = [row["title"] for row in rows]
        self.assertIn("背景收集未啟用", titles)
        collected = next(title for title in titles if title.startswith("最後收集檢查 "))
        self.assertIn("2020", collected)
        self.assertFalse(any(title.startswith("清單更新 ") for title in titles))
        self.assertIn("背景排程已啟用", [row["title"] for row in self.inspect()])

    def test_native_menu_reads_real_cli_snapshot_from_an_isolated_database(self):
        project = Path(__file__).resolve().parents[1]
        home = self.root / "CLI data 中文"
        command = [sys.executable, "-m", "monitorbot", "--home", str(home)]
        added = subprocess.run(command + ["add", "驗證原生選單"], cwd=project,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(0, added.returncode, added.stderr)
        snapshot = subprocess.run(command + ["menubar", "snapshot"], cwd=project,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(0, snapshot.returncode, snapshot.stderr)
        path = self.root / "cli-snapshot.json"
        path.write_text(snapshot.stdout)
        result = subprocess.run([str(self.binary), "--snapshot", str(path), "--dump-menu"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        titles = [row["title"] for row in json.loads(result.stdout)]
        self.assertIn("驗證原生選單", titles)
        self.assertIn("背景收集未啟用", titles)
        self.assertIn("尚未收集資料", titles)


if __name__ == "__main__":
    unittest.main()
