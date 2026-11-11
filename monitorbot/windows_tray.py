"""Control the Windows notification icon without changing collection settings."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def _command(data_dir, action, python=None, project_dir=None, snapshot=None, evidence=None):
    root = Path(data_dir).expanduser().resolve()
    project = Path(project_dir or Path(__file__).resolve().parent.parent).resolve()
    script = project / "native/WindowsTray.ps1"
    if not script.is_file():
        raise ValueError("native/WindowsTray.ps1 is missing")
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:24]
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden",
               "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", action,
               "-Key", key, "-DataDir", str(root), "-Python", str(Path(python or sys.executable).resolve()),
               "-Project", str(project)]
    if snapshot is not None:
        command += ["-SnapshotFile", str(Path(snapshot).resolve())]
    if evidence is not None:
        command += ["-Evidence", str(Path(evidence).resolve()), "-Verify"]
    return command


def prepare(data_dir, python=None, project_dir=None):
    if sys.platform != "win32":
        raise RuntimeError("Windows notification icon requires Windows")
    command = _command(data_dir, "Run", python, project_dir)
    Path(data_dir).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    return {"supported": True, "prepared": True, "started": False, "command": command,
            "login_enabled": False}


def _control(data_dir, action, project_dir=None):
    completed = subprocess.run(_command(data_dir, action, project_dir=project_dir),
                               capture_output=True, text=True, encoding="utf-8", timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        raise RuntimeError("Windows notification icon control failed: " + completed.stderr.strip())
    try:
        value = json.loads(completed.stdout)
        if type(value.get("loaded")) is not bool:
            raise ValueError()
        return dict(value, supported=True, prepared=True, login_enabled=False)
    except (ValueError, AttributeError):
        raise RuntimeError("Windows notification icon returned invalid status") from None


def status(data_dir):
    if sys.platform != "win32":
        return {"supported": False, "loaded": False, "login_enabled": False}
    return _control(data_dir, "Status")


def start(data_dir, login=False, python=None, project_dir=None, snapshot=None, evidence=None):
    prepare(data_dir, python, project_dir)
    current = _control(data_dir, "Status", project_dir)
    if current["loaded"]:
        return current
    command = _command(data_dir, "Run", python, project_dir, snapshot, evidence)
    error_path = Path(data_dir).expanduser().resolve() / "tray-errors.log"
    with error_path.open("ab") as errors:
        process = subprocess.Popen(command, stdout=errors, stderr=errors,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            current = _control(data_dir, "Status", project_dir)
            if current["loaded"]:
                return dict(current, pid=process.pid) if process.poll() is None else current
            if process.poll() not in (None, 0):
                raise RuntimeError("Windows notification icon exited before becoming ready; see " + str(error_path))
            time.sleep(0.2)
        raise RuntimeError("Windows notification icon did not become ready within 15 seconds; see " + str(error_path))
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise


def stop(data_dir, login=False):
    if sys.platform != "win32":
        raise RuntimeError("Windows notification icon requires Windows")
    result = _control(data_dir, "Stop")
    deadline = time.monotonic() + 10
    while result["loaded"] and time.monotonic() < deadline:
        time.sleep(0.2)
        result = status(data_dir)
    if result["loaded"]:
        raise RuntimeError("Windows notification icon did not stop")
    return result


def open_todo(data_dir):
    from .config import load_config
    from .desktop import todo_markdown
    from .store import Store
    root = Path(data_dir).expanduser().resolve()
    with Store(root) as store:
        content = todo_markdown(store, load_config(root))
    path = root / "daily-todo.md"
    path.write_text(content, encoding="utf-8")
    os.startfile(str(path))
    return {"path": str(path)}


def add_task(data_dir):
    from .desktop import _windows_dialog
    from .__main__ import main
    text = _windows_dialog("新增待辦", "輸入下一件要做的事。")
    if text.strip():
        return {"saved": main(["--home", str(data_dir), "add", "--", text]) == 0}
    return {"saved": False}
