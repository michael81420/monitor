import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from monitorbot import sources


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        self.since = self.now - timedelta(hours=1)

    def config(self, enabled):
        return {"data_dir": str(self.root), "sources": {
            name: {"enabled": name in enabled} for name in sources.SOURCES}}

    def test_chrome_reads_incremental_history_without_creating_copy(self):
        profile = self.root / "Default"
        profile.mkdir()
        history = profile / "History"
        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        with sqlite3.connect(history) as db:
            db.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT);"
                             "CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER);")
            db.execute("INSERT INTO urls VALUES(1,?,?)", ("https://example.com/work?code=secret&q=HDR#token", "HDR"))
            for ident, stamp in enumerate([self.since - timedelta(days=1), self.now - timedelta(minutes=3)], 1):
                db.execute("INSERT INTO visits VALUES(?,?,?)", (ident, 1, int((stamp-epoch).total_seconds()*1000000)))
        config = self.config(["chrome"])
        config["sources"]["chrome"]["profiles"] = [str(profile)]
        before = set(profile.iterdir())
        batch = sources.collect(config, {}, self.since, self.now)[0]
        self.assertEqual("ok", batch.status)
        self.assertEqual(1, len(batch.events))
        event = batch.events[0]
        self.assertEqual("https://example.com/work?q=HDR", event["url"])
        self.assertFalse(event["metadata"]["content_available"])
        self.assertNotIn("content", event)
        self.assertEqual(before, set(profile.iterdir()))
        again = sources.collect(config, {"chrome": batch.cursor}, self.since, self.now + timedelta(minutes=1))[0]
        self.assertEqual([], again.events)
        with self.assertRaises(sources.SourceError):
            sources.read_content(event, config)

    def test_chrome_filters_are_applied_to_metadata(self):
        config = self.config(["chrome"])
        config["sources"]["chrome"]["exclude"] = [{"domain": "private.example"}]
        event = sources._event("chrome", "1", self.now, "title", "https://private.example", {"domain": "private.example"})
        with patch.object(sources, "_chrome", return_value=([event], "test", True)):
            batch = sources.collect(config, {}, self.since, self.now)[0]
        self.assertEqual([], batch.events)

    def test_locked_chrome_profile_does_not_discard_other_profile_and_can_catch_up(self):
        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        stamp = int((self.now - timedelta(minutes=3) - epoch).total_seconds() * 1000000)
        profiles = [self.root / "Default", self.root / "Profile 1"]
        for profile in profiles:
            profile.mkdir()
            with sqlite3.connect(profile / "History") as db:
                db.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT);"
                                 "CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER);")
                db.execute("INSERT INTO urls VALUES(1,?,?)", ("https://example.invalid/synthetic", "Synthetic visit"))
                db.execute("INSERT INTO visits VALUES(1,1,?)", (stamp,))
        lock = sqlite3.connect(profiles[1] / "History")
        try:
            lock.execute("BEGIN EXCLUSIVE")
            config = self.config(["chrome"])
            config["sources"]["chrome"]["profiles"] = [str(profile) for profile in profiles]
            previous = self.since.isoformat()
            partial = sources.collect(config, {"chrome": previous}, self.since, self.now)[0]
            self.assertEqual("partial", partial.status)
            self.assertEqual(previous, partial.cursor)
            self.assertEqual(1, len(partial.events))
            lock.rollback()
            complete = sources.collect(config, {"chrome": previous}, self.since, self.now)[0]
            self.assertEqual("ok", complete.status)
            self.assertEqual(2, len(complete.events))
            self.assertIn(partial.events[0]["source_id"], {event["source_id"] for event in complete.events})
        finally:
            lock.close()

    def make_office(self, extension, files):
        path = self.root / ("document" + extension)
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        os.utime(path, (self.now.timestamp()-60, self.now.timestamp()-60))
        return path

    def test_office_documents_are_read_without_extracting_files(self):
        path = self.make_office(".docx", {"word/document.xml": '<document xmlns:w="urn:w"><w:p><w:t>HDR method</w:t></w:p></document>'})
        config = self.config(["word"])
        config["sources"]["word"].update(paths=[str(path)], recent=False)
        batch = sources.collect(config, {}, self.since, self.now)[1]
        self.assertEqual("ok", batch.status)
        self.assertEqual(1, len(batch.events))
        event = batch.events[0]
        self.assertNotIn("content", event)
        before = set(self.root.iterdir())
        self.assertIn("HDR method", sources.read_content(event, config))
        self.assertEqual(before, set(self.root.iterdir()))
        os.utime(path, (self.now.timestamp(), self.now.timestamp()))
        self.assertIn("changed since this event", sources.read_content(event, config))

    def test_excel_shared_strings_inline_text_formulas_and_cached_values(self):
        path = self.make_office(".xlsx", {
            "xl/sharedStrings.xml": '<sst><si><t>Support</t></si></sst>',
            "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="inlineStr"><is><t>HDR</t></is></c><c r="C1"><f>1+1</f><v>2</v></c>'
            '<c r="D1" t="b"><v>1</v></c></row></sheetData></worksheet>'})
        text = sources.office_text(path)
        self.assertIn("A1: Support", text)
        self.assertIn("B1: HDR", text)
        self.assertIn("C1: =1+1 [cached: 2]", text)
        self.assertIn("D1: 1", text)

    def test_ppt_slides_and_notes_are_available(self):
        path = self.make_office(".pptx", {
            "ppt/slides/slide2.xml": '<sld><t>Second</t></sld>',
            "ppt/slides/slide1.xml": '<sld><t>First</t></sld>',
            "ppt/notesSlides/notesSlide1.xml": '<notes><t>Presenter notes</t></notes>'})
        text = sources.office_text(path)
        self.assertLess(text.index("First"), text.index("Second"))
        self.assertIn("Presenter notes", text)

    def test_office_content_limit_reports_incomplete_instead_of_returning_fragment(self):
        path = self.make_office(".docx", {"word/document.xml": '<document><t>full content required</t></document>'})
        with self.assertRaisesRegex(sources.SourceError, "exceeds"):
            sources.office_text(path, max_chars=10)

    def test_large_ephemeral_content_marks_partial_and_retains_cursor(self):
        config = self.config(["codex"])
        config["content_max_chars"] = 5
        event = sources._event("codex", "large", self.now, "Message", "file:///session", {}, "long source content")
        previous = self.since.isoformat()
        with patch.object(sources, "_sessions", return_value=([event], "test")):
            batch = sources.collect(config, {"codex": previous}, self.since, self.now)[4]
        self.assertEqual("partial", batch.status)
        self.assertEqual(previous, batch.cursor)
        self.assertNotIn("content", batch.events[0])
        self.assertFalse(batch.events[0]["metadata"]["content_available"])

    def test_codex_conversations_have_references_without_copying_body_to_index(self):
        root = self.root / "sessions"
        root.mkdir()
        path = root / "session.jsonl"
        messages = [
            {"timestamp": (self.since-timedelta(days=1)).isoformat(), "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old"}]}},
            {"timestamp": (self.now-timedelta(minutes=1)).isoformat(), "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "private HDR question"}]}},
            {"timestamp": self.now.isoformat(), "type": "response_item",
             "payload": {"type": "function_call_output", "output": "do not collect tool output"}},
            ["invalid shape"],
        ]
        path.write_text("\n".join(json.dumps(m) for m in messages) + "\n{invalid complete record}\n", encoding="utf-8")
        config = self.config(["codex"])
        config["sources"]["codex"]["roots"] = [str(root)]
        batch = sources.collect(config, {}, self.since, self.now)[4]
        self.assertEqual("ok", batch.status)
        self.assertEqual(1, len(batch.events))
        event = batch.events[0]
        self.assertNotIn("private", event["title"])
        self.assertEqual("private HDR question", event["content"])
        self.assertEqual("private HDR question", sources.read_content(event, config))
        path.write_text(path.read_text().replace("private HDR question", "edited message"))
        with self.assertRaisesRegex(sources.SourceError, "changed"):
            sources.read_content(event, config)

    def test_claude_ignores_tool_results_and_reads_text_blocks(self):
        item = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "tool output"}, {"type": "text", "text": "Question"}]}}
        self.assertEqual(("user", "Question"), sources._session_message("claude", item))

    def test_incomplete_appended_conversation_record_does_not_advance_cursor(self):
        root = self.root / "sessions"
        root.mkdir()
        (root / "pending.jsonl").write_text('{"timestamp":', encoding="utf-8")
        config = self.config(["codex"])
        config["sources"]["codex"]["roots"] = [str(root)]
        previous = self.since.isoformat()
        batch = sources.collect(config, {"codex": previous}, self.since, self.now)[4]
        self.assertEqual("error", batch.status)
        self.assertIn("incomplete", batch.detail)
        self.assertEqual(previous, batch.cursor)

    def test_failed_sources_keep_cursor_and_missing_access_is_not_healthy(self):
        config = self.config(["chrome", "outlook", "teams", "chatgpt"])
        old = self.since.isoformat()
        with patch.object(sources, "_chrome", side_effect=sqlite3.OperationalError("database locked")):
            batches = sources.collect(config, {"chrome": old}, self.since, self.now)
        self.assertEqual("error", batches[0].status)
        self.assertEqual(old, batches[0].cursor)
        for source in ("chatgpt", "outlook", "teams"):
            self.assertEqual("unconfigured", next(b for b in batches if b.source == source).status)

    def test_graph_pagination_uses_server_nextlink_and_never_sends_token_to_other_host(self):
        config = {"microsoft": {"client_id": "app-id"}}
        with patch.object(sources, "_access_token", return_value="SECRET"):
            graph = sources._Graph(config)
        replies = [{"value": [{"id": "a"}], "@odata.nextLink": sources.GRAPH + "/me/chats?skip=2"},
                   {"value": [{"id": "b"}]}]
        with patch.object(sources, "_request_json", side_effect=replies) as request:
            self.assertEqual([{"id": "a"}, {"id": "b"}], list(graph.pages("/me/chats")))
            self.assertIn("skip=2", request.call_args_list[1].args[0])
        with patch.object(sources, "_request_json") as request:
            with self.assertRaisesRegex(sources.SourceError, "non-Microsoft"):
                graph.get("https://evil.example/?token=SECRET")
            request.assert_not_called()

    def test_microsoft_token_write_has_private_permissions_and_no_raw_response(self):
        config = {"data_dir": str(self.root), "microsoft": {"client_id": "app-id"}}
        sources._save_token(config, {"access_token": "access", "refresh_token": "refresh", "expires_in": 300,
                                    "unexpected_body": "must not persist"})
        path = self.root / "microsoft-auth.json"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        saved = json.loads(path.read_text())
        self.assertNotIn("unexpected_body", saved)
        self.assertEqual("access", saved["access_token"])

    def test_microsoft_login_poll_does_not_return_token_to_caller(self):
        config = {"data_dir": str(self.root), "microsoft": {"client_id": "app-id"}}
        with patch.object(sources, "_oauth", return_value={"access_token": "secret", "scope": "Mail.Read", "expires_in": 300}):
            result = sources.finish_microsoft_login(config, "device-code")
        self.assertTrue(result["authenticated"])
        self.assertNotIn("secret", json.dumps(result))

    def test_teams_old_thread_new_reply_is_collected(self):
        config = {"microsoft": {"client_id": "app-id", "chat_ids": [], "team_ids": ["team"]}}
        old = (self.since-timedelta(days=30)).isoformat()
        new = (self.now-timedelta(minutes=1)).isoformat()
        parent = {"id": "thread", "createdDateTime": old, "lastModifiedDateTime": old,
                  "body": {"content": "old"}, "replies": [
                      {"id": "reply", "lastModifiedDateTime": new, "body": {"content": "<p>ready for IT</p>"}}]}
        class FakeGraph:
            def __init__(self, config):
                pass
            def get(self, path, params=None):
                return {"userPrincipalName": "user@example.com"}
            def pages(self, path, params=None):
                return iter([{"id": "channel"}] if path.endswith("/channels") else [parent])
        with patch.object(sources, "_Graph", FakeGraph):
            events, detail = sources._microsoft("teams", config, self.since, self.now)
        self.assertEqual(1, len(events))
        self.assertEqual("thread", events[0]["metadata"]["reply_to_id"])
        self.assertEqual("user@example.com", events[0]["metadata"]["account"])
        self.assertEqual("ready for IT", events[0]["content"])


if __name__ == "__main__":
    unittest.main()
