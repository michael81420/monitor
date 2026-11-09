"""Read-only, versioned Gerrit change snapshots."""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .sources import SourceError, Unconfigured, _allowed, _event


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError("Gerrit redirects are not followed; configure the final HTTPS URL")


class Gerrit:
    def __init__(self, config):
        self.settings = config.get("sources", {}).get("gerrit", {})
        url = self.settings.get("url", "")
        if not url:
            raise Unconfigured("Configure sources.gerrit.url and query")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise SourceError("Gerrit URL must be HTTPS without credentials, query or fragment")
        self.url = url.rstrip("/")
        self.headers = {"Accept": "application/json"}
        self.prefix = ""
        token_env = self.settings.get("token_env")
        username = self.settings.get("username", "")
        if token_env:
            token = os.environ.get(token_env, "")
            if not username or not token:
                raise Unconfigured("Gerrit username or token environment variable is missing")
            self.headers["Authorization"] = "Basic " + base64.b64encode(
                (username + ":" + token).encode()).decode("ascii")
            self.prefix = "/a"
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get(self, path, params=None):
        url = self.url + self.prefix + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(url, headers=self.headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise SourceError("Gerrit response exceeds the 16 MiB request limit")
            if raw.startswith(b")]}'\n"):
                raw = raw[5:]
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise SourceError("Gerrit HTTP " + str(exc.code)) from None
        except urllib.error.URLError:
            raise SourceError("Gerrit connection failed") from None
        except (ValueError, UnicodeError):
            raise SourceError("Gerrit returned invalid JSON") from None


def _stamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,9})?", value):
        raise SourceError("Gerrit returned an invalid timestamp")
    return datetime.fromisoformat(value[:26]).replace(tzinfo=timezone.utc)


def _content(client, number):
    detail = client.get("/changes/" + str(number) + "/detail", {"o": ["CURRENT_REVISION", "CURRENT_COMMIT", "MESSAGES"]})
    comments = client.get("/changes/" + str(number) + "/comments")
    if not isinstance(detail, dict) or not isinstance(comments, dict):
        raise SourceError("Gerrit returned invalid change content")
    if detail.get("_number") != number:
        raise SourceError("Gerrit returned a different change")
    _stamp(detail.get("updated"))
    revisions, messages = detail.get("revisions", {}), detail.get("messages", [])
    if (not isinstance(revisions, dict) or not isinstance(messages, list)
            or any(not isinstance(message, dict) or not isinstance(message.get("author", {}), dict) for message in messages)
            or any(not isinstance(items, list) or any(not isinstance(comment, dict)
                   or not isinstance(comment.get("author", {}), dict) for comment in items) for items in comments.values())):
        raise SourceError("Gerrit returned invalid review discussion")
    current = detail.get("current_revision")
    if current is not None and not isinstance(current, str):
        raise SourceError("Gerrit returned invalid revision content")
    revision = revisions.get(current, {})
    if not isinstance(revision, dict) or not isinstance(revision.get("commit", {}), dict):
        raise SourceError("Gerrit returned invalid revision content")
    lines = [str(detail.get("subject", "")), "Status: " + str(detail.get("status", "")),
             str(revision.get("commit", {}).get("message", ""))]
    for message in detail.get("messages", []):
        lines.append("{} {}: {}".format(message.get("date", ""),
                     message.get("author", {}).get("name", "Unknown"), message.get("message", "")))
    for filename, items in comments.items():
        for comment in items:
            lines.append("{}:{} {} {}: {}".format(filename, comment.get("line", ""),
                         comment.get("updated", ""), comment.get("author", {}).get("name", "Unknown"),
                         comment.get("message", "")))
    return detail, "\n".join(lines)


def collect_changes(config, cutoff, now):
    client = Gerrit(config)
    query = client.settings.get("query")
    if not isinstance(query, str) or not query.strip():
        raise Unconfigured("Configure sources.gerrit.query to select your changes or projects")
    query = '({}) after:"{}" before:"{}"'.format(
        query, cutoff.strftime("%Y-%m-%d %H:%M:%S"),
        (now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"))
    events, offset = [], 0
    # shortcut: changes are current snapshots, use an event stream if every intermediate update is required.
    for _ in range(1000):
        page = client.get("/changes/", {"q": query, "n": 100, "S": offset})
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise SourceError("Gerrit returned an invalid change list")
        for item in page:
            number = item.get("_number")
            if type(number) is not int or number < 1:
                raise SourceError("Gerrit returned an invalid change number")
            stamp = _stamp(item.get("updated"))
            if not cutoff <= stamp <= now:
                continue
            metadata = {"server": client.url, "change_number": number, "project": item.get("project", ""),
                        "branch": item.get("branch", ""), "status": item.get("status", ""),
                        "version": item["updated"], "content_available": True, "coverage": "change_snapshot"}
            event = _event("gerrit", client.url + ":" + str(number) + ":" + item["updated"], stamp,
                           item.get("subject", "Gerrit change"), client.url + "/c/" + str(number), metadata)
            if _allowed(event, config):
                detail, content = _content(client, number)
                if detail.get("updated") != item["updated"]:
                    raise SourceError("Gerrit change updated during collection; retry required")
                event["content"] = content
                events.append(event)
        if not page or not page[-1].get("_more_changes"):
            return events, "Current change descriptions, review messages and published inline comments; no drafts or diffs"
        offset += len(page)
    raise SourceError("Gerrit pagination limit reached; narrow the query or lookback window")


def read_change(event, config):
    client = Gerrit(config)
    metadata = event.get("metadata", {})
    number = metadata.get("change_number")
    if metadata.get("server") != client.url or type(number) is not int or number < 1:
        raise SourceError("Gerrit reference does not match the configured server")
    detail, content = _content(client, number)
    if detail.get("updated") != metadata.get("version"):
        content = "[Source change updated since this event; this is its current content]\n" + content
    return content
