"""Read-only activity adapters. Source bodies are returned in memory, never cached."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List
from xml.etree import ElementTree as ET

from .filters import included
from .privacy import is_private, set_private


SOURCES = ("chrome", "word", "excel", "powerpoint", "codex", "claude", "chatgpt", "outlook", "teams", "gerrit")
GRAPH = "https://graph.microsoft.com/v1.0"
OFFICE = {"word": ".docx", "excel": ".xlsx", "powerpoint": ".pptx"}
SCOPES = ["offline_access", "User.Read", "Mail.Read", "Chat.Read", "Team.ReadBasic.All",
          "Channel.ReadBasic.All", "ChannelMessage.Read.All"]


class SourceError(RuntimeError):
    pass


class Unconfigured(SourceError):
    pass


@dataclass
class SourceBatch:
    source: str
    events: List[dict]
    status: str
    detail: str
    cursor: Any = None


def _date(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(value, timezone.utc)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Source timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def _iso(value) -> str:
    return _date(value).isoformat()


def _settings(config, source):
    return config.get("sources", {}).get(source, {})


def _path(value) -> Path:
    return Path(value).expanduser().resolve()


def _event(source, source_id, occurred_at, title, url, metadata, content=None):
    result = dict(source=source, source_id=str(source_id), occurred_at=_iso(occurred_at),
                  title=str(title), url=str(url), metadata=metadata)
    if content is not None:
        result["content"] = content
    return result


def _allowed(event, config) -> bool:
    """Apply the same metadata filters before reading message bodies."""
    return included(event, _settings(config, event["source"]))


def collect(config, cursors, since, now) -> List[SourceBatch]:
    """Return source health and incremental indexes; failed sources keep their cursor."""
    since, now = _date(since), _date(now)
    result = []
    for source in SOURCES:
        previous = cursors.get(source)
        settings = _settings(config, source)
        if settings.get("enabled", True) is False:
            result.append(SourceBatch(source, [], "excluded", "Disabled by source settings", previous))
            continue
        try:
            complete = True
            # shortcut: two-minute overlap covers short publication delays; use provider delta cursors for longer delays.
            cutoff = max(since, _date(previous) - timedelta(minutes=2)) if isinstance(previous, str) else since
            if source == "chrome":
                events, detail, complete = _chrome(config, cutoff, now)
            elif source in OFFICE:
                events, detail = _office(source, config, cutoff, now)
            elif source in ("codex", "claude"):
                events, detail = _sessions(source, config, cutoff, now)
            elif source == "chatgpt":
                raise Unconfigured("ChatGPT conversation access is not connected; browser visits do not include conversation content")
            elif source == "gerrit":
                from .gerrit import collect_changes
                events, detail = collect_changes(config, cutoff, now)
            else:
                events, detail = _microsoft(source, config, cutoff, now)
            events = [e for e in events if _allowed(e, config)]
            oversized = False
            for event in events:
                if len(event.get("content", "")) > int(config.get("content_max_chars", 100000)):
                    event.pop("content")
                    event["metadata"].update(content_available=False, coverage="partial_content_exceeds_limit")
                    oversized = True
            if oversized or not complete:
                message = "Content exceeds the read limit; indexes retained but full analysis is pending" if oversized else detail
                result.append(SourceBatch(source, events, "partial", message, previous))
            else:
                result.append(SourceBatch(source, events, "ok", detail, _iso(now)))
        except Unconfigured as exc:
            result.append(SourceBatch(source, [], "unconfigured", str(exc), previous))
        except (OSError, ValueError, sqlite3.Error, SourceError, subprocess.SubprocessError,
                zipfile.BadZipFile, ET.ParseError) as exc:
            # Exception text can contain a server response; persist only a controlled diagnostic.
            detail = str(exc) if isinstance(exc, SourceError) else type(exc).__name__ + ": source could not be read"
            result.append(SourceBatch(source, [], "error", detail, previous))
    return result


def _chrome(config, cutoff, now):
    settings = _settings(config, "chrome")
    configured = settings.get("profiles", [])
    if configured:
        profiles = [_path(p) for p in configured]
    else:
        base = Path.home() / "Library/Application Support/Google/Chrome"
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"
        profiles = [p for p in base.glob("*") if p.name == "Default" or p.name.startswith("Profile ")]
    profiles = sorted(p for p in profiles if (p / "History").is_file())
    if not profiles:
        raise Unconfigured("No readable Chrome profile History database; configure sources.chrome.profiles")
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    low, high = (int((_date(t) - epoch).total_seconds() * 1000000) for t in (cutoff, now))
    events = []
    failed = []
    for profile in profiles:
        uri = (profile / "History").as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                rows = db.execute("SELECT v.id,v.visit_time,u.url,u.title FROM visits v JOIN urls u ON u.id=v.url "
                                  "WHERE v.visit_time>=? AND v.visit_time<=? ORDER BY v.visit_time,v.id", (low, high))
                for visit_id, visited, url, title in rows:
                    parsed = urllib.parse.urlsplit(url)
                    if parsed.scheme not in ("http", "https", "file"):
                        continue
                    url = _safe_url(url)
                    metadata = dict(profile=str(profile), visit_id=visit_id, domain=parsed.hostname or "",
                                    content_available=False, coverage="history_metadata")
                    events.append(_event("chrome", str(profile) + ":" + str(visit_id),
                                         epoch + timedelta(microseconds=visited), title or parsed.hostname or "Web page",
                                         url, metadata))
        except sqlite3.Error as exc:
            failed.append(profile.name + ": " + type(exc).__name__)
    detail = "Read-only Chrome visit metadata; page contents and incognito activity are not captured"
    if failed:
        detail += "; profiles unavailable: " + ", ".join(failed)
    return events, detail, not failed


def _safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    sensitive = {"access_token", "id_token", "refresh_token", "code", "token", "password", "key", "api_key"}
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in sensitive]
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += ":" + str(parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, urllib.parse.urlencode(query), ""))


def _office(source, config, cutoff, now):
    suffix = OFFICE[source]
    settings = _settings(config, source)
    watched = settings.get("paths", [])
    paths = set()
    for item in watched:
        path = _path(item)
        if not path.exists():
            raise SourceError("Configured Office path does not exist")
        if path.is_file():
            paths.add(path)
        else:
            paths.update(p.resolve() for p in path.rglob("*" + suffix) if not p.name.startswith("~$"))
    recently_used = {}
    if sys.platform == "darwin" and settings.get("recent", True):
        query = "kMDItemFSName == '*%s'cd && kMDItemLastUsedDate >= $time.iso(%s)" % (
            suffix, cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
        found = subprocess.run(["/usr/bin/mdfind", query], capture_output=True, text=True, timeout=20, check=True)
        for name in found.stdout.splitlines():
            p = Path(name)
            if p.is_file() and p.suffix.lower() == suffix:
                stamp = subprocess.run(["/usr/bin/mdls", "-raw", "-name", "kMDItemLastUsedDate", str(p)],
                                       capture_output=True, text=True, timeout=5, check=True).stdout.strip()
                try:
                    recently_used[p.resolve()] = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z")
                except ValueError:
                    continue
                paths.add(p.resolve())
    if not paths and not watched:
        raise Unconfigured("No recently used Office documents detected; configure sources.%s.paths for explicit observation" % source)
    events = []
    for path in sorted(paths):
        if path.suffix.lower() != suffix or path.name.startswith("~$"):
            continue
        stat = path.stat()
        used = recently_used.get(path)
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        stamp = max(modified, used) if used else modified
        if cutoff <= stamp <= now:
            version = str(stat.st_mtime_ns)
            metadata = dict(path=str(path), version=version, content_available=True,
                            coverage="explicit_paths" if watched else "spotlight_last_used")
            events.append(_event(source, str(path) + ":" + version + ":" + _iso(stamp), stamp,
                                 path.name, path.as_uri(), metadata))
    return events, "Office activity from configured paths or macOS last-used metadata; unsaved edits are not captured"


def office_text(path, max_chars=100000):
    """Read Office Open XML without modifying the document or creating a copy."""
    path = _path(path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if path.suffix.lower() == ".docx":
            selected = [n for n in names if n == "word/document.xml" or re.match(r"word/(header|footer)\d+\.xml$", n)]
        elif path.suffix.lower() == ".pptx":
            selected = sorted((n for n in names if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)),
                              key=lambda n: ("notesSlides" in n, int(re.search(r"(\d+)\.xml$", n).group(1))))
        elif path.suffix.lower() == ".xlsx":
            selected = [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
        else:
            raise SourceError("Only .docx, .pptx and .xlsx content extraction is supported")
        if not selected:
            raise SourceError("Office document is missing its expected content XML")
        total = sum(archive.getinfo(n).file_size for n in selected)
        if total > 32 * 1024 * 1024:
            raise SourceError("Office XML exceeds the 32 MiB extraction limit")
        shared = []
        if path.suffix.lower() == ".xlsx" and "xl/sharedStrings.xml" in names:
            info = archive.getinfo("xl/sharedStrings.xml")
            if info.file_size > 32 * 1024 * 1024:
                raise SourceError("Excel shared strings exceed the extraction limit")
            shared = ["".join(el.itertext()) for el in ET.fromstring(archive.read(info))]
        chunks = []
        used = 0
        for name in selected:
            root = ET.fromstring(archive.read(name))
            if path.suffix.lower() == ".xlsx":
                lines = ["[" + name + "]"]
                for cell in root.iter():
                    if cell.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    values = {el.tag.rsplit("}", 1)[-1]: el for el in cell}
                    value = values.get("v")
                    text = value.text or "" if value is not None else ""
                    if cell.get("t") == "s" and text:
                        index = int(text)
                        text = shared[index] if 0 <= index < len(shared) else "[missing shared string]"
                    elif cell.get("t") == "inlineStr":
                        text = "".join(el.text or "" for el in cell.iter() if el.tag.rsplit("}", 1)[-1] == "t")
                    formula = values.get("f")
                    if formula is not None:
                        text = "=" + (formula.text or "") + " [cached: " + text + "]"
                    lines.append(cell.get("r", "?") + ": " + text)
                chunk = "\n".join(lines)
            else:
                chunk = "[" + name + "]\n" + "\n".join(el.text or "" for el in root.iter()
                                                         if el.tag.rsplit("}", 1)[-1] == "t")
            used += len(chunk) + (1 if chunks else 0)
            if used > int(max_chars):
                raise SourceError("Office content exceeds the read limit; full analysis is pending")
            chunks.append(chunk)
        return "\n".join(chunks)


def _session_message(source, item):
    if not isinstance(item, dict):
        return None
    if source == "codex":
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            return None
        if item.get("type") != "response_item" or payload.get("type") != "message":
            return None
        role = payload.get("role")
        parts = payload.get("content", [])
    else:
        if item.get("type") not in ("user", "assistant"):
            return None
        message = item.get("message", {})
        if not isinstance(message, dict):
            return None
        role = message.get("role", item.get("type"))
        parts = message.get("content", [])
    if role not in ("user", "assistant"):
        return None
    if isinstance(parts, str):
        text = parts
    elif isinstance(parts, list):
        text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)
                         and p.get("type") in ("text", "input_text", "output_text"))
    else:
        return None
    return (role, text) if text.strip() else None


def _session_roots(source, config):
    default = "~/.codex/sessions" if source == "codex" else "~/.claude/projects"
    return [_path(p) for p in _settings(config, source).get("roots", [default])]


def _sessions(source, config, cutoff, now):
    roots = _session_roots(source, config)
    if not any(p.is_dir() for p in roots):
        raise Unconfigured("No readable %s session directory; configure sources.%s.roots" % (source, source))
    events = []
    for root in roots:
        for path in sorted(root.rglob("*.jsonl")):
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                continue
            with path.open(encoding="utf-8") as stream:
                for line, raw in enumerate(stream, 1):
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        if raw.strip() and not raw.endswith("\n"):
                            raise SourceError("Conversation last record is incomplete; cursor retained for retry")
                        continue
                    try:
                        message = _session_message(source, item)
                        if message is None:
                            continue
                        stamp = _date(item.get("timestamp"))
                    except (ValueError, TypeError):
                        continue
                    if not (cutoff <= stamp <= now):
                        continue
                    role, content = message
                    session = item.get("sessionId", path.stem)
                    digest = hashlib.sha256(raw.rstrip("\r\n").encode()).hexdigest()[:16]
                    metadata = dict(path=str(path), line=line, session_id=session, role=role,
                                    version=digest, content_available=True, coverage="existing_cli_transcripts")
                    events.append(_event(source, str(path) + ":" + str(line) + ":" + digest, stamp,
                                         source + " " + role + " message " + str(session), path.as_uri(),
                                         metadata, content))
    return events, "Existing CLI conversation messages only; tool output and ephemeral sessions are not included"


def _request_json(url, data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise SourceError("Provider response exceeds the 16 MiB request limit")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # OAuth errors are codes, not raw server response strings.
        try:
            code = json.loads(exc.read(1024 * 1024)).get("error", "http_error")
            code = code.get("code", "http_error") if isinstance(code, dict) else code
            code = re.sub(r"[^A-Za-z0-9_.-]", "", str(code))[:80]
        except (ValueError, AttributeError):
            code = "http_error"
        raise SourceError("Provider HTTP %s (%s)" % (exc.code, code)) from None
    except urllib.error.URLError:
        raise SourceError("Provider connection failed") from None


def _microsoft_settings(config):
    settings = config.get("microsoft", {})
    if not settings.get("client_id"):
        raise Unconfigured("Microsoft source requires a registered public-client app client_id and delegated sign-in")
    return settings


def _token_path(config):
    settings = _microsoft_settings(config)
    default = Path(config.get("data_dir", ".monitor")) / "microsoft-auth.json"
    return _path(settings.get("token_file", default))


def _oauth(config, endpoint, fields):
    settings = _microsoft_settings(config)
    tenant = str(settings.get("tenant", "organizations"))
    if not re.fullmatch(r"[A-Za-z0-9.-]+", tenant):
        raise ValueError("Invalid Microsoft tenant")
    fields = dict(fields, client_id=settings["client_id"])
    url = "https://login.microsoftonline.com/" + tenant + "/oauth2/v2.0/" + endpoint
    return _request_json(url, urllib.parse.urlencode(fields).encode(),
                         {"Content-Type": "application/x-www-form-urlencoded"})


def _save_token(config, response, previous=None):
    path = _token_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = previous or {}
    payload = {key: response.get(key, previous.get(key)) for key in ("access_token", "refresh_token", "scope", "token_type")}
    payload["expires_at"] = time.time() + int(response.get("expires_in", 3600))
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            set_private(temporary)
            json.dump(payload, stream)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def start_microsoft_login(config):
    """Call only in response to an explicit login command; return device-code instructions."""
    settings = _microsoft_settings(config)
    return _oauth(config, "devicecode", {"scope": " ".join(settings.get("scopes", SCOPES))})


def finish_microsoft_login(config, device_code):
    """Perform one poll. The caller respects the device response's interval and expiry."""
    response = _oauth(config, "token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                        "device_code": device_code})
    _save_token(config, response)
    return {"authenticated": True, "scope": response.get("scope", ""), "expires_in": response.get("expires_in")}


def _access_token(config):
    path = _token_path(config)
    if not path.is_file():
        raise Unconfigured("Microsoft delegated sign-in is not complete; run the Microsoft login command")
    if not is_private(path):
        raise SourceError("Microsoft token file must have owner-only permissions")
    with path.open(encoding="utf-8") as stream:
        token = json.load(stream)
    if float(token.get("expires_at", 0)) <= time.time() + 120:
        if not token.get("refresh_token"):
            raise Unconfigured("Microsoft sign-in expired; run the Microsoft login command again")
        response = _oauth(config, "token", {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
        _save_token(config, response, token)
        token.update(response)
    if not token.get("access_token"):
        raise Unconfigured("Microsoft token file has no access token")
    return token["access_token"]


class _Graph:
    def __init__(self, config):
        self.config = config
        self.token = _access_token(config)
        self.max_pages = int(config.get("microsoft", {}).get("max_pages", 100))

    def get(self, path, params=None):
        url = path if path.startswith("https://") else GRAPH + path
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com" or parsed.port not in (None, 443):
            raise SourceError("Refused a non-Microsoft Graph pagination URL")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return _request_json(url, headers={"Authorization": "Bearer " + self.token,
                                          "Prefer": 'IdType="ImmutableId"'})

    def pages(self, path, params=None):
        count = 0
        while path:
            if count >= self.max_pages:
                raise SourceError("Microsoft pagination limit reached; increase microsoft.max_pages to complete coverage")
            data = self.get(path, params)
            values = data.get("value")
            if not isinstance(values, list):
                raise SourceError("Microsoft collection response has no value list")
            yield from values
            path, params = data.get("@odata.nextLink"), None
            count += 1


def _q(value):
    return urllib.parse.quote(str(value), safe="")


def _mail_event(item, now, account=""):
    stamp = item.get("lastModifiedDateTime") or item.get("receivedDateTime") or item.get("sentDateTime")
    if not stamp:
        raise SourceError("Mail message is missing its timestamp")
    sender = item.get("from", {}).get("emailAddress", {}).get("address", "")
    recipients = [r.get("emailAddress", {}).get("address", "") for r in item.get("toRecipients", [])]
    path = "/me/messages/" + _q(item["id"])
    meta = dict(account=account, message_id=item["id"], folder_id=item.get("parentFolderId", ""), sender=sender,
                recipients=recipients, conversation_id=item.get("conversationId", ""), graph_path=path,
                version=stamp, content_available=True, coverage="delegated_mailbox")
    return _event("outlook", item["id"] + ":" + stamp, stamp, item.get("subject", "") or "Mail message",
                  item.get("webLink", ""), meta)


def _chat_event(item, location, metadata):
    stamp = item.get("lastModifiedDateTime") or item.get("createdDateTime")
    if not stamp:
        raise SourceError("Teams message is missing its timestamp")
    sender = item.get("from") or {}
    user = sender.get("user") or sender.get("application") or {}
    meta = dict(metadata, message_id=item["id"], sender=user.get("displayName", user.get("id", "")),
                graph_path=location, version=stamp, content_available=True, coverage="delegated_teams")
    # Do not use message body text as an index title.
    title = item.get("subject") or ("Teams message by " + (user.get("displayName") or "unknown sender"))
    event = _event("teams", location + ":" + stamp, stamp, title, item.get("webUrl", ""), meta)
    event["content"] = _plain_html((item.get("body") or {}).get("content", ""))
    if item.get("deletedDateTime"):
        event["content"] = "[Source message deleted at " + item["deletedDateTime"] + "]"
    return event


def _microsoft(source, config, cutoff, now):
    graph = _Graph(config)
    identity = graph.get("/me", {"$select": "id,userPrincipalName,mail"})
    account = identity.get("userPrincipalName") or identity.get("mail") or identity.get("id", "")
    events = []
    if source == "outlook":
        fields = "id,subject,webLink,lastModifiedDateTime,receivedDateTime,sentDateTime,parentFolderId,from,toRecipients,conversationId"
        params = {"$filter": "lastModifiedDateTime ge " + cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "$select": fields, "$top": 100}
        for item in graph.pages("/me/messages", params):
            event = _mail_event(item, now, account)
            if cutoff <= _date(event["occurred_at"]) <= now:
                events.append(event)
        return events, "Delegated mailbox metadata, including sent messages; bodies fetched only when requested"
    settings = config.get("microsoft", {})
    chats = settings.get("chat_ids")
    if chats is None:
        chats = [c["id"] for c in graph.pages("/me/chats", {"$top": 50})]
    params = {"$filter": "lastModifiedDateTime gt " + (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "$orderby": "lastModifiedDateTime desc", "$top": 50}
    for chat_id in chats:
        path = "/chats/" + _q(chat_id) + "/messages"
        for item in graph.pages(path, params):
            event = _chat_event(item, path + "/" + _q(item["id"]), dict(account=account, chat_id=chat_id))
            if cutoff <= _date(event["occurred_at"]) <= now:
                events.append(event)
    teams = settings.get("team_ids")
    if teams is None:
        teams = [t["id"] for t in graph.pages("/me/joinedTeams")]
    for team_id in teams:
        base = "/teams/" + _q(team_id) + "/channels"
        channels = settings.get("channel_ids")
        if channels is None:
            channels = [c["id"] for c in graph.pages(base)]
        for channel_id in channels:
            path = base + "/" + _q(channel_id) + "/messages"
            for item in graph.pages(path, {"$top": 50, "$expand": "replies"}):
                metadata = dict(account=account, team_id=team_id, channel_id=channel_id)
                event = _chat_event(item, path + "/" + _q(item["id"]), metadata)
                if cutoff <= _date(event["occurred_at"]) <= now:
                    events.append(event)
                replies = list(item.get("replies", []))
                next_page = item.get("replies@odata.nextLink")
                if next_page:
                    replies.extend(graph.pages(next_page))
                for reply in replies:
                    location = path + "/" + _q(item["id"]) + "/replies/" + _q(reply["id"])
                    event = _chat_event(reply, location, dict(metadata, reply_to_id=item["id"]))
                    if cutoff <= _date(event["occurred_at"]) <= now:
                        events.append(event)
    return events, "Delegated chats and joined-team channels, including replies; pagination must complete before cursor advances"


def _plain_html(text):
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.I | re.S)
    text = re.sub(r"<(br|/p|/div|/li)\b[^>]*>", "\n", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


def _bounded(text, max_chars):
    if len(text) > max_chars:
        raise SourceError("Source content exceeds the read limit; full analysis is pending")
    return text


def read_content(event, config) -> str:
    """Re-read a referenced source. Never persist its body or a raw provider response."""
    if not _allowed(event, config):
        raise SourceError("Source is excluded by current filters")
    source, metadata = event["source"], event.get("metadata", {})
    max_chars = int(config.get("content_max_chars", 100000))
    if source == "gerrit":
        from .gerrit import read_change
        return _bounded(read_change(event, config), max_chars)
    if source in OFFICE:
        path = _path(metadata["path"])
        current = str(path.stat().st_mtime_ns)
        content = office_text(path, max_chars)
        if current != str(metadata.get("version", current)):
            content = "[Source document changed since this event; this is its current content]\n" + content
        return content
    if source in ("codex", "claude"):
        path, requested = _path(metadata["path"]), int(metadata["line"])
        roots = _session_roots(source, config)
        if not any(path == root or root in path.parents for root in roots):
            raise SourceError("Conversation reference is outside configured roots")
        with path.open(encoding="utf-8") as stream:
            for line, raw in enumerate(stream, 1):
                if line == requested:
                    digest = hashlib.sha256(raw.rstrip("\r\n").encode()).hexdigest()[:16]
                    if metadata.get("version") and metadata["version"] != digest:
                        raise SourceError("Conversation reference changed; original line cannot be verified")
                    message = _session_message(source, json.loads(raw))
                    if message:
                        return _bounded(message[1], max_chars)
                    break
        raise SourceError("Referenced conversation message is no longer available")
    if source in ("outlook", "teams"):
        graph_path = metadata.get("graph_path", "")
        expected = "/me/messages/" if source == "outlook" else ("/chats/", "/teams/")
        if not graph_path.startswith(expected) or "?" in graph_path or "#" in graph_path:
            raise SourceError("Invalid Microsoft source reference")
        graph = _Graph(config)
        item = graph.get(graph_path, {"$select": "body,lastModifiedDateTime"} if source == "outlook" else None)
        content = _plain_html((item.get("body") or {}).get("content", ""))
        if item.get("lastModifiedDateTime") != metadata.get("version"):
            content = "[Source message changed since this event; this is its current content]\n" + content
        return _bounded(content, max_chars)
    if source == "chrome":
        raise SourceError("Chrome history has no page body; use an explicitly authorized browser reader for content")
    raise SourceError("No content reader is connected for this source")
