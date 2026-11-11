"""Fixed local commands for people and CLI agents."""

import json
import os
from pathlib import Path
import sys

if __name__ == "__main__":
    from .install import bootstrap_runtime
    try:
        bootstrap_runtime()
    except (OSError, ValueError, KeyError):
        print(json.dumps({"error": "installed runtime could not be selected"}), file=sys.stderr)
        sys.exit(2)
    _early_home = os.environ.get("MONITOR_HOME") or str(Path(__file__).resolve().parent.parent / ".monitor")
    for _index, _argument in enumerate(sys.argv[1:], 1):
        if _argument.startswith("--home="):
            _early_home = _argument.split("=", 1)[1]
        elif _argument == "--home" and _index + 1 < len(sys.argv):
            _early_home = sys.argv[_index + 1]
    try:
        from .improve import recover_pending
        recover_pending(Path(__file__).resolve().parent.parent, Path(_early_home).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"error": "interrupted update requires manual recovery"}), file=sys.stderr)
        sys.exit(2)

import argparse
import copy
from datetime import datetime, timezone
import math
import platform
import shutil
import sqlite3
import time
import uuid

from .agents import AgentError, AgentRunner
from .privacy import is_private, set_private
from .config import data_path, initialize, load_config, save_config, utc_now, validate_config
from .filters import included, validate_filters
from . import install, sources
from .service import cycle_lock, reconcile_filters, run_cycle, tick, write_reports
from .store import Store, timestamp


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _payload(value):
    if value == "-":
        value = sys.stdin.read()
    elif value.startswith("@"):
        value = Path(value[1:]).expanduser().read_text(encoding="utf-8")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("JSON input must contain an object")
    return result


def _merge(target, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
    return target


def _json_input(parser):
    parser.add_argument("--json", required=True, help="JSON object, - for stdin, or @file")


def _actor_options(parser, review=False):
    parser.add_argument("--actor", choices=("user", "agent"), default="user")
    parser.add_argument("--expected-revision", type=int)
    if review:
        parser.add_argument("--reviewed", action="store_true", help="only after independent review")


def parser():
    root = Parser(prog="python3 -m monitorbot", description="Local work memory and task tracking")
    root.add_argument("--home", help="data directory; defaults to MONITOR_HOME or .monitor")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize without reading sources")
    init.add_argument("--lookback-days", type=int, default=None)
    add = commands.add_parser("add", help="save one thought immediately")
    add.add_argument("text")
    add.add_argument("--kind", choices=("optional", "candidate", "assigned"), default="optional")
    add.add_argument("--project", default="")
    add.add_argument("--due-at")
    commands.add_parser("ask", help="answer from indexed tasks, knowledge and source references").add_argument("text")
    _json_input(commands.add_parser("propose", help="submit an agent plan for service validation and independent review"))
    commands.add_parser("schema", help="print the fixed agent proposal JSON schema")
    task = commands.add_parser("task").add_subparsers(dest="action", required=True)
    task.add_parser("list").add_argument("--all", action="store_true")
    task.add_parser("get").add_argument("id")
    update = task.add_parser("update")
    update.add_argument("id")
    _json_input(update)
    _actor_options(update, review=True)
    transition = task.add_parser("transition")
    transition.add_argument("id")
    transition.add_argument("status", choices=("active", "waiting", "completed", "transferred", "cancelled", "excluded"))
    transition.add_argument("--reason", required=True)
    transition.add_argument("--evidence-id", action="append", default=None)
    _actor_options(transition, review=True)
    task.add_parser("history").add_argument("id", nargs="?")
    knowledge = commands.add_parser("knowledge").add_subparsers(dest="action", required=True)
    listing = knowledge.add_parser("list")
    listing.add_argument("--topic")
    listing.add_argument("--project")
    _json_input(knowledge.add_parser("upsert"))
    knowledge.add_parser("history").add_argument("id")
    question = commands.add_parser("question").add_subparsers(dest="action", required=True)
    question.add_parser("list").add_argument("--all", action="store_true")
    answer = question.add_parser("answer")
    answer.add_argument("id")
    choices = answer.add_mutually_exclusive_group(required=True)
    choices.add_argument("--choice", type=int, help="1-based choice number")
    choices.add_argument("--text")
    answer.add_argument("--always", action="store_true", help="apply to future matching cases")
    answer.add_argument("--scope")
    config = commands.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("show")
    _json_input(config.add_parser("set"))
    filters = commands.add_parser("filter").add_subparsers(dest="action", required=True)
    preview = filters.add_parser("preview")
    preview.add_argument("source")
    for name in ("add", "remove"):
        command = filters.add_parser(name)
        command.add_argument("source")
        command.add_argument("mode", choices=("include", "exclude"))
        if name == "add":
            _json_input(command)
        else:
            command.add_argument("index", type=int, help="1-based rule index")
    commands.add_parser("doctor")
    commands.add_parser("poll")
    run = commands.add_parser("run")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    commands.add_parser("tick")
    commands.add_parser("review")
    commands.add_parser("report")
    check = commands.add_parser("agent-check")
    check.add_argument("--backend", choices=("codex", "claude"), required=True)
    source = commands.add_parser("source").add_subparsers(dest="action", required=True)
    source.add_parser("read").add_argument("id")
    microsoft = source.add_parser("microsoft").add_subparsers(dest="login_action", required=True)
    microsoft.add_parser("login-start")
    microsoft.add_parser("login-poll")
    cleanup = commands.add_parser("cleanup").add_subparsers(dest="action", required=True)
    for name in ("preview", "apply"):
        command = cleanup.add_parser(name)
        command.add_argument("--before", required=True, help="timezone-aware ISO timestamp")
        command.add_argument("--types", nargs="+", required=True,
                             choices=("logs", "events", "questions", "history", "knowledge_history"))
        if name == "apply":
            command.add_argument("--confirmation", required=True)
    for name in ("deploy", "install", "start", "stop", "status"):
        control = commands.add_parser(name)
        if name in ("start", "stop"):
            control.add_argument("--login", action="store_true", help="also enable or remove this user's login startup")
    menu = commands.add_parser("menubar").add_subparsers(dest="action", required=True)
    for name in ("build", "install", "start", "stop", "status", "snapshot", "todo", "add"):
        control = menu.add_parser(name)
        if name == "stop":
            control.add_argument("--login", action="store_true", help="also remove menu bar login startup")
    return root


def _private_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            set_private(temporary)
            json.dump(value, stream)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _microsoft_login(config, action):
    path = Path(config["data_dir"]) / "microsoft-login.json"
    now = time.time()
    if action == "login-start":
        result = sources.start_microsoft_login(config)
        result["expires_at"] = now + int(result["expires_in"])
        result["next_poll_at"] = now + int(result.get("interval", 5))
        _private_json(path, result)
        return {key: value for key, value in result.items() if key != "device_code"}
    if not path.is_file():
        raise ValueError("Run source microsoft login-start first")
    if not is_private(path):
        raise ValueError("pending Microsoft sign-in file must have owner-only permissions")
    pending = json.loads(path.read_text(encoding="utf-8"))
    if now >= pending["expires_at"]:
        path.unlink()
        raise ValueError("Microsoft sign-in expired; run login-start again")
    if now < pending["next_poll_at"]:
        return {"authenticated": False, "status": "waiting_interval",
                "retry_after": math.ceil(pending["next_poll_at"] - now)}
    pending["next_poll_at"] = now + int(pending.get("interval", 5))
    _private_json(path, pending)
    try:
        result = sources.finish_microsoft_login(config, pending["device_code"])
    except sources.SourceError as exc:
        if "authorization_pending" in str(exc):
            return {"authenticated": False, "status": "authorization_pending",
                    "retry_after": int(pending.get("interval", 5))}
        if "slow_down" in str(exc):
            pending["interval"] = int(pending.get("interval", 5)) + 5
            pending["next_poll_at"] = now + pending["interval"]
            _private_json(path, pending)
            return {"authenticated": False, "status": "slow_down", "retry_after": pending["interval"]}
        raise
    path.unlink()
    return result


def _doctor(store, config):
    available = {}
    for backend in ("codex", "claude"):
        temporary = copy.deepcopy(config)
        temporary["agents"]["roles"]["doctor"] = {"backend": backend, "executable": backend}
        runner = AgentRunner(temporary, config["data_dir"])
        location = shutil.which(backend)
        verified = config["agents"].get("privacy_verified", {}).get(backend, {})
        current = runner.fingerprint("doctor") if location else None
        available[backend] = {"executable": location, "available": bool(location),
            "privacy_verified": bool(verified.get("privacy_verified") and verified.get("fingerprint") == current)}
    auth = Path(config["microsoft"]["token_file"]).expanduser()
    return {"platform": platform.system(), "python": platform.python_version(), "data_dir": config["data_dir"],
            "agents": available, "source_health": store.health(),
            "microsoft": {"client_id_configured": bool(config["microsoft"].get("client_id")),
                          "token_file_exists": auth.is_file(),
                          "token_file_private": is_private(auth) if auth.is_file() else None},
            "note": "Availability is not authentication or complete source coverage. No token contents were read."}


def _dispatch(args):
    home = data_path(args.home)
    if args.command == "deploy":
        return install.deploy(home)
    if args.command == "menubar" and args.action != "snapshot":
        from . import menubar
        if args.action in {"todo", "add"}:
            if sys.platform != "win32":
                raise RuntimeError("This notification icon action requires Windows")
            from . import windows_tray
            return {"todo": windows_tray.open_todo, "add": windows_tray.add_task}[args.action](home)
        if args.action == "stop":
            return menubar.stop(home, login=args.login)
        return {"build": menubar.build, "install": menubar.prepare,
                "start": menubar.start, "status": menubar.status}[args.action](home)
    if args.command == "schema":
        from .service import TRACK_SCHEMA
        return TRACK_SCHEMA
    if args.command == "init":
        config = initialize(home, args.lookback_days)
        with Store(home) as store:
            if not store.get_meta("started_at"):
                store.set_meta("started_at", utc_now())
            store.set_source_status("manual", "ok")
            return {"home": str(home), "lookback_days": config["lookback_days"],
                    "started_at": store.get_meta("started_at"), "initialized": True}
    config = load_config(home)
    if args.command == "tick":
        return tick(config)
    if args.command == "run" and args.loop:
        while True:
            try:
                tick(config)
            except RuntimeError as exc:
                if type(exc).__name__ != "ServiceBusy":
                    raise
            time.sleep(60)
            config = load_config(home)
    if args.command in {"install", "start", "stop", "status"}:
        if args.command in {"start", "stop"} and args.login:
            return {"start": install.start, "stop": install.stop}[args.command](home, login=True)
        return {"install": install.prepare, "start": install.start,
                "stop": install.stop, "status": install.status}[args.command](home)
    if args.command == "source" and args.action == "microsoft":
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        return _microsoft_login(config, args.login_action)
    with Store(home) as store:
        if args.command == "menubar" and args.action == "snapshot":
            return {"tasks": store.list_tasks(), "question_count": len(store.list_questions()),
                    "pending_events": store.count_events(unprocessed=True, excluded=False),
                    "source_health": store.health(), "generated_at": utc_now(),
                    "background": install.status(home), "last_poll": store.get_meta("last_poll")}
        if args.command == "propose":
            from .service import apply_proposal
            with cycle_lock(home):
                return apply_proposal(store, config, _payload(args.json))
        if args.command == "ask":
            from .service import answer_query
            return answer_query(store, config, args.text)
        if args.command == "add":
            if not args.text.strip() or len(args.text) > 2000:
                raise ValueError("task text must contain 1 to 2000 characters")
            if len(args.project) > 4000:
                raise ValueError("project name is too long")
            due = timestamp(args.due_at) if args.due_at is not None else None
            now = utc_now()
            if not store.get_meta("started_at"):
                store.set_meta("started_at", now)
            event = store.add_event({"source": "manual", "source_id": str(uuid.uuid4()),
                "occurred_at": now, "title": args.text, "metadata": {"kind": "manual"}})
            return store.upsert_task({"title": args.text, "kind": args.kind, "project": args.project,
                "due_at": due, "ownership": "self", "evidence_ids": [event["id"]]}, now=now,
                reason="manual entry")
        if args.command == "task":
            if args.action == "list":
                return store.list_tasks(include_closed=args.all)
            if args.action == "get":
                return store.get_task(args.id)
            if args.action == "history":
                return store.history(args.id)
            if args.action == "update":
                store.get_task(args.id)
                value = _payload(args.json)
                if value.get("id", args.id) != args.id:
                    raise ValueError("task ID cannot differ from command ID")
                value["id"] = args.id
                return store.upsert_task(value, actor=args.actor, reviewed=args.reviewed,
                                         expected_revision=args.expected_revision, reason="explicit CLI update")
            return store.transition_task(args.id, args.status, args.reason, args.evidence_id,
                actor=args.actor, reviewed=args.reviewed, expected_revision=args.expected_revision)
        if args.command == "knowledge":
            if args.action == "list":
                return store.list_knowledge(args.topic, args.project)
            if args.action == "history":
                return store.knowledge_history(args.id)
            return store.upsert_knowledge(_payload(args.json), actor="user")
        if args.command == "question":
            if args.action == "list":
                return store.list_questions(include_closed=args.all)
            answer = args.choice - 1 if args.choice is not None else args.text
            return store.answer_question(args.id, answer, apply_to_similar=args.always, scope=args.scope)
        if args.command == "config":
            if args.action == "set":
                patch = _payload(args.json)
                if "data_dir" in patch:
                    raise ValueError("use --home to select the data directory")
                config = _merge(config, patch)
                validate_config(config)
                save_config(home, config)
                if "sources" in patch:
                    reconcile_filters(store, config)
            return config
        if args.command == "filter":
            if args.source not in config["sources"]:
                raise ValueError("unknown source")
            settings = config["sources"][args.source]
            if args.action == "preview":
                all_events = store.events(limit=None)
                items = [event for event in all_events if event["source"] == args.source]
                excluded = [event["id"] for event in all_events
                            if not included(event, config["sources"].get(event["source"], {}))]
                affected = [task["id"] for task in store.list_tasks()
                            if task["evidence_ids"] and all(ref in excluded for ref in task["evidence_ids"])]
                return {"source": args.source, "settings": settings, "indexed_events": len(items),
                        "excluded_event_ids": excluded, "tasks_leaving_todo": affected}
            if args.action == "add":
                settings[args.mode].append(_payload(args.json))
            else:
                if not 1 <= args.index <= len(settings[args.mode]):
                    raise ValueError("filter index is out of range")
                settings[args.mode].pop(args.index - 1)
            validate_filters(settings)
            save_config(home, config)
            reconcile_filters(store, config)
            return {"source": args.source, "settings": settings, "applied": True}
        if args.command == "doctor":
            return _doctor(store, config)
        if args.command in {"poll", "run"}:
            with cycle_lock(home):
                return run_cycle(store, config)
        if args.command == "review":
            from .desktop import daily_review
            return daily_review(store, config)
        if args.command == "report":
            with cycle_lock(home):
                run_cycle(store, config)
                return write_reports(store, config)
        if args.command == "agent-check":
            selected = copy.deepcopy(config)
            default = config["agents"]["default"]
            executable = default.get("executable", args.backend) if default["backend"] == args.backend else args.backend
            selected["agents"]["roles"]["smoke"] = {"backend": args.backend, "executable": executable}
            result = AgentRunner(selected, home).smoke_test()
            config["agents"].setdefault("privacy_verified", {})[args.backend] = result
            save_config(home, config)
            if not result.get("privacy_verified"):
                raise AgentError("synthetic persistence audit did not pass; source content remains blocked")
            return result
        if args.command == "source" and args.action == "read":
            event = store.get_event(args.id)
            if event["excluded"] or not included(event, config["sources"].get(event["source"], {})):
                raise ValueError("source is excluded by current filters")
            return {"event_id": args.id, "content": sources.read_content(event, config)}
        if args.command == "cleanup":
            return store.cleanup({"before": args.before, "types": args.types}, dry_run=args.action == "preview",
                                 confirmation=getattr(args, "confirmation", None))
    raise ValueError("unknown command")


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = _dispatch(args)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, default=str))
        return 0
    except KeyboardInterrupt:
        return 130
    except AgentError:
        print(json.dumps({"error": {"type": "AgentError",
            "message": "Agent request failed; check doctor, CLI login, quota and persistence verification."}}), file=sys.stderr)
        return 2
    except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
