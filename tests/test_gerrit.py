import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from monitorbot import gerrit, sources
from monitorbot.store import Store


class GerritTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 1, tzinfo=timezone.utc)
        self.since = self.now - timedelta(hours=1)
        self.config = {"sources": {name: {"enabled": name == "gerrit"} for name in sources.SOURCES}}
        self.config["sources"]["gerrit"].update(url="https://review.example/gerrit", query="owner:self OR reviewer:self")
        self.change = {"_number": 12, "updated": "2026-11-09 00:30:00.123456789", "subject": "Fix HDR",
                       "project": "display", "branch": "main", "status": "NEW"}
        self.detail = dict(self.change, current_revision="abc", revisions={"abc": {"commit": {"message": "Description"}}},
                           messages=[{"date": "today", "message": "Please test", "author": {"name": "Reviewer"}}])
        self.comments = {"a.py": [{"line": 3, "message": "Check range", "author": {"name": "Reviewer"}}]}

    def batch(self, previous=None):
        return sources.collect(self.config, {"gerrit": previous} if previous else {}, self.since, self.now)[-1]

    def test_paginated_changes_persist_indexes_and_read_current_discussion(self):
        second = dict(self.change, _number=13)
        replies = [[dict(self.change, _more_changes=True)], self.detail, self.comments,
                   [second], dict(self.detail, _number=13), {}]
        with patch.object(gerrit.Gerrit, "get", side_effect=replies) as get:
            batch = self.batch()
        self.assertEqual("ok", batch.status)
        self.assertEqual(self.now.isoformat(), batch.cursor)
        self.assertEqual([12, 13], [e["metadata"]["change_number"] for e in batch.events])
        self.assertEqual(1, get.call_args_list[3].args[1]["S"])
        self.assertIn('after:"2026-11-09 00:00:00"', get.call_args_list[0].args[1]["q"])
        with tempfile.TemporaryDirectory() as directory, Store(directory) as store:
            event = store.add_event(batch.events[0])
            store.add_event(batch.events[0])
            self.assertEqual(1, len(store.events()))
            saved = store.get_event(event["id"])
            self.assertNotIn("content", saved)
            self.assertEqual(12, saved["metadata"]["change_number"])
            with patch.object(gerrit.Gerrit, "get", side_effect=[self.detail, self.comments]):
                text = sources.read_content(saved, self.config)
            self.assertIn("Description", text)
            self.assertIn("Please test", text)
            self.assertIn("a.py:3", text)
            self.assertIn("Check range", text)
            with patch.object(gerrit.Gerrit, "get", side_effect=[dict(self.detail, updated="2026-11-09 00:40:00"), {}]):
                self.assertIn("updated since this event", sources.read_content(saved, self.config))

    def test_exclusions_avoid_body_reads_and_failed_later_page_keeps_cursor(self):
        self.config["sources"]["gerrit"]["exclude"] = [{"project": "display"}]
        with patch.object(gerrit.Gerrit, "get", return_value=[self.change]) as get:
            self.assertEqual([], self.batch().events)
            self.assertEqual(1, get.call_count)
        previous = self.since.isoformat()
        with patch.object(gerrit.Gerrit, "get", side_effect=[[dict(self.change, _more_changes=True)], sources.SourceError("Failed")]):
            batch = self.batch(previous)
        self.assertEqual("error", batch.status)
        self.assertEqual(previous, batch.cursor)

    def test_changed_during_collection_and_malformed_pages_do_not_advance(self):
        for replies in ([[self.change], dict(self.detail, updated="new"), {}],
                        [[self.change], dict(self.detail, current_revision=[]), {}],
                        [{"invalid": True}], [[dict(self.change, updated="yesterday")]], [[{"_number": "12"}]]):
            with self.subTest(replies=replies), patch.object(gerrit.Gerrit, "get", side_effect=replies):
                self.assertEqual("error", self.batch().status)

    def test_reference_cannot_send_credentials_to_another_server(self):
        event = sources._event("gerrit", "12", self.now, "title", "https://evil.example", {
            "server": "https://evil.example", "change_number": 12})
        with patch.object(gerrit.Gerrit, "get") as get, self.assertRaises(sources.SourceError):
            sources.read_content(event, self.config)
        get.assert_not_called()

    def test_https_auth_xssi_and_error_redaction(self):
        settings = self.config["sources"]["gerrit"]
        settings.update(username="alice", token_env="MONITOR_TEST_GERRIT_TOKEN")
        with patch.dict(os.environ, MONITOR_TEST_GERRIT_TOKEN="secret"):
            client = gerrit.Gerrit(self.config)
        response = io.BytesIO(b")]}'\n" + json.dumps([self.change]).encode())
        with patch.object(client.opener, "open", return_value=response) as request:
            self.assertEqual([self.change], client.get("/changes/"))
            sent = request.call_args.args[0]
            self.assertEqual("https://review.example/gerrit/a/changes/", sent.full_url)
            self.assertTrue(sent.get_header("Authorization").startswith("Basic "))
            self.assertNotIn("secret", sent.full_url)
        error = urllib.error.HTTPError("https://example", 403, "secret", {}, io.BytesIO(b"secret"))
        with patch.object(client.opener, "open", side_effect=error), self.assertRaisesRegex(sources.SourceError, "^Gerrit HTTP 403$"):
            client.get("/changes/")
        with self.assertRaises(sources.SourceError):
            gerrit._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")
        for url in ("http://review.example", "https://u:secret@review.example", "https://review.example?token=secret"):
            settings["url"] = url
            with self.assertRaises(sources.SourceError):
                gerrit.Gerrit(self.config)

    def test_large_content_uses_existing_partial_health_behavior(self):
        self.config["content_max_chars"] = 5
        with patch.object(gerrit.Gerrit, "get", side_effect=[[self.change], self.detail, self.comments]):
            batch = self.batch()
        self.assertEqual("partial", batch.status)
        self.assertIsNone(batch.cursor)
        self.assertNotIn("content", batch.events[0])


if __name__ == "__main__":
    unittest.main()
