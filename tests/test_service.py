import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from monitorbot import service
from monitorbot.agents import AgentError, validate_json
from monitorbot.config import initialize
from monitorbot.reports import previous_week
from monitorbot.sources import SourceBatch, SourceError
from monitorbot.store import Store


EMPTY_PLAN = {"tasks": [], "knowledge": [], "questions": []}


class FakeRunner:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []

    def run(self, role, payload, schema):
        self.calls.append((role, copy.deepcopy(payload)))
        result = self.handler(role, payload) if self.handler else copy.deepcopy(EMPTY_PLAN)
        validate_json(result, schema)
        return result


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = initialize(self.root)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.now = datetime.now(timezone.utc)

    def event(self, source="manual", source_id="synthetic", at=None, content=None):
        value = {"source": source, "source_id": source_id,
                 "occurred_at": (at or self.now).isoformat(), "title": "Synthetic work activity",
                 "url": "https://example.invalid/work", "metadata": {"content_available": True}}
        if content is not None:
            value["content"] = content
        return value

    def proposal(self, task, status="active", refs=None, **changes):
        result = {"id": task.get("id"), "title": task["title"], "outcome": task.get("outcome", ""),
                  "project": task.get("project", ""), "kind": task.get("kind", "assigned"),
                  "ownership": task.get("ownership", "self"), "status": status,
                  "due_at": task.get("due_at"), "waiting_until": task.get("waiting_until"),
                  "waiting_for": task.get("waiting_for", ""), "next_action": task.get("next_action", ""),
                  "priority": task.get("priority", 50), "blocks_others": task.get("blocks_others", False),
                  "evidence_ids": refs if refs is not None else task.get("evidence_ids", []),
                  "reason": "Synthetic source supports this update", "substantive": True}
        result.update(changes)
        return result

    def run_with(self, batches, runner=None, read=None):
        with patch.object(service.sources, "collect", return_value=batches):
            if read is not None:
                with patch.object(service.sources, "read_content", side_effect=read):
                    return service.run_cycle(self.store, self.config, runner or FakeRunner())
            return service.run_cycle(self.store, self.config, runner or FakeRunner())

    def test_unreadable_expected_body_stays_pending_and_blocks_idle_cancellation(self):
        old = self.now - timedelta(days=40)
        original = self.store.add_event(self.event("outlook", "old-idea", old))
        self.store.mark_processed([original["id"]])
        task = self.store.upsert_task({"title": "Synthetic optional investigation", "kind": "optional",
                                      "evidence_ids": [original["id"]], "last_activity_at": old.isoformat()}, now=old)
        fresh = self.event("outlook", "new-progress")
        runner = FakeRunner()
        result = self.run_with([SourceBatch("outlook", [fresh], "ok", "synthetic", self.now.isoformat())],
                               runner, SourceError("synthetic content unavailable"))
        self.assertEqual(1, result["pending"])
        self.assertEqual(0, result["processed"])
        self.assertFalse(self.store.events(unprocessed=True)[0]["processed"])
        self.assertEqual("partial", self.store.health()["outlook"]["status"])
        self.assertEqual("active", self.store.get_task(task["id"])["status"])
        self.assertEqual([], runner.calls)

    def test_idle_disagreement_is_not_repeated_without_changed_input(self):
        old = self.now - timedelta(days=40)
        event = self.store.add_event(self.event("outlook", "old-idea", old))
        self.store.mark_processed([event["id"]])
        task = self.store.upsert_task({"title": "Synthetic candidate", "kind": "candidate",
                                      "evidence_ids": [event["id"]], "last_activity_at": old.isoformat()}, now=old)
        def disagree(role, payload):
            self.assertEqual("reviewer", role)
            return {"decisions": [{"task_id": task["id"], "status": "active",
                                    "reason": "Synthetic case is still relevant", "evidence_ids": []}]}
        runner = FakeRunner(disagree)
        batch = SourceBatch("outlook", [], "ok", "synthetic", self.now.isoformat())
        self.run_with([batch], runner)
        self.run_with([batch], runner)
        self.assertEqual(1, len(runner.calls))
        self.assertEqual("active", self.store.get_task(task["id"])["status"])
        revised = self.store.upsert_task({"id": task["id"], "next_action": "New explicit follow-up"},
                                        substantive=False)
        self.run_with([batch], runner)
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(revised["revision"], self.store.get_task(task["id"])["revision"])

    def test_unreadable_backlog_does_not_starve_later_manual_activity(self):
        for index in range(1000):
            self.store.add_event(self.event("outlook", "unread-" + str(index),
                                            self.now - timedelta(minutes=2) + timedelta(microseconds=index)))
        latest = self.store.add_event(self.event("manual", "latest-idea"))
        runner = FakeRunner()
        first = self.run_with([], runner, SourceError("synthetic unreadable body"))
        self.assertEqual(len(self.store.events(unprocessed=True, limit=None)), first["pending"])
        second = self.run_with([], runner, SourceError("synthetic unreadable body"))
        self.assertTrue(self.store.get_event(latest["id"])["processed"])
        self.assertEqual(1000, second["pending"])
        self.assertTrue(any(role == "tracker" for role, _ in runner.calls))

    def completion_cycle(self, verdict):
        old = self.store.add_event(self.event("manual", "initial-request", self.now - timedelta(days=1)))
        self.store.mark_processed([old["id"]])
        task = self.store.upsert_task({"title": "Deliver synthetic report", "kind": "assigned", "ownership": "self",
                                      "evidence_ids": [old["id"]], "outcome": "Recipient receives report"})
        source = self.event("teams", "delivery-confirmation", content="Synthetic recipient confirms delivery")
        def respond(role, payload):
            if role == "tracker":
                refs = [payload["events"][0]["id"]]
                proposal = self.proposal(task, "completed", refs, outcome="TRACKER_ONLY_CONCLUSION",
                                         reason="TRACKER_ONLY_CONCLUSION")
                return {"tasks": [proposal], "knowledge": [], "questions": []}
            self.assertEqual("reviewer", role)
            self.assertNotIn("TRACKER_ONLY_CONCLUSION", json.dumps(payload))
            self.assertEqual("Recipient receives report", payload["tasks"][0]["outcome"])
            return {"decisions": [{"task_id": task["id"], "status": verdict,
                                    "reason": "Independent synthetic evidence evaluation",
                                    "evidence_ids": [payload["events"][0]["id"]]}]}
        runner = FakeRunner(respond)
        self.run_with([SourceBatch("teams", [source], "ok", "synthetic", self.now.isoformat())], runner)
        return self.store.get_task(task["id"]), runner

    def test_tracker_and_reviewer_have_independent_contexts_before_completion(self):
        task, runner = self.completion_cycle("completed")
        self.assertEqual("completed", task["status"])
        self.assertEqual(["tracker", "reviewer"], [role for role, _ in runner.calls])
        self.assertIn("獨立複核", task["end_reason"])

    def test_reviewer_disagreement_keeps_task_in_tracking(self):
        task, runner = self.completion_cycle("active")
        self.assertEqual("active", task["status"])
        self.assertTrue(any(item["kind"] == "review_disagreement" for item in self.store.logs()))

    def test_tracker_failure_leaves_event_unprocessed(self):
        event = self.store.add_event(self.event())
        def fail(role, payload):
            raise AgentError("synthetic CLI failure")
        result = self.run_with([], FakeRunner(fail))
        self.assertEqual(1, result["pending"])
        self.assertFalse(self.store.get_event(event["id"])["processed"])
        self.assertTrue(any(item["kind"] == "tracking_failed" for item in self.store.logs()))

    def test_new_task_can_be_created_from_an_event_and_retry_does_not_duplicate_it(self):
        event = self.store.add_event(self.event())
        def create(role, payload):
            return {"tasks": [self.proposal({"id": None, "title": "Synthetic new task"},
                                             refs=[payload["events"][0]["id"]])],
                    "knowledge": [], "questions": []}
        runner = FakeRunner(create)
        self.run_with([], runner)
        self.run_with([], runner)
        self.assertEqual(1, len(self.store.list_tasks()))
        self.assertTrue(self.store.get_event(event["id"])["processed"])
        self.assertEqual(1, len(runner.calls))

    def test_source_failure_keeps_last_successful_cursor(self):
        previous = (self.now - timedelta(hours=1)).isoformat()
        self.store.set_source_status("outlook", "ok", cursor=previous)
        self.run_with([SourceBatch("outlook", [], "error", "synthetic connection failure", None)])
        self.assertEqual(previous, self.store.get_cursor("outlook"))
        self.assertEqual("error", self.store.health()["outlook"]["status"])

    def test_source_filters_hide_only_after_all_evidence_is_excluded_and_preserve_knowledge(self):
        teams = self.store.add_event(self.event("teams", "team-reference"))
        mail = self.store.add_event(self.event("outlook", "mail-reference"))
        self.store.mark_processed([teams["id"], mail["id"]])
        task = self.store.upsert_task({"title": "Synthetic multi-source task", "kind": "assigned",
                                      "evidence_ids": [teams["id"], mail["id"]]})
        knowledge = self.store.upsert_knowledge({"topic": "Synthetic mechanism", "content": "Supported mechanism",
                                                "evidence_ids": [teams["id"]]})
        self.config["sources"]["teams"]["exclude"] = [{"source": "teams"}]
        self.run_with([])
        self.assertEqual("active", self.store.get_task(task["id"])["status"])
        self.config["sources"]["outlook"]["exclude"] = [{"source": "outlook"}]
        self.run_with([])
        self.assertEqual([], self.store.list_tasks())
        self.assertEqual(knowledge["id"], self.store.list_knowledge()[0]["id"])
        self.config["sources"]["teams"]["exclude"] = []
        self.run_with([])
        self.assertEqual([], self.store.list_tasks())
        new_source = self.event("teams", "renewed-reference", content="Synthetic renewed responsibility")
        def reopen(role, payload):
            return {"tasks": [self.proposal(task, "active", refs=[payload["events"][0]["id"]])],
                    "knowledge": [], "questions": []}
        self.run_with([SourceBatch("teams", [new_source], "ok", "synthetic", self.now.isoformat())],
                      FakeRunner(reopen))
        self.assertEqual("active", self.store.get_task(task["id"])["status"])

    def test_new_source_evidence_cannot_bypass_explicit_user_exclusion(self):
        old = self.store.add_event(self.event("manual", "original"))
        self.store.mark_processed([old["id"]])
        task = self.store.upsert_task({"title": "Synthetic explicitly excluded task", "kind": "assigned",
                                      "evidence_ids": [old["id"]]})
        self.store.transition_task(task["id"], "excluded", "User excluded this topic", actor="user")
        new_source = self.event("teams", "new-discussion", content="Synthetic new discussion")
        def reopen(role, payload):
            return {"tasks": [self.proposal(task, "active", refs=[payload["events"][0]["id"]])],
                    "knowledge": [], "questions": []}
        self.run_with([SourceBatch("teams", [new_source], "ok", "synthetic", self.now.isoformat())],
                      FakeRunner(reopen))
        self.assertEqual("excluded", self.store.get_task(task["id"])["status"])
        self.assertEqual([], self.store.list_tasks())

    def test_question_refresh_rewrites_only_public_fields_and_resolves_obsolete_question(self):
        event = self.store.add_event(self.event())
        question = self.store.upsert_question({"text": "Is development done?", "choices": ["Yes", "No"],
                                              "evidence_ids": [event["id"]]})
        def revise(role, payload):
            self.assertEqual("clarifier", role)
            return {"questions": [{"id": question["id"], "status": "open", "text": "Which version needs verification?",
                                    "choices": ["Version A", "Version B"], "suggested_index": 1,
                                    "reason": "New synthetic context"}]}
        self.assertTrue(service.refresh_questions(self.store, self.config, FakeRunner(revise)))
        current = self.store.list_questions()[0]
        self.assertEqual("Which version needs verification?", current["text"])
        self.assertEqual(["Version A", "Version B"], current["choices"])
        self.assertEqual(question["created_at"], current["created_at"])
        def resolve(role, payload):
            return {"questions": [{"id": question["id"], "status": "resolved", "text": current["text"],
                                    "choices": current["choices"], "suggested_index": 1,
                                    "reason": "Someone else resolved it"}]}
        self.assertTrue(service.refresh_questions(self.store, self.config, FakeRunner(resolve)))
        self.assertEqual([], self.store.list_questions())

    def test_question_refresh_failure_does_not_declare_stale_question_reviewed(self):
        event = self.store.add_event(self.event("outlook"))
        self.store.upsert_question({"text": "Synthetic unresolved question", "choices": ["A", "B"],
                                   "evidence_ids": [event["id"]]})
        runner = FakeRunner()
        with patch.object(service.sources, "read_content", side_effect=SourceError("synthetic access failure")):
            self.assertFalse(service.refresh_questions(self.store, self.config, runner))
        self.assertEqual([], runner.calls)
        self.assertEqual(1, len(self.store.list_questions()))

    def test_weekly_payload_uses_event_time_and_keeps_current_reopened_task(self):
        monday = datetime(2026, 11, 9, 2, tzinfo=timezone.utc)
        start, end = previous_week(monday)
        friday = end - timedelta(days=2)
        previous = self.store.add_event(self.event("manual", "last-week-delivery", friday))
        task = self.store.upsert_task({"title": "Synthetic weekly work", "kind": "assigned",
                                      "evidence_ids": [previous["id"]]}, now=start, occurred_at=start)
        completed = self.store.transition_task(task["id"], "completed", "Delivered last week", actor="user",
                                              now=friday, occurred_at=friday)
        latest = self.store.add_event(self.event("manual", "this-week-correction", monday))
        self.store.transition_task(task["id"], "active", "Reopened this week", evidence_ids=[latest["id"]],
                                   expected_revision=completed["revision"], now=monday, occurred_at=monday)
        def report(role, payload):
            self.assertEqual("reporter", role)
            data = payload["data"]
            self.assertEqual([previous["id"]], [item["id"] for item in data["events"]])
            self.assertEqual("active", data["tasks"][0]["status"])
            completed_records = [item for item in data["history"] if item["after"]["status"] == "completed"]
            self.assertEqual(1, len(completed_records))
            self.assertEqual(friday, datetime.fromisoformat(completed_records[0]["occurred_at"]))
            self.assertEqual(monday, datetime.fromisoformat(payload["current_state_as_of"]))
            return {"markdown": "# Synthetic weekly report\n", "conflicts": []}
        runner = FakeRunner(report)
        result = service.write_reports(self.store, self.config, runner, now=monday)
        self.assertEqual({"weekly", "system"}, set(result))
        self.assertTrue(all(Path(path).is_file() for path in result.values()))

    def test_weekly_conflict_returns_to_tracker_before_report_continues(self):
        task = self.store.upsert_task({"title": "Synthetic conflicting task", "kind": "assigned"})
        calls = []
        def respond(role, payload):
            calls.append(role)
            if role == "tracker":
                self.assertEqual(task["id"], payload["conflicts"][0]["task_id"])
                return copy.deepcopy(EMPTY_PLAN)
            if len(calls) == 1:
                return {"markdown": "", "conflicts": [{"task_id": task["id"], "message": "Synthetic conflict"}]}
            return {"markdown": "# Resolved synthetic report\n", "conflicts": []}
        result = service.write_reports(self.store, self.config, FakeRunner(respond), now=self.now)
        self.assertEqual(["reporter", "tracker", "reporter", "reporter"], calls)
        self.assertTrue(Path(result["weekly"]).is_file())

    def test_schedule_obeys_work_hours_daily_once_and_hourly_poll(self):
        monday = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
        self.store.set_meta("started_at", (monday - timedelta(days=1)).isoformat())
        self.store.set_meta("last_poll", (monday - timedelta(minutes=30)).isoformat())
        due = service.schedule_due(self.store, self.config, monday)
        self.assertTrue(due["daily"])
        self.assertTrue(due["weekly"])
        self.assertFalse(due["poll"])
        self.store.set_meta("daily_shown", "2026-11-09")
        self.assertFalse(service.schedule_due(self.store, self.config, monday)["daily"])
        self.assertTrue(service.schedule_due(self.store, self.config, monday + timedelta(minutes=31))["poll"])
        self.assertFalse(service.schedule_due(self.store, self.config, monday - timedelta(hours=1))["daily"])
        self.assertFalse(service.schedule_due(self.store, self.config, monday + timedelta(hours=10))["daily"])
        self.assertFalse(service.schedule_due(self.store, self.config, monday + timedelta(days=5))["daily"])

    def test_weekly_failure_cooldown_and_success_prevent_repeated_reports(self):
        monday = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
        self.store.set_meta("started_at", (monday - timedelta(days=1)).isoformat())
        self.store.set_meta("weekly_attempt", monday.isoformat())
        self.assertFalse(service.schedule_due(self.store, self.config, monday + timedelta(minutes=59))["weekly"])
        self.assertTrue(service.schedule_due(self.store, self.config, monday + timedelta(hours=1))["weekly"])
        self.store.set_meta("weekly_done", "2026-11-09")
        self.assertFalse(service.schedule_due(self.store, self.config, monday + timedelta(days=1))["weekly"])


if __name__ == "__main__":
    unittest.main()
