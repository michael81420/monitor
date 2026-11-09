import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from monitorbot import desktop, install, windows


class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "work space 中文"

    def test_install_only_prepares_task_for_current_user_without_registering(self):
        with patch.object(install.sys, "platform", "win32"), \
             patch.object(windows.subprocess, "run", side_effect=AssertionError("must not register")):
            result = install.prepare(self.root)
            install.prepare(self.root)
        task = ET.parse(result["path"])
        def value(name):
            return task.find(".//{" + windows.TASK_NS + "}" + name).text
        self.assertEqual("InteractiveToken", value("LogonType"))
        self.assertEqual("LeastPrivilege", value("RunLevel"))
        self.assertEqual("IgnoreNew", value("MultipleInstancesPolicy"))
        self.assertEqual("PT0S", value("ExecutionTimeLimit"))
        restart = task.find(".//{" + windows.TASK_NS + "}RestartOnFailure")
        self.assertEqual("PT1M", restart.find("{" + windows.TASK_NS + "}Interval").text)
        self.assertEqual("3", restart.find("{" + windows.TASK_NS + "}Count").text)
        self.assertIn('"' + str(self.root.resolve()) + '"', value("Arguments"))
        self.assertTrue(value("Arguments").endswith("run --loop"))
        self.assertFalse(result["started"])
        self.assertNotEqual(result["label"], windows.prepare(self.root / "second")["label"])

    def test_windows_controls_route_to_scheduler_and_send_paths_as_json(self):
        windows.prepare(self.root)
        result = dict(registered=True, loaded=True, login_enabled=True, state=4, last_exit_code=0)
        completed = subprocess.CompletedProcess([], 0, json.dumps(result), "")
        with patch.object(install.sys, "platform", "win32"), \
             patch.object(windows.subprocess, "run", return_value=completed) as run:
            self.assertTrue(install.start(self.root, login=True)["loaded"])
            request = json.loads(run.call_args.kwargs["input"])
            self.assertEqual("start", request["action"])
            self.assertEqual(str(self.root.resolve() / "windows-task.xml"), request["path"])
            self.assertTrue(request["login"])
            self.assertEqual("powershell.exe", run.call_args.args[0][0])
            install.stop(self.root, login=True)
            self.assertEqual("stop", json.loads(run.call_args.kwargs["input"])["action"])
            self.assertTrue(install.status(self.root)["supported"])

    def test_missing_definition_and_scheduler_errors_are_not_success(self):
        with patch.object(windows.sys, "platform", "win32"), \
             patch.object(windows.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "install"):
                windows.control(self.root, "start")
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 1, "", "private error body")
            with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                windows.control(self.root, "status")
            run.return_value = subprocess.CompletedProcess([], 0, '{"loaded":"false"}', "")
            with self.assertRaisesRegex(RuntimeError, "invalid status"):
                windows.control(self.root, "status")

    def test_daily_dialogs_use_windows_adapter(self):
        with patch.object(desktop.sys, "platform", "win32"), \
             patch.object(desktop, "_windows_dialog", return_value="selected") as dialog, \
             patch.object(desktop, "_osascript", side_effect=AssertionError("mac only")):
            self.assertEqual("selected", desktop.choose("Title", "Prompt", ["selected"]))
            dialog.assert_called_with("Title", "Prompt", ["selected"])
            self.assertEqual("selected", desktop._text("Title", "Prompt"))
            dialog.assert_called_with("Title", "Prompt")
