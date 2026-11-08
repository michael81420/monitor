"""Build and control this installation's native macOS menu bar process."""

import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from . import install


def _identity(data_dir):
    root, label, domain = install._identity(data_dir)
    return root, label + ".menubar", domain


def _project(data_dir, project_dir=None):
    manifest = install.runtime_manifest(data_dir)
    if manifest is not None:
        return Path(manifest["project_dir"])
    if project_dir is None:
        raise ValueError("Run deploy before installing the menu bar icon")
    return Path(project_dir).expanduser().resolve()


def build(data_dir, project_dir=None):
    install._require_mac()
    project = _project(data_dir, project_dir)
    directory = project / "native"
    source = directory / "MenuBar.swift"
    app = directory / "Monitor.app"
    binary = app / "Contents/MacOS/MonitorMenuBar"
    bundle_id = _identity(data_dir)[1]
    if any(path.is_symlink() for path in (directory, source, app, app / "Contents", binary.parent, binary)):
        raise ValueError("menu bar build paths must not be symlinks")
    if not source.is_file():
        raise ValueError("native/MenuBar.swift is missing; deploy the current source first")
    compiler = shutil.which("swiftc")
    if compiler is None:
        raise RuntimeError("Swift compiler is unavailable; install Xcode Command Line Tools before starting the menu bar icon")
    if app.exists():
        try:
            previous = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        except (OSError, ValueError):
            raise ValueError("existing app bundle is not owned by this installation") from None
        if previous.get("CFBundleIdentifier") != bundle_id:
            raise ValueError("existing app bundle is not owned by this installation")
    with tempfile.TemporaryDirectory(prefix=".menubar-build-", dir=str(directory)) as temporary:
        staged = Path(temporary) / "Monitor.app"
        output = staged / "Contents/MacOS/MonitorMenuBar"
        output.parent.mkdir(parents=True)
        cache = Path(temporary) / "ModuleCache"
        try:
            result = subprocess.run([compiler, "-O", "-framework", "Cocoa", "-module-cache-path", str(cache),
                                     str(source), "-o", str(output)],
                                    cwd=str(project), capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise RuntimeError("menu bar compilation timed out") from None
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("menu bar compilation failed with exit code " + str(result.returncode))
        output.chmod(0o700)
        (staged / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleExecutable": "MonitorMenuBar",
            "CFBundleName": "Monitor", "CFBundleDisplayName": "Monitor",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSUIElement": True,
            "NSHighResolutionCapable": True,
        }))
        backup = Path(temporary) / "previous.app"
        if app.exists():
            app.rename(backup)
        try:
            staged.rename(app)
        except OSError:
            if backup.exists():
                backup.rename(app)
            raise
    return {"binary": str(binary), "app": str(app), "bundle_id": bundle_id,
            "project_dir": str(project), "built": True}


def prepare(data_dir, python=None, project_dir=None):
    root, label, domain = _identity(data_dir)
    executable = Path(python or sys.executable).resolve()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    built = build(root, project_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    definition = {
        "Label": label,
        "ProgramArguments": [built["binary"], "--python", str(executable),
                             "--project", built["project_dir"], "--home", str(root)],
        "WorkingDirectory": built["project_dir"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(root / "menubar-errors.log"),
    }
    path = root / "menubar-launchagent.plist"
    descriptor, temporary = tempfile.mkstemp(prefix=".menubar-plist-", dir=str(root))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(definition, handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return dict(built, path=str(path), label=label, domain=domain, prepared=True, started=False)


def _login_path(data_dir):
    _, label, _ = _identity(data_dir)
    return Path.home() / "Library/LaunchAgents" / (label + ".plist")


def _set_login(data_dir, enabled):
    root, _, _ = _identity(data_dir)
    target = root / "menubar-launchagent.plist"
    link = _login_path(root)
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise ValueError("menu bar login item is not owned by this installation")
        if not enabled:
            link.unlink()
    elif enabled:
        if not target.is_file():
            raise ValueError("menu bar LaunchAgent is not prepared")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)


def status(data_dir):
    root, label, domain = _identity(data_dir)
    path, link = root / "menubar-launchagent.plist", _login_path(root)
    result = {"path": str(path), "label": label, "prepared": path.is_file(),
              "loaded": False, "supported": sys.platform == "darwin",
              "login_enabled": link.is_symlink() and link.resolve() == path.resolve()}
    if not result["supported"]:
        return result
    process = subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True, text=True)
    result["loaded"] = process.returncode == 0
    if result["loaded"]:
        for field in ("state", "last exit code", "pid"):
            found = re.search(r"^\s*" + re.escape(field) + r"\s*=\s*(.+)$", process.stdout, re.MULTILINE)
            if found:
                result[field.replace(" ", "_")] = found.group(1).strip()
    return result


def start(data_dir, login=True, python=None, project_dir=None):
    install._require_mac()
    root, label, domain = _identity(data_dir)
    current = status(root)
    if current["loaded"]:
        if login:
            _set_login(root, True)
            current["login_enabled"] = True
        if current.get("state") != "running" and not current.get("pid"):
            process = subprocess.run(["launchctl", "kickstart", domain + "/" + label], capture_output=True, text=True)
            if process.returncode:
                raise RuntimeError("menu bar launchctl kickstart failed with exit code " + str(process.returncode))
            return status(root)
        return current
    prepared = prepare(root, python=python, project_dir=project_dir)
    if login:
        _set_login(root, True)
    process = subprocess.run(["launchctl", "bootstrap", domain, prepared["path"]], capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError("menu bar launchctl bootstrap failed with exit code " + str(process.returncode))
    return status(root)


def stop(data_dir, login=False):
    install._require_mac()
    root, label, domain = _identity(data_dir)
    if login:
        _set_login(root, False)
    current = status(root)
    if not current["loaded"]:
        return current
    process = subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError("menu bar launchctl bootout failed with exit code " + str(process.returncode))
    return status(root)
