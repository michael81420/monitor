import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from monitorbot import install
from monitorbot import menubar


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        start = patch.object(menubar, "start", return_value={"loaded": True})
        stop = patch.object(menubar, "stop", return_value={"loaded": False})
        self.menu_start, self.menu_stop = start.start(), stop.start()
        self.addCleanup(start.stop)
        self.addCleanup(stop.stop)

    def tearDown(self):
        self.directory.cleanup()

    def test_prepare_writes_only_local_plist_without_loading_service(self):
        with patch.object(install.subprocess, "run", side_effect=AssertionError("must not start service")):
            result = install.prepare(self.root)
        path = Path(result["path"])
        self.assertEqual(path.parent, self.root.resolve())
        definition = plistlib.loads(path.read_bytes())
        self.assertTrue(Path(definition["ProgramArguments"][0]).is_absolute())
        self.assertEqual(definition["ProgramArguments"][-1], "tick")
        self.assertEqual(definition["StartInterval"], 60)
        self.assertEqual(definition["StandardOutPath"], "/dev/null")
        self.assertEqual(Path(definition["StandardErrorPath"]), self.root.resolve() / "service-errors.log")
        self.assertEqual(set(definition["EnvironmentVariables"]), {"PATH"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result["started"])

    def test_different_data_directories_have_different_labels(self):
        one = install.prepare(self.root / "one")
        two = install.prepare(self.root / "two")
        self.assertNotEqual(one["label"], two["label"])

    def test_start_bootstraps_prepared_plist_and_is_idempotent(self):
        prepared = install.prepare(self.root)
        missing = subprocess.CompletedProcess([], 113, "", "not found")
        success = subprocess.CompletedProcess([], 0, "", "")
        loaded = subprocess.CompletedProcess([], 0, "state = waiting\nlast exit code = 0\n", "")
        with patch.object(install.sys, "platform", "darwin"), \
             patch.object(install.subprocess, "run", side_effect=[missing, success, loaded]) as run:
            result = install.start(self.root)
        self.assertTrue(result["loaded"])
        self.assertEqual(run.call_args_list[1].args[0], ["launchctl", "bootstrap", prepared["domain"], prepared["path"]])
        with patch.object(install.sys, "platform", "darwin"), \
             patch.object(install.subprocess, "run", return_value=loaded) as run:
            self.assertTrue(install.start(self.root)["loaded"])
        self.assertEqual(run.call_count, 1)
        self.menu_start.assert_called_with(self.root.resolve(), login=False)

    def test_stop_only_unloads_this_label(self):
        prepared = install.prepare(self.root)
        loaded = subprocess.CompletedProcess([], 0, "state = waiting", "")
        success = subprocess.CompletedProcess([], 0, "", "")
        missing = subprocess.CompletedProcess([], 113, "", "not found")
        with patch.object(install.sys, "platform", "darwin"), \
             patch.object(install.subprocess, "run", side_effect=[loaded, success, missing]) as run:
            result = install.stop(self.root)
        self.assertFalse(result["loaded"])
        self.assertEqual(run.call_args_list[1].args[0], ["launchctl", "bootout", prepared["domain"] + "/" + prepared["label"]])
        self.menu_stop.assert_called_once_with(self.root.resolve(), login=False)

    def test_unsupported_platform_does_not_invoke_launchctl(self):
        with patch.object(install.sys, "platform", "win32"), \
             patch.object(install.subprocess, "run", side_effect=AssertionError("must not invoke launchctl")):
            self.assertFalse(install.status(self.root)["supported"])
            with self.assertRaisesRegex(RuntimeError, "macOS"):
                install.start(self.root)

    def test_login_startup_is_explicit_and_does_not_overwrite_other_files(self):
        install.prepare(self.root)
        with patch.object(install.Path, "home", return_value=self.root / "user"):
            install._set_login(self.root, True)
            path = install._login_path(self.root)
            self.assertTrue(path.is_symlink())
            self.assertEqual((self.root / "launchagent.plist").resolve(), path.resolve())
            install._set_login(self.root, True)
            install._set_login(self.root, False)
            self.assertFalse(path.exists())
            path.write_text("belongs to another program")
            with self.assertRaisesRegex(ValueError, "not owned"):
                install._set_login(self.root, True)
            self.assertEqual("belongs to another program", path.read_text())

    def test_login_stop_removes_startup_even_if_job_is_not_loaded(self):
        install.prepare(self.root)
        missing = subprocess.CompletedProcess([], 113, "", "not found")
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), \
             patch.object(install.subprocess, "run", return_value=missing):
            install._set_login(self.root, True)
            result = install.stop(self.root, login=True)
            self.assertFalse(result["login_enabled"])

    def workspace(self, token_file=None):
        workspace = self.root / "workspace"
        (workspace / "monitorbot").mkdir(parents=True)
        (workspace / "monitorbot/__main__.py").write_text("# synthetic runtime\n")
        (workspace / "README.md").write_text("MonitorBot")
        (workspace / ".git").mkdir()
        (workspace / ".git/private").write_text("not runtime")
        data = workspace / ".monitor"
        data.mkdir()
        (data / "config.json").write_text(json.dumps({"microsoft": {
            "token_file": token_file or str(data / "microsoft-auth.json")}}))
        (data / "microsoft-auth.json").write_text("synthetic secret")
        (data / "monitor.sqlite3").write_bytes(b"synthetic database")
        (workspace / "monitorbot/external.py").symlink_to(data / "microsoft-auth.json")
        (workspace / "native").mkdir()
        (workspace / "native/MenuBar.swift").write_text("// synthetic native source\n")
        (workspace / "native/MonitorMenuBar").write_bytes(b"old compiled binary")
        (workspace / "native/Monitor.app").mkdir()
        return workspace, data

    def test_deploy_moves_data_and_prepares_installed_runtime_without_starting(self):
        workspace, data = self.workspace()
        old_home = str(data)
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), \
             patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 113, "", "")) as run:
            install.prepare(data, project_dir=workspace)
            install._set_login(data, True)
            old_login = install._login_path(data)
            result = install.deploy(data, project_dir=workspace)
        installed, canonical = Path(result["project_dir"]), Path(result["data_dir"])
        self.assertTrue(data.is_symlink())
        self.assertEqual(data.resolve(), canonical)
        self.assertEqual((installed / ".monitor").resolve(), canonical)
        self.assertEqual((canonical / "monitor.sqlite3").read_bytes(), b"synthetic database")
        self.assertEqual((canonical / "microsoft-auth.json").read_text(), "synthetic secret")
        self.assertFalse((installed / ".git").exists())
        self.assertFalse((installed / "microsoft-auth.json").exists())
        self.assertFalse((installed / "monitorbot/external.py").exists())
        self.assertTrue((installed / "native/MenuBar.swift").is_file())
        self.assertFalse((installed / "native/MonitorMenuBar").exists())
        self.assertFalse((installed / "native/Monitor.app").exists())
        self.assertFalse(old_login.exists())
        self.assertTrue((installed / "monitorbot/__main__.py").is_file())
        config = json.loads((canonical / "config.json").read_text())
        self.assertEqual(config["microsoft"]["token_file"], str(canonical / "microsoft-auth.json"))
        self.assertNotEqual(str(canonical), old_home)
        manifest = json.loads((canonical / "runtime.json").read_text())
        self.assertEqual(manifest["workspace"], str(workspace.resolve()))
        self.assertEqual((canonical / "runtime.json").stat().st_mode & 0o777, 0o600)
        definition = plistlib.loads((canonical / "launchagent.plist").read_bytes())
        self.assertEqual(definition["WorkingDirectory"], str(installed))
        self.assertEqual(definition["ProgramArguments"][-3:], ["--home", str(canonical), "tick"])
        self.assertFalse(result["started"])
        self.assertTrue(all(call.args[0][1] == "print" for call in run.call_args_list))

    def test_deploy_stops_old_loaded_label_before_moving_data(self):
        workspace, data = self.workspace()
        loaded = subprocess.CompletedProcess([], 0, "state = waiting", "")
        missing = subprocess.CompletedProcess([], 113, "", "")
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), \
             patch.object(install.subprocess, "run", side_effect=[loaded, subprocess.CompletedProcess([], 0, "", ""), missing]) as run:
            old_label = install._identity(data)[1]
            install.deploy(data, project_dir=workspace)
        self.assertEqual(run.call_args_list[1].args[0][-1].split("/")[-1], old_label)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["print", "bootout", "print"])

    def test_redeploy_uses_original_workspace_and_preserves_previous_app(self):
        workspace, data = self.workspace(token_file="/custom/auth.json")
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop", return_value={}):
            initial = install.deploy(data, project_dir=workspace)
            app = Path(initial["project_dir"])
            (workspace / "README.md").write_text("updated original workspace")
            second = install.deploy(data, project_dir=app)
            manifest = install.runtime_manifest(data)
        self.assertEqual(second["installation_root"], initial["installation_root"])
        self.assertEqual(manifest["workspace"], str(workspace.resolve()))
        self.assertEqual((app / "README.md").read_text(), "updated original workspace")
        self.assertEqual((Path(second["backup"]) / "README.md").read_text(), "MonitorBot")
        self.assertEqual(json.loads((data / "config.json").read_text())["microsoft"]["token_file"], "/custom/auth.json")

    def test_deploy_refuses_foreign_target_before_stopping_or_moving(self):
        workspace, data = self.workspace()
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop") as stop:
            base = install._installation_root(workspace.resolve())
            base.mkdir(parents=True)
            (base / "foreign").write_text("keep")
            with self.assertRaisesRegex(ValueError, "not owned"):
                install.deploy(data, project_dir=workspace)
            stop.assert_not_called()
        self.assertFalse(data.is_symlink())
        self.assertEqual((base / "foreign").read_text(), "keep")

    def test_failed_deployment_restores_original_data_and_runtime(self):
        workspace, data = self.workspace()
        config = (data / "config.json").read_text()
        install.prepare(data, project_dir=workspace)
        original_plist = (data / "launchagent.plist").read_bytes()
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), \
             patch.object(install, "stop", return_value={}), \
             patch.object(install, "prepare", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(OSError, "synthetic failure"):
                install.deploy(data, project_dir=workspace)
        self.assertTrue(data.is_dir())
        self.assertFalse(data.is_symlink())
        self.assertEqual(json.loads((data / "config.json").read_text()), json.loads(config))
        self.assertEqual((data / "monitor.sqlite3").read_bytes(), b"synthetic database")
        self.assertEqual((data / "launchagent.plist").read_bytes(), original_plist)

    def test_failed_redeployment_restores_previous_app_and_single_data_link(self):
        workspace, data = self.workspace()
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop", return_value={}):
            result = install.deploy(data, project_dir=workspace)
            (workspace / "README.md").write_text("new runtime")
            with patch.object(install, "prepare", side_effect=OSError("synthetic failure")):
                with self.assertRaisesRegex(OSError, "synthetic failure"):
                    install.deploy(data, project_dir=Path(result["project_dir"]))
            self.assertEqual(install.runtime_manifest(data)["data_dir"], result["data_dir"])
        self.assertEqual((Path(result["project_dir"]) / "README.md").read_text(), "MonitorBot")
        self.assertEqual(data.resolve(), Path(result["data_dir"]))

    def test_deploy_refuses_to_replace_a_different_workspace_database(self):
        workspace, data = self.workspace()
        other = self.root / "other-data"
        other.mkdir()
        (other / "monitor.sqlite3").write_bytes(b"different database")
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop") as stop:
            with self.assertRaisesRegex(ValueError, "different data"):
                install.deploy(other, project_dir=workspace)
            stop.assert_not_called()
        self.assertEqual((other / "monitor.sqlite3").read_bytes(), b"different database")
        self.assertEqual((data / "monitor.sqlite3").read_bytes(), b"synthetic database")

    def test_deploy_from_installed_app_does_not_create_nested_installation(self):
        workspace, data = self.workspace()
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop", return_value={}):
            result = install.deploy(data, project_dir=workspace)
            with self.assertRaisesRegex(ValueError, "canonical data"):
                install.deploy(self.root / "other", project_dir=result["project_dir"])

    def test_bootstrap_redirects_before_workspace_modules_are_loaded(self):
        workspace, data = self.workspace()
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop", return_value={}):
            result = install.deploy(data, project_dir=workspace)
            with patch.object(install.os, "chdir") as chdir, patch.object(install.os, "execv") as execute:
                self.assertTrue(install.bootstrap_runtime(["--home=ignored", "--home", str(data), "add", "a thought"], workspace))
        chdir.assert_called_once_with(Path(result["project_dir"]))
        self.assertEqual(execute.call_args.args[1], [install.sys.executable, "-m", "monitorbot", "--home", result["data_dir"], "add", "a thought"])

    def test_explicit_temporary_home_without_manifest_does_not_redirect(self):
        workspace, data = self.workspace()
        with patch.object(install.Path, "home", return_value=self.root / "user"), \
             patch.object(install.sys, "platform", "darwin"), patch.object(install, "stop", return_value={}):
            result = install.deploy(data, project_dir=workspace)
            with patch.object(install.os, "execv") as execute:
                self.assertFalse(install.bootstrap_runtime(["--home", str(self.root / "uninstalled"), "init"], workspace))
                self.assertFalse(install.bootstrap_runtime(["--home", result["data_dir"], "init"], result["project_dir"]))
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
