import contextlib
import io
import json
from pathlib import Path
import plistlib
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


if __name__ == "__main__":
    unittest.main()
