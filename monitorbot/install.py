"""Prepare and control this installation's macOS LaunchAgent."""

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


RUNTIME_ITEMS = ("monitorbot", "tests", "skills", "docs", "scripts", "native", "README.md", "CONTEXT.md")


def _installation_root(workspace):
    identity = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
    return Path.home() / "Library/Application Support/MonitorBot" / identity


def runtime_manifest(data_dir):
    root = Path(data_dir).expanduser().resolve()
    path = root / "runtime.json"
    if path.is_symlink():
        raise ValueError("runtime manifest must not be a symlink")
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("application") != "monitorbot"
            or not isinstance(manifest.get("workspace"), str)):
        raise ValueError("runtime manifest is not owned by MonitorBot")
    workspace = Path(manifest["workspace"]).expanduser().resolve()
    destination = _installation_root(workspace)
    if destination.is_symlink():
        raise ValueError("installation target must not be a symlink")
    base = destination.resolve()
    expected = {"workspace": workspace, "installation_root": base,
                "project_dir": base / "app", "data_dir": base / "data"}
    if root != base / "data" or any(manifest.get(key) != str(value) for key, value in expected.items()):
        raise ValueError("runtime manifest has inconsistent installation paths")
    if (base / "app").is_symlink():
        raise ValueError("installed runtime must not be a symlink")
    if workspace == base or base in workspace.parents:
        raise ValueError("installed runtime cannot be used as its own workspace")
    return manifest


def bootstrap_runtime(arguments=None, project_dir=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    project = Path(project_dir or Path(__file__).resolve().parent.parent).resolve()
    home = os.environ.get("MONITOR_HOME") or str(project / ".monitor")
    remaining = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--home":
            if index + 1 >= len(arguments):
                return False
            home = arguments[index + 1]
            index += 2
        elif argument.startswith("--home="):
            home = argument.split("=", 1)[1]
            index += 1
        else:
            remaining.extend(arguments[index:])
            break
    manifest = runtime_manifest(home)
    if manifest is None or project == Path(manifest["project_dir"]):
        return False
    installed = Path(manifest["project_dir"])
    if not (installed / "monitorbot/__main__.py").is_file():
        raise ValueError("installed MonitorBot runtime is missing")
    os.chdir(installed)
    os.execv(sys.executable, [sys.executable, "-m", "monitorbot", "--home", manifest["data_dir"]] + remaining)
    return True


def _private_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".install-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def deploy(data_dir, python=None, project_dir=None):
    """Move this installation off protected Desktop paths, without enabling it."""
    _require_mac()
    source = Path(data_dir).expanduser().resolve()
    previous = runtime_manifest(source)
    workspace = Path(previous["workspace"] if previous else
                     project_dir or Path(__file__).resolve().parent.parent).resolve()
    destination = _installation_root(workspace)
    if destination.is_symlink():
        raise ValueError("installation target must not be a symlink")
    base = destination.resolve()
    app, data = base / "app", base / "data"
    link = workspace / ".monitor"
    if not (workspace / "monitorbot/__main__.py").is_file():
        raise ValueError("workspace does not contain MonitorBot")
    if workspace == source or source in workspace.parents or base == workspace or base in workspace.parents:
        raise ValueError("deployment paths overlap the workspace")
    if not previous and runtime_manifest(workspace / ".monitor") is not None:
        raise ValueError("deploy from an installed runtime requires its canonical data directory")
    if app.is_symlink() or data.is_symlink():
        raise ValueError("installation targets must not be symlinks")
    if base.exists() and any(base.iterdir()) and previous is None:
        raise ValueError("installation target already exists and is not owned by this deployment")
    if data.exists() and source != data:
        raise ValueError("installation target contains different data")
    if (link.exists() or link.is_symlink()) and link.resolve() != source:
        raise ValueError("workspace contains different data")
    executable = Path(python or sys.executable).resolve()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    config_path = source / "config.json"
    if config_path.is_symlink() or (source / "launchagent.plist").is_symlink():
        raise ValueError("configuration and service definition must not be symlinks")
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
    if config is not None and (not isinstance(config, dict) or not isinstance(config.get("microsoft", {}), dict)):
        raise ValueError("configuration must contain objects")
    update_token = bool(config and config.get("microsoft", {}).get("token_file") and
                        Path(config["microsoft"]["token_file"]).expanduser().resolve() == source / "microsoft-auth.json")
    old_plist = (source / "launchagent.plist").read_bytes() if (source / "launchagent.plist").is_file() else None
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".app-stage-", dir=str(base)))
    moved, swapped, updated_config, backup = False, False, False, None
    old_link = os.readlink(link) if link.is_symlink() else None
    try:
        for name in RUNTIME_ITEMS:
            item = workspace / name
            if item.is_symlink() or not item.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, stage / name, ignore=lambda directory, names: [
                    entry for entry in names if entry.startswith(".") or entry == "__pycache__"
                    or entry in ("MonitorMenuBar", "Monitor.app")
                    or (Path(directory) / entry).is_symlink()])
            else:
                shutil.copy2(item, stage / name)
        if not (stage / "monitorbot/__main__.py").is_file():
            raise ValueError("runtime copy is incomplete")
        stop(source, login=True)
        from .service import cycle_lock
        with cycle_lock(source):
            if source != data:
                source.rename(data)
                moved = True
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                link.symlink_to(data, target_is_directory=True)
            (stage / ".monitor").symlink_to(data, target_is_directory=True)
            if app.exists():
                backups = base / "install-backup"
                backups.mkdir(exist_ok=True, mode=0o700)
                backup = backups / uuid.uuid4().hex
                app.rename(backup)
            stage.rename(app)
            swapped = True
            if update_token:
                updated = dict(config)
                updated["microsoft"] = dict(config["microsoft"], token_file=str(data / "microsoft-auth.json"))
                _private_json(data / "config.json", updated)
                updated_config = True
            result = prepare(data, python=executable, project_dir=app)
            manifest = {"application": "monitorbot", "workspace": str(workspace),
                        "project_dir": str(app), "installation_root": str(base), "data_dir": str(data)}
            _private_json(data / "runtime.json", manifest)
            return dict(result, deployed=True, **manifest, backup=str(backup) if backup else None)
    except BaseException:
        if swapped:
            app.rename(stage)
        if backup is not None and backup.exists():
            backup.rename(app)
        if moved:
            if link.is_symlink() and link.resolve() == data:
                link.unlink()
            data.rename(source)
            if old_link is not None:
                link.symlink_to(old_link, target_is_directory=True)
        elif old_link is None and link.is_symlink() and link.resolve() == data:
            link.unlink()
        if updated_config and config_path.parent.exists():
            _private_json(config_path, config)
        if swapped and old_plist is not None:
            (source / "launchagent.plist").write_bytes(old_plist)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _identity(data_dir):
    root = Path(data_dir).expanduser().resolve()
    suffix = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    label = "local.monitorbot." + suffix
    return root, label, "gui/" + str(os.getuid()) if hasattr(os, "getuid") else ""


def prepare(data_dir, python=None, project_dir=None):
    root, label, domain = _identity(data_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    project = Path(project_dir or Path(__file__).resolve().parent.parent).resolve()
    executable = Path(python or sys.executable).resolve()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    directories = [str(executable.parent), str(Path.home() / ".local/bin"),
                   "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    for name in ("codex", "claude"):
        found = shutil.which(name)
        if found:
            directories.insert(0, str(Path(found).parent))
    definition = {
        "Label": label,
        "ProgramArguments": [str(executable), "-m", "monitorbot", "--home", str(root), "tick"],
        "WorkingDirectory": str(project),
        "StartInterval": 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(directories))},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(root / "service-errors.log"),
    }
    path = root / "launchagent.plist"
    temporary = path.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(definition))
    temporary.chmod(0o600)
    temporary.replace(path)
    return {"path": str(path), "label": label, "domain": domain, "prepared": True,
            "started": False, "note": "Run start to enable background execution."}


def _require_mac():
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent control requires macOS")


def status(data_dir):
    root, label, domain = _identity(data_dir)
    result = {"path": str(root / "launchagent.plist"), "label": label,
              "prepared": (root / "launchagent.plist").is_file(), "loaded": False,
              "supported": sys.platform == "darwin",
              "login_enabled": _login_path(root).is_symlink() and
              _login_path(root).resolve() == (root / "launchagent.plist").resolve()}
    if not result["supported"]:
        return result
    process = subprocess.run(["launchctl", "print", domain + "/" + label],
                             capture_output=True, text=True)
    result["loaded"] = process.returncode == 0
    if result["loaded"]:
        for field in ("state", "last exit code", "pid"):
            found = re.search(r"^\s*" + re.escape(field) + r"\s*=\s*(.+)$", process.stdout, re.MULTILINE)
            if found:
                result[field.replace(" ", "_")] = found.group(1).strip()
    return result


def _login_path(data_dir):
    _, label, _ = _identity(data_dir)
    return Path.home() / "Library" / "LaunchAgents" / (label + ".plist")


def _set_login(data_dir, enabled):
    root, _, _ = _identity(data_dir)
    target = root / "launchagent.plist"
    link = _login_path(root)
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise ValueError("login item already exists and is not owned by this installation")
        if not enabled:
            link.unlink()
    elif enabled:
        if not target.is_file():
            raise ValueError("Run install before enabling login startup")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)


def start(data_dir, login=False):
    _require_mac()
    root, label, domain = _identity(data_dir)
    path = root / "launchagent.plist"
    if not path.is_file():
        raise ValueError("Run install before start")
    from . import menubar
    menu = menubar.start(root, login=login)
    if login:
        _set_login(root, True)
    current = status(root)
    if current["loaded"]:
        return dict(current, menubar=menu)
    process = subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                             capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError("launchctl bootstrap failed with exit code " + str(process.returncode))
    return dict(status(root), menubar=menu)


def stop(data_dir, login=False):
    _require_mac()
    root, label, domain = _identity(data_dir)
    from . import menubar
    menu = menubar.stop(root, login=login)
    if login:
        _set_login(root, False)
    current = status(root)
    if not current["loaded"]:
        return dict(current, menubar=menu)
    process = subprocess.run(["launchctl", "bootout", domain + "/" + label],
                             capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError("launchctl bootout failed with exit code " + str(process.returncode))
    return dict(status(root), menubar=menu)
