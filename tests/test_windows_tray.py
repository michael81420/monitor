import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from monitorbot import install, menubar, windows_tray
from monitorbot.store import Store


class WindowsTrayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "中文 data"

    def test_command_preserves_paths_and_is_scoped_to_installation(self):
        first = windows_tray._command(self.root, "Run")
        second = windows_tray._command(self.root.parent / "other", "Run")
        self.assertEqual(str(self.root.resolve()), first[first.index("-DataDir") + 1])
        self.assertNotEqual(first[first.index("-Key") + 1], second[second.index("-Key") + 1])
        self.assertIn("-File", first)
        self.assertNotIn("-Command", first)

    def test_windows_menu_controls_route_to_notification_icon(self):
        for name in ("build", "prepare", "start", "stop", "status"):
            target = "prepare" if name == "build" else name
            with patch.object(menubar.sys, "platform", "win32"), \
                 patch.object(windows_tray, target, return_value={"loaded": True}) as operation:
                self.assertTrue(getattr(menubar, name)(self.root)["loaded"])
                operation.assert_called_once()

    def test_stop_icon_does_not_stop_collection(self):
        with patch.object(windows_tray.sys, "platform", "win32"), \
             patch.object(windows_tray, "_control", return_value={"loaded": False}) as control, \
             patch.object(install, "stop", side_effect=AssertionError("must keep collection running")):
            self.assertFalse(windows_tray.stop(self.root)["loaded"])
        control.assert_called_once_with(self.root, "Stop")

    def test_status_rejects_malformed_output(self):
        with patch.object(windows_tray.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"loaded":"false"}', "")):
            with self.assertRaisesRegex(RuntimeError, "invalid status"):
                windows_tray._control(self.root, "Status")

    def test_prepare_does_not_register_login_or_start_process(self):
        with patch.object(windows_tray.sys, "platform", "win32"), \
             patch.object(windows_tray.subprocess, "Popen", side_effect=AssertionError("must not launch")):
            result = windows_tray.prepare(self.root)
        self.assertTrue(result["prepared"])
        self.assertFalse(result["started"])
        self.assertFalse(result["login_enabled"])

    def test_running_icon_start_is_idempotent(self):
        with patch.object(windows_tray.sys, "platform", "win32"), \
             patch.object(windows_tray, "_control", return_value={"loaded": True}), \
             patch.object(windows_tray.subprocess, "Popen", side_effect=AssertionError("duplicate launch")):
            self.assertTrue(windows_tray.start(self.root)["loaded"])

    def test_typed_title_starting_with_hyphen_is_saved(self):
        from monitorbot import desktop
        with patch.object(desktop, "_windows_dialog", return_value="-x 中文待辦"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(windows_tray.add_task(self.root)["saved"])
        with Store(self.root) as store:
            self.assertEqual(["-x 中文待辦"], [task["title"] for task in store.list_tasks()])

    def test_readiness_failure_terminates_only_newly_spawned_host(self):
        with patch.object(windows_tray.sys, "platform", "win32"), \
             patch.object(windows_tray, "_control", side_effect=[{"loaded": False}, RuntimeError("readiness unavailable")]), \
             patch.object(windows_tray.subprocess, "Popen") as launch:
            launch.return_value.poll.return_value = None
            with self.assertRaisesRegex(RuntimeError, "readiness unavailable"):
                windows_tray.start(self.root)
            launch.return_value.terminate.assert_called_once()
            launch.return_value.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
