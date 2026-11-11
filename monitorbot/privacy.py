"""Owner-only permissions for local authentication files."""

import base64
import json
import os
from pathlib import Path
import subprocess


WINDOWS_ACL = r'''
$ErrorActionPreference = 'Stop'
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($request.action -eq 'protect') {
        $acl = New-Object Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
        [IO.File]::SetAccessControl($request.path, $acl)
    }
    $acl = Get-Acl -LiteralPath $request.path
    $private = $acl.AreAccessRulesProtected -and $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -eq 0) { $private = $false }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -ne $sid.Value -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne [Security.AccessControl.FileSystemRights]::FullControl) {
            $private = $false
        }
    }
    ConvertTo-Json -Compress -InputObject ([bool]$private)
} catch {
    [Console]::Error.WriteLine('Authentication file permissions could not be verified')
    exit 1
}
'''


def _windows_acl(path, action):
    encoded = base64.b64encode(WINDOWS_ACL.encode('utf-16le')).decode('ascii')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        input=json.dumps({'path': str(path.resolve()), 'action': action}),
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        raise OSError('Authentication file permissions could not be verified')
    try:
        private = json.loads(result.stdout)
    except ValueError:
        raise OSError('Authentication file permissions returned an invalid result') from None
    if type(private) is not bool:
        raise OSError('Authentication file permissions returned an invalid result')
    return private


def set_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise OSError('Authentication file must be a regular file')
    if os.name == 'nt':
        if not _windows_acl(path, 'protect'):
            raise OSError('Authentication file permissions are not owner-only')
    else:
        path.chmod(0o600)


def is_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    if os.name == 'nt':
        return _windows_acl(path, 'check')
    return not bool(path.stat().st_mode & 0o077)
