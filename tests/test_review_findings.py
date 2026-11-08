import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from monitorbot import service
from monitorbot.config import initialize
from monitorbot.sources import SourceError
from monitorbot.store import Store
from test_service import FakeRunner


class ReviewFindingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = initialize(self.root)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)

    def test_unreadable_high_priority_questions_do_not_hide_readable_question(self):
        bad = self.store.add_event({"source": "teams", "source_id": "missing",
                                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                                    "title": "Unavailable discussion", "url": "https://example.invalid/missing"})
        for index in range(20):
            self.store.upsert_question({"text": "Missing discussion {}".format(index),
                                       "choices": ["Yes", "No"], "priority": 100,
                                       "evidence_ids": [bad["id"]]})
        good = self.store.upsert_question({"text": "Readable manual question", "choices": ["Yes", "No"], "priority": 1})
        def respond(role, payload):
            return {"questions": [{"id": question["id"], "text": question["text"],
                                    "choices": question["choices"], "status": "open", "reason": "Still relevant",
                                    "suggested_index": 0} for question in payload["questions"]]}
        runner = FakeRunner(respond)
        with patch.object(service.sources, "read_content", side_effect=SourceError("Unavailable")):
            service.refresh_questions(self.store, self.config, runner)
        self.assertIn(good["id"], self.store.get_meta("daily_reviewed_questions"))
        self.assertLessEqual(len(self.store.get_meta("daily_reviewed_questions")), 20)

    def test_conflict_retry_rebuilds_history_and_preserves_period_filters(self):
        now = datetime.now(timezone.utc)
        start, end = service.previous_week(now, self.config["timezone"])
        happened = start + timedelta(days=2)
        event = self.store.add_event({"source": "manual", "source_id": "finished-work",
                                      "occurred_at": happened.isoformat(), "title": "Delivery confirmed"})
        task = self.store.upsert_task({"title": "Deliver report", "kind": "assigned", "ownership": "self",
                                      "evidence_ids": [event["id"]]}, occurred_at=happened.isoformat())
        unrelated = self.store.upsert_task({"title": "Old unrelated completed task"}, now=start - timedelta(days=10))
        self.store.transition_task(unrelated["id"], "completed", "Old completion", actor="user", now=start - timedelta(days=9))
        reporter_calls = []
        def respond(role, payload):
            if role == "tracker":
                return {"tasks": [{"id": task["id"], "title": task["title"], "outcome": "Delivered",
                    "project": "", "kind": "assigned", "ownership": "self", "status": "completed",
                    "due_at": None, "waiting_until": None, "waiting_for": "", "next_action": "",
                    "priority": 50, "blocks_others": False, "evidence_ids": [event["id"]],
                    "reason": "Delivery confirmed", "substantive": True}], "knowledge": [], "questions": []}
            if role == "reviewer":
                return {"decisions": [{"task_id": task["id"], "status": "completed", "reason": "Independent agreement",
                                        "evidence_ids": [event["id"]]}]}
            reporter_calls.append(copy.deepcopy(payload["data"]))
            if len(reporter_calls) == 1:
                return {"markdown": "", "conflicts": [{"task_id": task["id"], "message": "Check completion"}]}
            return {"markdown": "# Verified weekly report", "conflicts": []}
        service.write_reports(self.store, self.config, FakeRunner(respond), now=now)
        second = reporter_calls[1]
        actual = self.store.history(since=start.isoformat(), until=end.isoformat())
        self.assertEqual(len(actual), len(second["history"]))
        self.assertEqual(len(actual), second["coverage_counts"]["history"])
        self.assertTrue(any(item["after"]["status"] == "completed" for item in second["history"]))
        self.assertNotIn(unrelated["id"], {item["id"] for item in second["tasks"]})

    def test_explicitly_excluded_task_does_not_keep_asking_questions(self):
        task = self.store.upsert_task({"title": "Do not track this task"})
        self.store.upsert_question({"task_id": task["id"], "text": "Still needed?", "choices": ["Yes", "No"]})
        self.store.transition_task(task["id"], "excluded", "User exclusion", actor="user")
        runner = FakeRunner()
        self.assertTrue(service.refresh_questions(self.store, self.config, runner))
        self.assertEqual([], self.store.list_questions())
        self.assertEqual([], runner.calls)


if __name__ == "__main__":
    unittest.main()
