"""Windows Task Scheduler adapter. Tasks run only in the current user's session."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import os
from xml.etree import ElementTree as ET


TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
CONTROL = r'''
$ErrorActionPreference = 'Stop'
try {
    $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    $task = $null
    try { $task = $folder.GetTask($r.name) }
    catch {
        if ($_.Exception.GetBaseException().HResult -ne -2147024894) { throw }
    }
    if ($null -ne $task -and $task.Definition.RegistrationInfo.Description -ne $r.owner) {
        throw 'Task is not owned by this installation'
    }
    $login = $false
    if ($null -ne $task) {
        foreach ($trigger in $task.Definition.Triggers) {
            if ($trigger.Type -eq 9 -and $trigger.Enabled) { $login = $true }
        }
    }
    if ($r.action -eq 'start') {
        $definition = $service.NewTask(0)
        $definition.XmlText = [IO.File]::ReadAllText($r.path)
        $changed = $null -eq $task
        if ($null -ne $task) {
            $old = $task.Definition.Actions.Item(1)
            $new = $definition.Actions.Item(1)
            $changed = ($old.Path -ne $new.Path -or $old.Arguments -ne $new.Arguments -or $old.WorkingDirectory -ne $new.WorkingDirectory)
        }
        if ($changed -or ($r.login -and -not $login)) {
            $definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $definition.Principal.LogonType = 3
            if ($r.login -or $login) {
                $trigger = $definition.Triggers.Create(9)
                $trigger.UserId = $definition.Principal.UserId
            }
            $task = $folder.RegisterTaskDefinition($r.name, $definition, 6, $definition.Principal.UserId, $null, 3, $null)
        }
        if ($task.State -ne 4) { $null = $task.Run($null) }
    }
    if ($r.action -eq 'stop' -and $null -ne $task) {
        if ($task.State -eq 4 -or $task.State -eq 2) { $task.Stop(0) }
        if ($r.login) {
            $folder.DeleteTask($r.name, 0)
            $task = $null
        }
    }
    $login = $false
    if ($null -ne $task) {
        foreach ($trigger in $task.Definition.Triggers) {
            if ($trigger.Type -eq 9 -and $trigger.Enabled) { $login = $true }
        }
    }
    @{
        registered = ($null -ne $task)
        loaded = ($null -ne $task -and $task.State -eq 4)
        login_enabled = $login
        state = $(if ($null -ne $task) { [int]$task.State } else { $null })
        last_exit_code = $(if ($null -ne $task) { $task.LastTaskResult } else { $null })
    } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine('Windows Task Scheduler control failed: ' + $_.Exception.GetType().Name)
    exit 1
}
'''


def prepare(data_dir, python=None, project_dir=None):
    from .install import _identity
    root, label, _ = _identity(data_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    project = Path(project_dir or Path(__file__).resolve().parent.parent).resolve()
    executable = Path(python or sys.executable).resolve()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    windowless = executable.with_name("pythonw.exe")
    if windowless.is_file():
        executable = windowless
    ET.register_namespace("", TASK_NS)
    task = ET.Element("{" + TASK_NS + "}Task", version="1.2")
    def node(parent, name, text=None):
        item = ET.SubElement(parent, "{" + TASK_NS + "}" + name)
        item.text = text
        return item
    registration = node(task, "RegistrationInfo")
    node(registration, "Description", "MonitorBot:" + str(root))
    node(task, "Triggers")
    principal = node(node(task, "Principals"), "Principal")
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    node(settings, "MultipleInstancesPolicy", "IgnoreNew")
    node(settings, "DisallowStartIfOnBatteries", "false")
    node(settings, "StopIfGoingOnBatteries", "false")
    node(settings, "ExecutionTimeLimit", "PT0S")
    restart = node(settings, "RestartOnFailure")
    node(restart, "Interval", "PT1M")
    node(restart, "Count", "3")
    action = node(node(task, "Actions"), "Exec")
    node(action, "Command", str(executable))
    node(action, "Arguments", subprocess.list2cmdline(["-m", "monitorbot", "--home", str(root), "run", "--loop"]))
    node(action, "WorkingDirectory", str(project))
    path = root / "windows-task.xml"
    if path.is_symlink():
        raise ValueError("Windows task definition must not be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".windows-task-", dir=str(root))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(ET.tostring(task, encoding="utf-16", xml_declaration=True))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": str(path), "label": label, "prepared": True, "started": False,
            "note": "Run start to register and run the Windows task. Use --login for login startup."}


def control(data_dir, action, login=False):
    from .install import _identity
    if sys.platform != "win32":
        raise RuntimeError("Windows Task Scheduler requires Windows")
    root, label, _ = _identity(data_dir)
    path = root / "windows-task.xml"
    if action == "start" and not path.is_file():
        raise ValueError("Run install before start")
    request = dict(name=label, owner="MonitorBot:" + str(root), path=str(path), action=action, login=login)
    encoded = base64.b64encode(CONTROL.encode("utf-16le")).decode("ascii")
    process = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                             input=json.dumps(request), capture_output=True, text=True, timeout=30)
    if process.returncode:
        raise RuntimeError("Windows Task Scheduler control failed with exit code " + str(process.returncode))
    try:
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or any(type(result.get(key)) is not bool for key in
                                             ("registered", "loaded", "login_enabled")):
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError("Windows Task Scheduler returned an invalid status") from None
    return dict(result, path=str(path), label=label, prepared=path.is_file(), supported=True)
