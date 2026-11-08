"""Local records and the rules that keep task changes reviewable."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid


ACTIVE = {"active", "waiting"}
CLOSED = {"completed", "transferred", "cancelled", "excluded"}
METADATA_FIELDS = {
    "path", "line", "session_id", "role", "profile", "visit_id", "domain",
    "account", "folder_id", "sender", "recipients", "participants",
    "conversation_id", "chat_id", "team_id", "channel_id", "message_id",
    "reply_to_id", "graph_path", "version", "content_available", "coverage",
    "document_id", "app", "application", "updated_at", "kind", "offset",
}
TASK_FIELDS = {
    "id", "title", "outcome", "project", "kind", "ownership", "status",
    "due_at", "waiting_until", "waiting_for", "next_action", "priority",
    "blocks_others", "evidence_ids", "last_activity_at", "explicit_excluded",
    "end_reason", "excluded_by",
}


def timestamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id():
    return uuid.uuid4().hex


def _locator_url(value):
    value = _bounded_text(value, "url", 8000)
    parts = urlsplit(value)
    secret_fields = {"access_token", "refresh_token", "id_token", "token", "api_key", "apikey", "password", "passwd", "secret", "code"}
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if key.casefold() not in secret_fields]
    fragment = parts.fragment
    if "=" in fragment:
        pairs = parse_qsl(fragment, keep_blank_values=True)
        if any(key.casefold() in secret_fields for key, _ in pairs):
            fragment = urlencode([(key, val) for key, val in pairs if key.casefold() not in secret_fields])
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, urlencode(query), fragment))


def _bounded_text(value, field, limit=4000, allow_empty=True):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise ValueError("invalid " + field)
    return value


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data_dir / "monitor.sqlite3"
        self.db = sqlite3.connect(str(self.path), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                data TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0, UNIQUE(source,source_id)
            );
            CREATE INDEX IF NOT EXISTS events_time ON events(occurred_at);
            CREATE INDEX IF NOT EXISTS events_pending ON events(processed,excluded,occurred_at,id);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS history_time ON history(occurred_at);
            CREATE TABLE IF NOT EXISTS knowledge (
                id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_history (
                id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferences (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                source TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logs (
                id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, data TEXT NOT NULL
            );
        """)
        self.path.chmod(0o600)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    @contextmanager
    def mutation_lock(self):
        """Keep the final data check and code replacement in one short write lock."""
        with self._write():
            yield

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT data FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self._write():
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, _json(value)))

    def add_event(self, event):
        source = _bounded_text(event.get("source"), "source", 100, False)
        source_id = _bounded_text(event.get("source_id"), "source_id", 2000, False)
        event_id = hashlib.sha256((source + "\0" + source_id).encode()).hexdigest()
        metadata = {}
        for key, value in event.get("metadata", {}).items():
            if key not in METADATA_FIELDS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                metadata[key] = value
            elif isinstance(value, list) and all(isinstance(v, (str, int, float, bool)) for v in value):
                metadata[key] = value
        data = {
            "id": event_id, "source": source, "source_id": source_id,
            "occurred_at": timestamp(event.get("occurred_at")),
            "title": _bounded_text(event.get("title", ""), "title", 2000),
            "url": _locator_url(event.get("url", "")),
            "metadata": metadata,
        }
        with self._write():
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO events(id,source,source_id,occurred_at,recorded_at,data) VALUES (?,?,?,?,?,?)",
                (event_id, source, source_id, data["occurred_at"], timestamp(), _json(data)),
            ).rowcount == 1
        result = self.get_event(event_id)
        result["inserted"] = inserted
        return result

    def get_event(self, event_id):
        row = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise KeyError("unknown event: " + event_id)
        data = json.loads(row["data"])
        data.update(processed=bool(row["processed"]), excluded=bool(row["excluded"]), recorded_at=row["recorded_at"])
        return data

    def events(self, since=None, until=None, unprocessed=False, limit=500, source=None, offset=0, excluded=None):
        clauses, params = [], []
        if source is not None:
            clauses.append("source=?")
            params.append(_bounded_text(source, "source", 100, False))
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer or None")
        for boundary, operator in ((since, ">="), (until, "<")):
            if boundary is not None:
                clauses.append("occurred_at " + operator + " ?")
                params.append(timestamp(boundary))
        if unprocessed:
            clauses.append("processed=0")
        if excluded is not None:
            if type(excluded) is not bool:
                raise ValueError("excluded must be boolean or None")
            clauses.append("excluded=?")
            params.append(int(excluded))
        sql = "SELECT id FROM events" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY occurred_at,id LIMIT ? OFFSET ?"
        params.extend((-1 if limit is None else limit, offset))
        return [self.get_event(row[0]) for row in self.db.execute(sql, params)]

    def count_events(self, unprocessed=False, source=None, excluded=None):
        clauses, params = [], []
        if unprocessed:
            clauses.append("processed=0")
        if source is not None:
            clauses.append("source=?")
            params.append(_bounded_text(source, "source", 100, False))
        if excluded is not None:
            if type(excluded) is not bool:
                raise ValueError("excluded must be boolean or None")
            clauses.append("excluded=?")
            params.append(int(excluded))
        sql = "SELECT COUNT(*) FROM events" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        return self.db.execute(sql, params).fetchone()[0]

    def search(self, query, limit=30):
        query = _bounded_text(query, "search query", 500, False).strip()
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("search limit must be an integer from 1 to 500")
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        result = {}
        for table, order in (("tasks", "updated_at"), ("knowledge", "updated_at"), ("events", "occurred_at")):
            sql = "SELECT id,data FROM " + table + " WHERE data LIKE ? ESCAPE '\\'"
            if table == "events":
                sql += " AND excluded=0"
            elif table == "tasks":
                sql += " AND status!='excluded'"
            sql += " ORDER BY " + order + " DESC,id LIMIT ?"
            rows = self.db.execute(sql, (pattern, limit)).fetchall()
            result[table] = [self.get_event(row[0]) if table == "events" else json.loads(row[1]) for row in rows]
        return result

    def mark_processed(self, ids):
        with self._write():
            self.db.executemany("UPDATE events SET processed=1 WHERE id=?", ((item,) for item in ids))

    def set_source_status(self, source, status, detail="", cursor=None, checked_at=None):
        if status not in {"ok", "partial", "error", "unconfigured", "excluded"}:
            raise ValueError("invalid source status")
        previous = self.health().get(source, {})
        data = {"source": source, "status": status, "detail": _bounded_text(detail, "detail"),
                "checked_at": timestamp(checked_at), "cursor": previous.get("cursor")}
        if cursor is not None and status in {"ok", "partial"}:
            data["cursor"] = cursor
        if status == "ok":
            data["last_success_at"] = data["checked_at"]
        elif previous.get("last_success_at"):
            data["last_success_at"] = previous["last_success_at"]
        with self._write():
            self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?)", (source, _json(data)))
        return data

    def health(self):
        return {row[0]: json.loads(row[1]) for row in self.db.execute("SELECT source,data FROM sources")}

    def get_cursor(self, source):
        return self.health().get(source, {}).get("cursor")

    def get_task(self, task_id):
        row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("unknown task: " + task_id)
        return json.loads(row[0])

    def _evidence(self, ids):
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("evidence_ids must be a list of event IDs")
        return [self.get_event(item) for item in dict.fromkeys(ids)]

    def _validate_task(self, task):
        _bounded_text(task["title"], "title", 2000, False)
        for field in ("outcome", "project", "waiting_for", "next_action", "end_reason"):
            _bounded_text(task[field], field)
        if task["status"] not in ACTIVE | CLOSED:
            raise ValueError("invalid task status")
        if task["kind"] not in {"optional", "candidate", "assigned"}:
            raise ValueError("invalid task kind")
        if task["ownership"] not in {"self", "other", "unknown"}:
            raise ValueError("invalid task ownership")
        if type(task["priority"]) is not int or not 0 <= task["priority"] <= 100:
            raise ValueError("priority must be an integer from 0 to 100")
        for field in ("explicit_excluded", "blocks_others"):
            if type(task[field]) is not bool:
                raise ValueError(field + " must be boolean")
        for field in ("due_at", "waiting_until", "last_activity_at"):
            if task.get(field) is not None:
                task[field] = timestamp(task[field])
        self._evidence(task["evidence_ids"])

    def _source_coverage(self, task, now):
        health = self.health()
        evidence = self._evidence(task["evidence_ids"])
        if evidence and all(event["excluded"] for event in evidence):
            return False
        for event in evidence:
            if event["excluded"]:
                continue
            if event["source"] == "manual":
                continue
            source = health.get(event["source"], {})
            if source.get("status") != "ok":
                return False
            checked = source.get("checked_at")
            if not checked or datetime.fromisoformat(now) - datetime.fromisoformat(checked) > timedelta(hours=2):
                return False
        return True

    def _idle(self, task, now, days):
        return (task["status"] == "active" and task["kind"] in {"optional", "candidate"}
                and task["due_at"] is None and not task["waiting_for"] and task["waiting_until"] is None
                and not task["explicit_excluded"]
                and datetime.fromisoformat(now) - datetime.fromisoformat(task["last_activity_at"]) >= timedelta(days=days)
                and self._source_coverage(task, now))

    def _save_task(self, task, previous, actor, reason, now, substantive, occurred_at=None):
        task["revision"] = (previous["revision"] if previous else 0) + 1
        task["updated_at"] = now
        if previous:
            task["created_at"] = previous["created_at"]
            if not substantive:
                task["last_activity_at"] = previous["last_activity_at"]
        else:
            task["created_at"] = now
        record = {"id": _id(), "task_id": task["id"], "actor": actor, "reason": reason,
                  "occurred_at": timestamp(occurred_at) if occurred_at is not None else now,
                  "recorded_at": now, "before": previous,
                  "after": task, "revision": task["revision"], "evidence_ids": task["evidence_ids"]}
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)",
                        (task["id"], task["status"], now, _json(task)))
        self.db.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                        (record["id"], task["id"], record["occurred_at"], record["recorded_at"], _json(record)))
        return task

    def upsert_task(self, data, actor="user", reason="", substantive=True,
                    reviewed=False, expected_revision=None, now=None, occurred_at=None):
        data = {key: value for key, value in data.items() if key != "id" or value is not None}
        unknown = set(data) - TASK_FIELDS
        if unknown:
            raise ValueError("unknown task fields: " + ", ".join(sorted(unknown)))
        now = timestamp(now)
        with self._write():
            row = self.db.execute("SELECT data FROM tasks WHERE id=?", (data.get("id", ""),)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and expected_revision is not None and previous["revision"] != expected_revision:
                raise ValueError("stale task revision")
            task = dict(previous) if previous else {
                "id": _id(), "title": "", "outcome": "", "project": "", "kind": "candidate",
                "ownership": "unknown", "status": "active", "due_at": None,
                "waiting_until": None, "waiting_for": "", "next_action": "", "priority": 50,
                "blocks_others": False, "evidence_ids": [], "last_activity_at": now,
                "explicit_excluded": False, "end_reason": "", "excluded_by": "",
            }
            task.update(data)
            _bounded_text(task["id"], "task id", 200, False)
            task["evidence_ids"] = list(dict.fromkeys(task["evidence_ids"]))
            if substantive and "last_activity_at" not in data:
                task["last_activity_at"] = now
            self._validate_task(task)
            self._check_transition(previous, task, actor, reviewed, expected_revision, reason, now)
            if previous and "last_activity_at" not in data and all(
                    previous.get(key) == task.get(key) for key in TASK_FIELDS - {"last_activity_at"}):
                return previous
            if previous and all(previous.get(key) == task.get(key) for key in TASK_FIELDS):
                return previous
            return self._save_task(task, previous, actor, reason, now, substantive, occurred_at)

    def _check_transition(self, previous, task, actor, reviewed, expected_revision, reason, now):
        if actor not in {"user", "agent", "filter"}:
            raise ValueError("invalid actor")
        changed_status = previous is None or previous["status"] != task["status"]
        if previous and previous["explicit_excluded"] and actor != "user":
            if not task["explicit_excluded"] or task["status"] != "excluded":
                raise ValueError("explicit user exclusion must be removed by the user")
        if actor != "user" and task["status"] in ACTIVE and previous and previous["status"] in CLOSED:
            evidence = self._evidence(task["evidence_ids"])
            if not any(not event["excluded"] and event["id"] not in previous["evidence_ids"] for event in evidence):
                raise ValueError("reopening requires new, unexcluded evidence")
        if changed_status and task["status"] in CLOSED:
            if not reason.strip():
                raise ValueError("leaving tracking requires a reason")
            if actor == "filter":
                if task["status"] != "excluded":
                    raise ValueError("filters can only exclude tasks")
            elif actor != "user":
                if not reviewed or previous is None or expected_revision != previous["revision"]:
                    raise ValueError("independent review of the current revision is required")
                if task["status"] in {"completed", "transferred"} and not task["evidence_ids"]:
                    raise ValueError("completion and handoff require evidence")
                if task["status"] == "cancelled" and not self._idle(previous, now, 30):
                    raise ValueError("task is not eligible for idle cancellation")
            task["end_reason"] = reason
        if task["explicit_excluded"] and task["status"] != "excluded":
            raise ValueError("explicit exclusion requires excluded status")

    def transition_task(self, task_id, status, reason, evidence_ids=None, actor="agent",
                        reviewed=False, expected_revision=None, now=None, occurred_at=None):
        data = {"id": task_id, "status": status}
        if evidence_ids is not None:
            data["evidence_ids"] = list(dict.fromkeys(self.get_task(task_id)["evidence_ids"] + evidence_ids))
        if status == "excluded" and actor == "user":
            data["explicit_excluded"] = True
            data["excluded_by"] = "user"
        return self.upsert_task(data, actor, reason, substantive=False, reviewed=reviewed,
                                expected_revision=expected_revision, now=now, occurred_at=occurred_at)

    def list_tasks(self, include_closed=False, now=None):
        now = timestamp(now)
        sql = "SELECT data FROM tasks" + ("" if include_closed else " WHERE status IN ('active','waiting')")
        tasks = [json.loads(row[0]) for row in self.db.execute(sql)]
        def order(task):
            if task["status"] == "waiting":
                overdue = bool(task["waiting_until"] and task["waiting_until"] < now)
                return (1, 0 if overdue else 1, task["due_at"] or "9999", -task["priority"], task["id"])
            optional = task["kind"] == "optional"
            rank = (0 if task["due_at"] and task["due_at"] < now else
                    1 if task["due_at"] else 2 if task["blocks_others"] else 3)
            return (0 if task["status"] == "active" else 2, 4 if optional else rank,
                    task["due_at"] or "9999", -task["priority"], task["id"])
        return sorted(tasks, key=order)

    def history(self, task_id=None, since=None, until=None):
        clauses, params = [], []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        for boundary, operator in ((since, ">="), (until, "<")):
            if boundary:
                clauses.append("occurred_at " + operator + " ?")
                params.append(timestamp(boundary))
        sql = "SELECT data FROM history" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        return [json.loads(row[0]) for row in self.db.execute(sql + " ORDER BY occurred_at,rowid", params)]

    def idle_candidates(self, now=None, days=30):
        now = timestamp(now)
        if not isinstance(days, int) or days < 1:
            raise ValueError("idle days must be positive")
        return [task for task in self.list_tasks(now=now) if self._idle(task, now, days)]

    def reconcile_exclusions(self, excluded_event_ids=None, now=None):
        excluded = set(excluded_event_ids or [])
        now = timestamp(now)
        changed = []
        with self._write():
            self.db.execute("UPDATE events SET excluded=0")
            self.db.executemany("UPDATE events SET excluded=1 WHERE id=?", ((item,) for item in excluded))
            for task in self.list_tasks():
                evidence = self._evidence(task["evidence_ids"])
                if evidence and all(event["excluded"] for event in evidence):
                    previous = dict(task)
                    task.update(status="excluded", excluded_by="source", end_reason="all evidence sources excluded")
                    changed.append(self._save_task(task, previous, "filter", task["end_reason"], now, False))
        return changed

    def upsert_knowledge(self, data, actor="agent", now=None):
        data = {key: value for key, value in data.items() if key != "id" or value is not None}
        allowed = {"id", "topic", "project", "content", "stage", "scope", "version", "effective_at", "evidence_ids", "supersedes"}
        if set(data) - allowed:
            raise ValueError("unknown knowledge fields")
        now = timestamp(now)
        with self._write():
            row = self.db.execute("SELECT data FROM knowledge WHERE id=?", (data.get("id", ""),)).fetchone()
            previous = json.loads(row[0]) if row else None
            result = dict(previous) if previous else {"id": data.get("id") or _id(), "topic": "", "project": "",
                "content": "", "stage": "proposed", "scope": "", "version": "", "effective_at": None,
                "evidence_ids": [], "supersedes": None, "revision": 0, "created_at": now}
            result.update(data)
            _bounded_text(result["id"], "knowledge id", 200, False)
            for field in ("topic", "content"):
                _bounded_text(result[field], field, 16000 if field == "content" else 2000, False)
            for field in ("project", "scope", "version"):
                _bounded_text(result[field], field)
            if result["stage"] not in {"proposed", "decided", "implemented", "verified"}:
                raise ValueError("invalid knowledge stage")
            self._evidence(result["evidence_ids"])
            if result["effective_at"] is not None:
                result["effective_at"] = timestamp(result["effective_at"])
            if result["supersedes"]:
                if not self.db.execute("SELECT 1 FROM knowledge WHERE id=?", (result["supersedes"],)).fetchone():
                    raise ValueError("unknown superseded knowledge")
                if result["supersedes"] == result["id"]:
                    raise ValueError("knowledge cannot supersede itself")
            result["revision"] += 1
            result["updated_at"] = now
            self.db.execute("INSERT OR REPLACE INTO knowledge VALUES (?,?,?)", (result["id"], _json(result), now))
            version = {"id": _id(), "knowledge_id": result["id"], "actor": actor, "recorded_at": now,
                       "before": previous, "after": result, "evidence_ids": result["evidence_ids"]}
            self.db.execute("INSERT INTO knowledge_history VALUES (?,?,?,?)", (version["id"], result["id"], now, _json(version)))
        return result

    def list_knowledge(self, topic=None, project=None):
        values = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM knowledge ORDER BY updated_at,id")]
        return [item for item in values if (topic is None or topic.casefold() in item["topic"].casefold())
                and (project is None or project == item["project"])]

    def knowledge_history(self, knowledge_id):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM knowledge_history WHERE knowledge_id=? ORDER BY recorded_at,rowid", (knowledge_id,))]

    def upsert_question(self, data, now=None):
        data = {key: value for key, value in data.items() if key != "id" or value is not None}
        allowed = {"id", "text", "choices", "task_id", "priority", "evidence_ids", "rule_key", "scope",
                   "suggested_index", "status", "reason"}
        if set(data) - allowed:
            raise ValueError("unknown question fields")
        now = timestamp(now)
        with self._write():
            row = self.db.execute("SELECT data FROM questions WHERE id=?", (data.get("id", ""),)).fetchone()
            previous = json.loads(row[0]) if row else None
            question = dict(previous) if previous else {"id": data.get("id") or _id(), "text": "", "choices": [],
                "task_id": None, "priority": 50, "evidence_ids": [], "rule_key": "", "scope": "",
                "suggested_index": 0, "status": "open", "reason": "", "created_at": now}
            question.update(data)
            _bounded_text(question["id"], "question id", 200, False)
            _bounded_text(question["text"], "question text", 4000, False)
            if not isinstance(question["choices"], list) or not 2 <= len(question["choices"]) <= 10:
                raise ValueError("questions require 2 to 10 choices")
            for choice in question["choices"]:
                _bounded_text(choice, "choice", 2000, False)
            if question["status"] not in {"open", "resolved", "answered"}:
                raise ValueError("invalid question status")
            if type(question["suggested_index"]) is not int or not 0 <= question["suggested_index"] < len(question["choices"]):
                raise ValueError("invalid suggested choice")
            if type(question["priority"]) is not int or not 0 <= question["priority"] <= 100:
                raise ValueError("invalid question priority")
            if question["task_id"]:
                self.get_task(question["task_id"])
            self._evidence(question["evidence_ids"])
            question["updated_at"] = now
            question["reviewed_at"] = now
            self.db.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?)",
                            (question["id"], question["status"], now, _json(question)))
        return question

    def list_questions(self, include_closed=False):
        sql = "SELECT data FROM questions" + ("" if include_closed else " WHERE status='open'")
        return sorted((json.loads(row[0]) for row in self.db.execute(sql)),
                      key=lambda q: (-q["priority"], q["created_at"], q["id"]))

    def _get_question(self, question_id):
        row = self.db.execute("SELECT data FROM questions WHERE id=?", (question_id,)).fetchone()
        if not row:
            raise KeyError("unknown question: " + question_id)
        return json.loads(row[0])

    def answer_question(self, question_id, answer, apply_to_similar=False, scope=None, now=None):
        now = timestamp(now)
        with self._write():
            question = self._get_question(question_id)
            if question["status"] != "open":
                raise ValueError("question is no longer open")
            if type(answer) is int:
                if not 0 <= answer < len(question["choices"]):
                    raise ValueError("invalid answer choice")
                answer = question["choices"][answer]
            _bounded_text(answer, "answer", 4000, False)
            question.update(status="answered", answer=answer, answered_at=now,
                            apply_to_similar=bool(apply_to_similar), updated_at=now)
            self.db.execute("UPDATE questions SET status='answered',updated_at=?,data=? WHERE id=?",
                            (now, _json(question), question_id))
            if question["rule_key"]:
                rule_scope = scope if scope is not None else question["scope"]
                key = hashlib.sha256(_json([question["rule_key"], rule_scope]).encode()).hexdigest()
                row = self.db.execute("SELECT data FROM preferences WHERE id=?", (key,)).fetchone()
                preference = json.loads(row[0]) if row else {"id": key, "rule_key": question["rule_key"],
                    "scope": rule_scope, "answer": answer, "samples": 0, "active": False,
                    "evidence_question_ids": [], "created_at": now, "history": []}
                previous_answer = preference["answer"]
                preference["history"].append({"question_id": question_id, "answer": answer,
                    "explicit": bool(apply_to_similar), "at": now})
                if previous_answer == answer:
                    preference["samples"] += 1
                else:
                    preference["samples"] = 1
                    preference["active"] = False
                preference.update(answer=answer, updated_at=now)
                preference["evidence_question_ids"].append(question_id)
                preference["active"] = bool(apply_to_similar or preference["active"] or preference["samples"] >= 3)
                self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (key, _json(preference)))
        return question

    def resolve_question(self, question_id, reason, now=None):
        question = self._get_question(question_id)
        data = {key: value for key, value in question.items() if key in {
            "id", "text", "choices", "task_id", "priority", "evidence_ids", "rule_key", "scope", "suggested_index"}}
        data.update(status="resolved", reason=reason)
        return self.upsert_question(data, now=now)

    def preferences(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM preferences ORDER BY id")]

    def log(self, kind, detail, entity_id=None, now=None):
        now = timestamp(now)
        if isinstance(detail, dict):
            _bounded_text(_json(detail), "log detail", 4000)
        else:
            _bounded_text(detail, "log detail", 4000)
        record = {"id": _id(), "kind": _bounded_text(kind, "log kind", 100, False),
                  "detail": detail, "entity_id": entity_id, "recorded_at": now}
        with self._write():
            self.db.execute("INSERT INTO logs VALUES (?,?,?)", (record["id"], now, _json(record)))
        return record

    def logs(self, since=None, limit=200, until=None):
        sql = "SELECT data FROM logs"
        params = []
        clauses = []
        if since:
            clauses.append("recorded_at>=?")
            params.append(timestamp(since))
        if until:
            clauses.append("recorded_at<?")
            params.append(timestamp(until))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recorded_at DESC,rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [json.loads(row[0]) for row in self.db.execute(sql, params)]

    def _cleanup_rows(self, before, types):
        rows = {}
        for table in types:
            column = "updated_at" if table == "questions" else "recorded_at"
            where = column + "<?"
            if table == "questions":
                where += " AND status!='open'"
            rows[table] = [row[0] for row in self.db.execute("SELECT id FROM " + table + " WHERE " + where + " ORDER BY id", (before,))]
        if "questions" in types:
            referenced = {item for pref in self.preferences() for item in pref["evidence_question_ids"]}
            rows["questions"] = [item for item in rows["questions"] if item not in referenced]
        if "events" in types:
            referenced = set()
            for table in ("tasks", "history", "knowledge", "knowledge_history", "questions"):
                removing = set(rows.get(table, []))
                for row in self.db.execute("SELECT id,data FROM " + table):
                    if row[0] in removing:
                        continue
                    value = json.loads(row[1])
                    referenced.update(value.get("evidence_ids", []))
                    for snapshot in (value.get("before"), value.get("after")):
                        if snapshot:
                            referenced.update(snapshot.get("evidence_ids", []))
            rows["events"] = [item for item in rows["events"] if item not in referenced]
        return rows

    def cleanup(self, policy, dry_run=True, confirmation=None):
        if not isinstance(policy, dict) or set(policy) != {"before", "types"}:
            raise ValueError("cleanup requires before and types")
        before = timestamp(policy["before"])
        types = policy["types"]
        if not isinstance(types, list) or not types or set(types) - {"events", "history", "logs", "questions", "knowledge_history"}:
            raise ValueError("invalid cleanup types")
        types = sorted(set(types))
        with self._write():
            rows = self._cleanup_rows(before, types)
            token = hashlib.sha256(_json({"before": before, "rows": rows}).encode()).hexdigest()
            result = {"before": before, "counts": {table: len(ids) for table, ids in rows.items()},
                      "confirmation_token": token, "dry_run": bool(dry_run)}
            if dry_run:
                return result
            if not isinstance(confirmation, str) or confirmation != token:
                raise ValueError("cleanup requires the unchanged preview confirmation token")
            for table, ids in rows.items():
                self.db.executemany("DELETE FROM " + table + " WHERE id=?", ((item,) for item in ids))
            record = {"id": _id(), "kind": "cleanup", "detail": _json(result["counts"]),
                      "entity_id": None, "recorded_at": timestamp()}
            self.db.execute("INSERT INTO logs VALUES (?,?,?)", (record["id"], record["recorded_at"], _json(record)))
            return result
