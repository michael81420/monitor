"""Review and verify code changes before applying them to the background service."""

import difflib
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .agents import AgentError, AgentRunner


PROPOSAL_SCHEMA = {
    "type": "object", "properties": {
        "summary": {"type": "string", "maxLength": 4000},
        "evidence_log_ids": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string", "maxLength": 100000},
                "reason": {"type": "string", "maxLength": 2000}},
            "required": ["path", "content", "reason"], "additionalProperties": False}},
    }, "required": ["summary", "evidence_log_ids", "changes"], "additionalProperties": False,
}
ASSESSMENT_SCHEMA = {
    "type": "object", "properties": {
        "concerns": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
    }, "required": ["concerns", "constraints"], "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object", "properties": {
        "approved": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 4000}},
    "required": ["approved", "reason"], "additionalProperties": False,
}


class VerificationBlocked(RuntimeError):
    pass


PROTECTED = {"monitorbot/improve.py", "monitorbot/agents.py", "monitorbot/__init__.py", "monitorbot/__main__.py", "monitorbot/install.py", "monitorbot/menubar.py"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _allowed(path, root):
    item = PurePosixPath(path)
    if item.is_absolute() or str(item) != path or ".." in item.parts or "\\" in path:
        return False
    if path in PROTECTED:
        return False
    permitted = (len(item.parts) == 2 and item.parts[0] in ("monitorbot", "tests") and item.suffix == ".py")
    permitted = permitted or path == "skills/monitorbot/SKILL.md"
    if not permitted:
        return False
    destination = root / path
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents if parent != root.parent):
        return False
    return root.resolve() in destination.resolve().parents


def _source_files(root):
    files = {}
    for pattern in ("monitorbot/*.py", "tests/*.py", "skills/monitorbot/SKILL.md"):
        for path in sorted(root.glob(pattern)):
            if path.is_file() and not path.is_symlink() and root.resolve() in path.resolve().parents:
                relative = path.relative_to(root).as_posix()
                files[relative] = path.read_text(encoding="utf-8")
    return files


def _report(data_dir):
    directory = data_dir / "reports" / "system"
    if directory.is_symlink() or data_dir.resolve() not in directory.resolve().parents:
        return ""
    reports = [path for path in directory.rglob("*.md")
               if not path.is_symlink() and directory.resolve() in path.resolve().parents]
    if not reports:
        return ""
    latest = max(reports, key=lambda path: path.stat().st_mtime_ns)
    return latest.read_text(encoding="utf-8")[:12000]


def _proposal_changes(proposal, original, root):
    from .agents import validate_json
    validate_json(proposal, PROPOSAL_SCHEMA)
    seen = set()
    changes = []
    for change in proposal["changes"]:
        path = change["path"]
        if path in seen or not _allowed(path, root):
            raise VerificationBlocked("proposal contains a duplicate, protected or unsupported path")
        seen.add(path)
        if path.startswith("tests/") and path in original:
            raise VerificationBlocked("existing regression tests are immutable")
        if original.get(path) != change["content"]:
            changes.append(change)
    return changes


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".monitor-change-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _profile(candidate, readable):
    candidate = candidate.resolve()
    allowed_read = ["/System", "/usr", "/bin", "/sbin", "/private/var/db/timezone",
                    str(Path(sys.base_prefix).resolve()), str(Path(sys.executable).resolve()), str(candidate)]
    allowed_read.extend(str(Path(path).resolve()) for path in readable)
    paths = " ".join("(subpath " + json.dumps(path) + ")" for path in dict.fromkeys(allowed_read))
    return ("(version 1)\n(deny default)\n(allow file-read-metadata)\n"
            '(allow file-read-data (literal "/"))\n'
            "(allow file-read* " + paths + " (literal \"/dev/null\") (literal \"/dev/urandom\"))\n"
            "(allow file-write* (subpath " + json.dumps(str(candidate / ".test-tmp")) + ")"
            " (subpath " + json.dumps(str(candidate / ".migration-data")) + "))\n"
            "(allow process-fork)\n(allow process-exec)\n(allow signal (target self))\n"
            "(allow sysctl-read)\n(deny network*)\n")


def _sandbox(candidate, control, script, arguments=(), timeout=180, readable=()):
    executable = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not executable:
        raise VerificationBlocked("macOS sandbox-exec is unavailable; automatic application is blocked")
    profile = control / "verification.sb"
    profile.write_text(_profile(candidate, [script] + list(readable)), encoding="utf-8")
    temp = candidate / ".test-tmp"
    temp.mkdir(exist_ok=True)
    env = {"PATH": os.defpath, "TMPDIR": str(temp), "LANG": "en_US.UTF-8", "HOME": str(Path.home())}
    command = [executable, "-f", str(profile), sys.executable, "-I", "-B", str(script)] + list(arguments)
    try:
        process = subprocess.Popen(command, cwd=str(candidate), env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise VerificationBlocked("isolated verification could not finish") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise VerificationBlocked("isolated verification timed out") from None
    if process.returncode:
        if b"sandbox_apply" in stderr or b"Operation not permitted" in stderr:
            raise VerificationBlocked("OS sandbox could not be established")
        raise VerificationBlocked("isolated verification failed")
    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise VerificationBlocked("isolated verification produced invalid evidence") from None
    if not isinstance(result, dict):
        raise VerificationBlocked("isolated verification produced invalid evidence")
    return result


PROBE = '''import errno,json,pathlib,socket,sys
outside=pathlib.Path(sys.argv[1])
result={}
try:
    outside.read_text()
    result['outside_read_denied']=False
except PermissionError:
    result['outside_read_denied']=True
try:
    outside.with_name('forbidden-write').write_text('probe')
    result['outside_write_denied']=False
except PermissionError:
    result['outside_write_denied']=True
try:
    with socket.socket() as handle:
        handle.bind(('127.0.0.1',0))
    result['network_denied']=False
except PermissionError:
    result['network_denied']=True
print(json.dumps(result))
'''

TEST_DRIVER = '''import io,json,pathlib,sys,unittest
from unittest import case,loader,result,runner,suite
def verify():
    candidate,baseline=map(pathlib.Path,sys.argv[1:3])
    mode=sys.argv[3]
    # Keep the test oracle before candidate imports can change unittest.
    core_modules=(unittest,case,loader,result,runner,suite)
    core_classes=tuple({value for module in core_modules for value in vars(module).values()
                        if isinstance(value,type) and value.__module__.startswith('unittest')})
    module_refs=[(module,{name:value for name,value in vars(module).items()
                          if isinstance(value,type) or hasattr(value,'__code__')}) for module in core_modules]
    class_refs=[(cls,dict(vars(cls))) for cls in core_classes]
    function_refs=[(value,value.__code__) for _,attributes in class_refs+module_refs
                   for value in attributes.values() if hasattr(value,'__code__')]
    TestCase,TestSuite,TestLoader=unittest.TestCase,unittest.TestSuite,unittest.TestLoader
    TextTestRunner,TestResult=unittest.TextTestRunner,unittest.TestResult
    run_tests=TextTestRunner.run
    successful=TestResult.wasSuccessful
    load_names=TestLoader.loadTestsFromNames
    count_tests=TestSuite.countTestCases
    stream=io.StringIO
    serialize=json.dumps

    def unchanged():
        return (all(getattr(module,name,None) is value for module,attributes in module_refs
                    for name,value in attributes.items())
                and all(dict(vars(cls))==attributes for cls,attributes in class_refs)
                and all(value.__code__ is code for value,code in function_refs))

    def must_fail(self):
        raise AssertionError('trusted failure canary')

    Canary=type('Canary',(TestCase,),{'test_failure':must_fail})

    def canary():
        check=run_tests(TextTestRunner(stream=stream(),verbosity=0),TestSuite([Canary('test_failure')]))
        return check.testsRun==1 and len(check.failures)==1 and not check.errors and not successful(check)

    sys.path.insert(0,str(candidate))
    sys.path.insert(1,str(candidate/'tests'))
    for directory in (candidate/'monitorbot',candidate/'tests'):
        for file in directory.glob('*.py'):
            compile(file.read_text(encoding='utf-8'),str(file),'exec')
    original={file.name:file.read_bytes() for file in baseline.glob('test*.py')}
    for name,content in original.items():
        if (candidate/'tests'/name).read_bytes()!=content:
            raise ValueError('baseline was changed')
    selected=[file.stem for file in (candidate/'tests').glob('test*.py')
              if (file.name in original)==(mode=='baseline')]
    tests=load_names(TestLoader(),sorted(selected))
    expected=count_tests(tests)
    trusted=unchanged() and canary() and unchanged()
    if trusted:
        outcome=run_tests(TextTestRunner(stream=stream(),verbosity=0),tests)
        trusted=unchanged() and canary() and unchanged()
        passed=trusted and successful(outcome) and (expected>0 or mode!='baseline') and outcome.testsRun==expected
        evidence={'passed':passed,'baseline_tests':expected if mode=='baseline' else 0,
                  'tests_run':outcome.testsRun,'failures':len(outcome.failures),'errors':len(outcome.errors)}
    else:
        evidence={'passed':False,'baseline_tests':expected if mode=='baseline' else 0,
                  'tests_run':0,'failures':0,'errors':0}
    evidence['oracle_intact']=trusted
    print(serialize(evidence))
verify()
'''

MIGRATION_DRIVER = '''import json,pathlib,sqlite3,sys
candidate,data=map(pathlib.Path,sys.argv[1:])
connect=sqlite3.connect
def rows(connection):
    result={}
    tables=connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for (name,) in tables:
        quoted='"'+name.replace('"','""')+'"'
        columns=[row[1] for row in connection.execute('PRAGMA table_info('+quoted+')')]
        values=connection.execute('SELECT * FROM '+quoted).fetchall()
        result[name]=(columns,sorted(repr(row) for row in values))
    return result
before_db=connect(str(data/'monitor.sqlite3'))
before=rows(before_db)
before_db.close()
sys.path.insert(0,str(candidate))
from monitorbot.store import Store
instance=Store(data)
instance.close()
after_db=connect(str(data/'monitor.sqlite3'))
preserved=True
for name,(columns,values) in before.items():
    quoted='"'+name.replace('"','""')+'"'
    try:
        projection=','.join('"'+column.replace('"','""')+'"' for column in columns)
        current=sorted(repr(row) for row in after_db.execute('SELECT '+projection+' FROM '+quoted).fetchall())
        preserved=preserved and current==values
    except sqlite3.Error:
        preserved=False
integrity=after_db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
after_db.close()
print(json.dumps({'preserved':preserved,'integrity':integrity,'tables_checked':len(before)}))
'''


def _verify(candidate, original, changes, database):
    with tempfile.TemporaryDirectory(prefix="monitor-verify-") as temporary:
        control = Path(temporary)
        baseline = control / "baseline_tests"
        baseline.mkdir()
        (baseline / "__init__.py").write_text("")
        for name, content in original.items():
            if name.startswith("tests/"):
                (baseline / Path(name).name).write_text(content, encoding="utf-8")
        forbidden = control / "private-probe"
        forbidden.write_text("synthetic private content")
        probe = control / "probe.py"
        probe.write_text(PROBE)
        checks = _sandbox(candidate, control, probe, [str(forbidden)])
        if not all(checks.get(key) is True for key in ("outside_read_denied", "outside_write_denied", "network_denied")):
            raise VerificationBlocked("sandbox did not enforce all required boundaries")
        driver = control / "tests.py"
        driver.write_text(TEST_DRIVER)
        tests = _sandbox(candidate, control, driver, [str(candidate), str(baseline), "baseline"],
                         readable=[baseline])
        if not tests.get("passed") or tests.get("baseline_tests", 0) < 1:
            raise VerificationBlocked("unchanged baseline or candidate tests failed")
        added_tests = _sandbox(candidate, control, driver, [str(candidate), str(baseline), "added"],
                              readable=[baseline])
        if not added_tests.get("passed"):
            raise VerificationBlocked("new candidate tests failed")
        tests["added_tests"] = added_tests
        migration = {"required": True}
        data = candidate / ".migration-data"
        data.mkdir()
        shutil.copy2(database, data / "monitor.sqlite3")
        driver = control / "migration.py"
        driver.write_text(MIGRATION_DRIVER)
        _sandbox(candidate, control, driver, [str(candidate), str(data)])
        migration.update(_migration_evidence(database, data / "monitor.sqlite3"))
        if not migration.get("preserved") or not migration.get("integrity"):
            raise VerificationBlocked("candidate data migration did not preserve existing records")
        return {"sandbox": checks, "tests": tests, "migration": migration}


def _migration_evidence(original, migrated):
    """Inspect the child database in the parent; candidate Python cannot supply this verdict."""
    if migrated.is_symlink():
        raise VerificationBlocked("candidate migration database is a symlink")
    connections = []
    try:
        for path in (original, migrated):
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("BEGIN")
            connections.append(connection)
        before, after = connections
        tables = before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        current_tables = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        preserved = True
        for (name,) in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in before.execute('PRAGMA table_info(' + quoted + ')')]
            current_columns = {row[1] for row in after.execute('PRAGMA table_info(' + quoted + ')')}
            if name not in current_tables or not set(columns) <= current_columns:
                preserved = False
                break
            projection = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
            query = 'SELECT ' + projection + ' FROM ' + quoted
            if sorted(map(repr, before.execute(query))) != sorted(map(repr, after.execute(query))):
                preserved = False
                break
        integrity = after.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return {"preserved": preserved, "integrity": integrity, "tables_checked": len(tables),
                "verified_by": "parent_sqlite"}
    except sqlite3.Error:
        raise VerificationBlocked("candidate data migration could not be independently inspected") from None
    finally:
        for connection in connections:
            connection.close()


def _snapshot(database, destination):
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(str(destination))
        try:
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise VerificationBlocked("database backup failed integrity verification")
            copy.commit()
        finally:
            copy.close()
    finally:
        source.close()
    destination.chmod(0o600)
    restored = destination.with_suffix(".restore-check.sqlite3")
    backup = sqlite3.connect(str(destination))
    target = sqlite3.connect(str(restored))
    try:
        backup.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise VerificationBlocked("database backup could not be restored")
        if list(backup.iterdump()) != list(target.iterdump()):
            raise VerificationBlocked("restored database differs from backup")
    finally:
        target.close()
        backup.close()
        if restored.exists():
            restored.unlink()


def _apply(root, changes, original, artifact):
    journal = []
    if _source_files(root) != original:
        raise VerificationBlocked("code or regression tests changed during verification")
    for change in changes:
        path = root / change["path"]
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != original.get(change["path"]):
            raise VerificationBlocked("source changed during verification; refusing stale proposal")
        if current is not None:
            backup = artifact / "original" / change["path"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(current, encoding="utf-8")
    rollback = {"status": "prepared", "files": {change["path"]: {
        "before": original.get(change["path"]), "after": change["content"]} for change in changes}}
    _write(artifact / "rollback.json", json.dumps(rollback, ensure_ascii=False))
    try:
        for change in changes:
            path = root / change["path"]
            _write(path, change["content"])
            journal.append(change["path"])
        rollback["status"] = "applied"
        _write(artifact / "rollback.json", json.dumps(rollback, ensure_ascii=False))
    except BaseException:
        for name in reversed(journal):
            if name in original:
                _write(root / name, original[name])
            else:
                (root / name).unlink(missing_ok=True)
        rollback["status"] = "rolled_back"
        _write(artifact / "rollback.json", json.dumps(rollback, ensure_ascii=False))
        raise


def _database_unchanged(database, backup):
    connections = []
    try:
        for path in (database, backup):
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("BEGIN")
            connections.append(connection)
        return list(connections[0].iterdump()) == list(connections[1].iterdump())
    finally:
        for connection in connections:
            connection.close()


def recover_pending(root, data_dir):
    recovered = []
    for path in (data_dir / "improvements").glob("*/rollback.json"):
        if path.is_symlink():
            raise VerificationBlocked("unsafe rollback artifact")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") != "prepared":
            continue
        for name, content in record["files"].items():
            if not _allowed(name, root) or not isinstance(content, dict):
                raise VerificationBlocked("unsafe rollback path")
            current = (root / name).read_text(encoding="utf-8") if (root / name).exists() else None
            if current not in (content["before"], content["after"]):
                raise VerificationBlocked("interrupted update has subsequent edits; manual recovery required")
        for name, content in record["files"].items():
            if content["before"] is None:
                (root / name).unlink(missing_ok=True)
            else:
                _write(root / name, content["before"])
        record["status"] = "rolled_back"
        _write(path, json.dumps(record, ensure_ascii=False))
        recovered.append(path.parent.name)
    return recovered


def improve(store, config, runner=None):
    settings = config.get("improvement", {})
    if not settings.get("enabled", True):
        return {"status": "skipped", "reason": "automatic improvement is disabled"}
    root = Path(settings.get("project_dir", Path(__file__).resolve().parent.parent)).resolve()
    data_dir = Path(config.get("data_dir", store.data_dir)).resolve()
    try:
        recovered = recover_pending(root, data_dir)
    except (OSError, ValueError, VerificationBlocked) as error:
        return {"status": "blocked", "reason": "interrupted update requires recovery: " + type(error).__name__}
    for proposal in recovered:
        store.log("improvement_rolled_back", {"proposal_hash": proposal})
    original = _source_files(root)
    logs = [record for record in store.logs(limit=100)
            if not str(record.get("kind", "")).startswith("improvement_")][:20]
    logs = [dict(record, detail=json.dumps(record.get("detail"), ensure_ascii=False)[:1000]) for record in logs]
    report = _report(data_dir)
    if not logs and not report:
        return {"status": "skipped", "reason": "no system evidence to review"}
    issues = store.get_meta("improvement_issues", {})
    inputs = {"system_report": report, "logs": logs}
    issue_id = _digest(inputs)[:24]
    if issue_id in issues:
        return {"status": "skipped", "issue_id": issue_id, "reason": "unchanged evidence was already reviewed"}
    issue = {"status": "discovered", "updated_at": _now()}
    issues[issue_id] = issue
    store.set_meta("improvement_issues", issues)
    runner = runner or AgentRunner(config, data_dir)
    artifact = None
    proposal_hash = None
    try:
        code, omitted, remaining = {}, [], 60000
        for name, content in original.items():
            if name not in PROTECTED and len(content) <= remaining:
                code[name] = content
                remaining -= len(content)
            else:
                omitted.append(name)
        previous = [{key: str(value)[:500] for key, value in item.items()
                     if key in ("status", "attempt_status", "reason", "proposal_hash")}
                    for item in list(issues.values())[-10:]]
        payload = dict(inputs, code=code, omitted_files=omitted,
                       previous_attempts=previous,
                       instruction="只提出有紀錄依據的必要程式或skill修正。缺少登入、權限或外部資料不能用程式假造。"
                       "提供完整替換檔案，不刪改既有測試，不修改improve.py、agents.py、__init__.py、__main__.py、install.py、menubar.py，"
                       "不接觸設定或正式資料。"
                       "沒有可證明的新方法時changes回傳空陣列。")
        proposal = runner.run("improver", payload, PROPOSAL_SCHEMA)
        changes = _proposal_changes(proposal, original, root)
        if any(item not in {record.get("id") for record in logs} for item in proposal["evidence_log_ids"]):
            raise VerificationBlocked("proposal cites unknown operation records")
        if not changes:
            issue.update(attempt_status="no_change", reason=proposal["summary"])
            store.set_meta("improvement_issues", issues)
            return {"status": "skipped", "issue_id": issue_id, "reason": "no justified code change proposed"}
        proposal_hash = _digest(sorted(({"path": item["path"], "content": item["content"]}
                                        for item in changes), key=lambda item: item["path"]))
        attempts = store.get_meta("improvement_attempts", {})
        if proposal_hash in attempts:
            issue.update(attempt_status="duplicate", proposal_hash=proposal_hash)
            store.set_meta("improvement_issues", issues)
            return {"status": "skipped", "issue_id": issue_id, "proposal_hash": proposal_hash,
                    "reason": "identical proposal was already attempted"}
        artifact = data_dir / "improvements" / proposal_hash
        artifact.mkdir(parents=True, exist_ok=True, mode=0o700)
        patch = "".join("".join(difflib.unified_diff(original.get(item["path"], "").splitlines(True),
                                                    item["content"].splitlines(True),
                                                    fromfile="a/" + item["path"], tofile="b/" + item["path"]))
                        for item in changes)
        _write(artifact / "proposal.json", json.dumps(proposal, ensure_ascii=False, indent=2))
        _write(artifact / "change.diff", patch)
        issue.update(status="in_progress", proposal_hash=proposal_hash, artifact_dir=str(artifact))
        attempts[proposal_hash] = {"status": "in_progress", "issue_id": issue_id}
        store.set_meta("improvement_issues", issues)
        store.set_meta("improvement_attempts", attempts)
        prior = runner.run("change_reviewer", dict(inputs, original_code={item["path"]: original.get(item["path"], "")
                                                                        for item in changes},
                           instruction="尚未提供修改方案。先獨立判斷原始問題、必要約束與不能破壞的行為。"),
                           ASSESSMENT_SCHEMA)
        _snapshot(Path(store.path), artifact / "database-backup.sqlite3")
        with tempfile.TemporaryDirectory(prefix="monitor-candidate-") as temporary:
            candidate = Path(temporary)
            for name, content in original.items():
                _write(candidate / name, content)
            for item in changes:
                _write(candidate / item["path"], item["content"])
            verification = _verify(candidate, original, changes, artifact / "database-backup.sqlite3")
        _write(artifact / "verification.json", json.dumps(verification, ensure_ascii=False, indent=2))
        review = runner.run("change_reviewer", dict(inputs, independent_assessment=prior, patch=patch,
                            verification=verification,
                            instruction="檢查實際修改是否解決原問題並遵守先前獨立約束。"
                            "固定基準測試與資料保留證據必須成立，不能只憑另一個agent同意。"), REVIEW_SCHEMA)
        from .agents import validate_json
        validate_json(review, REVIEW_SCHEMA)
        _write(artifact / "review.json", json.dumps(review, ensure_ascii=False, indent=2))
        if not review["approved"]:
            raise VerificationBlocked("independent change review rejected the proposal")
        if settings.get("auto_apply", True):
            with store.mutation_lock():
                if not _database_unchanged(Path(store.path), artifact / "database-backup.sqlite3"):
                    raise VerificationBlocked("live data changed during verification; refusing stale data proof")
                _apply(root, changes, original, artifact)
            status = "applied"
        else:
            status = "verified"
        issue.update(status="verified" if status == "applied" else "in_progress",
                     attempt_status=status, reason=review["reason"], updated_at=_now())
        attempts[proposal_hash].update(status=status)
        store.set_meta("improvement_attempts", attempts)
        store.set_meta("improvement_issues", issues)
        result = {"status": status, "issue_id": issue_id, "proposal_hash": proposal_hash,
                  "artifact_dir": str(artifact), "reason": review["reason"]}
        store.log("improvement_" + status, result)
        return result
    except (AgentError, VerificationBlocked, OSError, ValueError, sqlite3.Error) as error:
        reason = str(error) if isinstance(error, VerificationBlocked) else type(error).__name__
        issue.update(status="in_progress", attempt_status="blocked", reason=reason, updated_at=_now())
        store.set_meta("improvement_issues", issues)
        if proposal_hash:
            attempts = store.get_meta("improvement_attempts", {})
            attempts[proposal_hash] = {"status": "blocked", "issue_id": issue_id, "reason": reason}
            store.set_meta("improvement_attempts", attempts)
        result = {"status": "blocked", "issue_id": issue_id, "proposal_hash": proposal_hash,
                  "reason": reason, "artifact_dir": str(artifact) if artifact else None}
        store.log("improvement_blocked", result)
        return result
