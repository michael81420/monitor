from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from monitorbot.store import Store


START = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
LATER = START + timedelta(days=40)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, source="teams", source_id="m1", at=START):
        return self.store.add_event({"source": source, "source_id": source_id,
            "occurred_at": at.isoformat(), "title": "HDR discussion",
            "url": "https://teams.microsoft.com/l/message/example",
            "metadata": {"message_id": source_id}})

    def task(self, **changes):
        data = {"title": "Research HDR", "kind": "optional"}
        data.update(changes)
        return self.store.upsert_task(data, now=START)

    def test_index_deduplicates_and_does_not_persist_raw_content(self):
        marker = "SECRET_RAW_CONTENT_NOT_AN_INDEX"
        value = {"source": "outlook", "source_id": "mail-1", "occurred_at": START.isoformat(),
            "title": "Meeting", "url": "https://outlook.office.com/mail/item",
            "content": marker, "body": marker, "messages": [marker],
            "metadata": {"body": marker, "messages": [marker], "text": marker,
                         "message_id": "mail-1", "folder_id": "inbox", "recipients": ["user@example.com"]}}
        first = self.store.add_event(value)
        second = self.store.add_event(value)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.events()[0]["metadata"],
                         {"message_id": "mail-1", "folder_id": "inbox", "recipients": ["user@example.com"]})
        self.assertNotIn(marker.encode(), self.store.path.read_bytes())
        self.store.mark_processed([first["id"]])
        self.assertEqual(self.store.events(unprocessed=True), [])

    def test_waiting_two_months_and_assigned_tasks_never_idle_cancel(self):
        waiting = self.task(title="IT verification", kind="assigned", ownership="self", status="waiting",
            waiting_for="Development owner", waiting_until=(START + timedelta(days=60)).isoformat())
        assigned = self.task(title="Promised report", kind="assigned", ownership="self")
        self.assertEqual(self.store.idle_candidates(now=LATER), [])
        for task in (waiting, assigned):
            with self.assertRaisesRegex(ValueError, "not eligible"):
                self.store.transition_task(task["id"], "cancelled", "idle", reviewed=True,
                                           expected_revision=task["revision"], now=LATER)
        self.assertEqual(self.store.get_task(waiting["id"])["status"], "waiting")

    def test_source_urls_do_not_persist_credentials_or_login_tokens(self):
        event = self.store.add_event({"source": "chrome", "source_id": "visit-1", "occurred_at": START.isoformat(),
            "url": "https://user:password@example.com/page?item=123&access_token=secret#id_token=private&section=2"})
        self.assertEqual(event["url"], "https://example.com/page?item=123#section=2")

    def test_source_event_queries_page_without_cropping_full_reconciliation(self):
        first = self.event("teams", "one", START)
        second = self.event("teams", "two", START + timedelta(seconds=1))
        self.event("outlook", "mail", START + timedelta(seconds=2))
        self.assertEqual([item["id"] for item in self.store.events(source="teams", limit=1, offset=1)], [second["id"]])
        self.assertEqual([item["id"] for item in self.store.events(source="teams", limit=None)], [first["id"], second["id"]])
        self.assertEqual(len(self.store.events(limit=None, offset=1)), 2)
        for args in ({"limit": 0}, {"offset": -1}, {"limit": True}):
            with self.assertRaises(ValueError):
                self.store.events(**args)

    def test_null_ids_create_records_and_unknown_knowledge_time_stays_unknown(self):
        task = self.store.upsert_task({"id": None, "title": "A task"})
        knowledge = self.store.upsert_knowledge({"id": None, "topic": "HDR", "content": "Existing mechanism"})
        question = self.store.upsert_question({"id": None, "text": "Who owns it?", "choices": ["Me", "Other"]})
        self.assertTrue(task["id"])
        self.assertTrue(knowledge["id"])
        self.assertTrue(question["id"])
        self.assertIsNone(knowledge["effective_at"])

    def test_unconfirmed_and_optional_tasks_idle_without_unrelated_source_health(self):
        optional = self.task()
        candidate = self.task(title="Unconfirmed request", kind="candidate", ownership="unknown")
        self.store.set_source_status("outlook", "unconfigured", checked_at=LATER)
        self.assertEqual({t["id"] for t in self.store.idle_candidates(now=LATER)}, {optional["id"], candidate["id"]})
        closed = self.store.transition_task(candidate["id"], "cancelled", "responsibility unconfirmed; no follow-up",
            reviewed=True, expected_revision=candidate["revision"], now=LATER)
        self.assertEqual(closed["status"], "cancelled")
        self.assertIn("responsibility unconfirmed", closed["end_reason"])

    def test_deadline_blocks_idle_cancellation(self):
        self.task(due_at=(START + timedelta(days=60)).isoformat())
        self.assertEqual(self.store.idle_candidates(now=LATER), [])

    def test_stable_caller_id_makes_retries_idempotent(self):
        task = self.task(id="stable-id-from-event")
        retried = self.store.upsert_task({"id": task["id"], "title": task["title"]}, now=LATER)
        self.assertEqual(retried["revision"], 1)
        self.assertEqual(retried["last_activity_at"], task["last_activity_at"])
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertEqual(len(self.store.history()), 1)

    def test_history_keeps_event_time_separate_from_processing_time(self):
        event = self.event()
        task = self.task(evidence_ids=[event["id"]])
        self.store.transition_task(task["id"], "completed", "Delivered", reviewed=True,
            expected_revision=task["revision"], now=LATER, occurred_at=START + timedelta(days=1))
        record = self.store.history(task["id"])[-1]
        self.assertEqual(datetime.fromisoformat(record["occurred_at"]), START + timedelta(days=1))
        self.assertEqual(datetime.fromisoformat(record["recorded_at"]), LATER)

    def test_source_failure_and_stale_health_block_idle_cancellation(self):
        event = self.event()
        task = self.task(evidence_ids=[event["id"]])
        self.assertEqual(self.store.idle_candidates(now=LATER), [])
        self.store.set_source_status("teams", "error", "connection failed", checked_at=LATER)
        self.assertEqual(self.store.idle_candidates(now=LATER), [])
        self.store.set_source_status("teams", "ok", checked_at=START)
        self.assertEqual(self.store.idle_candidates(now=LATER), [])
        self.store.set_source_status("teams", "ok", checked_at=LATER)
        self.assertEqual([t["id"] for t in self.store.idle_candidates(now=LATER)], [task["id"]])

    def test_reading_and_rewriting_summary_do_not_reset_idle_clock(self):
        task = self.task()
        revised = self.store.upsert_task({"id": task["id"], "outcome": "A summary"},
                                         actor="agent", substantive=False, now=LATER)
        self.assertEqual(revised["last_activity_at"], task["last_activity_at"])
        self.assertEqual(len(self.store.idle_candidates(now=LATER)), 1)

    def test_independent_review_and_current_revision_required(self):
        event = self.event()
        task = self.task(evidence_ids=[event["id"]])
        for arguments in ({}, {"reviewed": True}, {"reviewed": True, "expected_revision": 0}):
            with self.assertRaises(ValueError):
                self.store.transition_task(task["id"], "completed", "Delivered", **arguments)
        self.assertEqual(self.store.get_task(task["id"])["revision"], 1)
        finished = self.store.transition_task(task["id"], "completed", "Delivered", reviewed=True,
                                               expected_revision=1, now=LATER)
        self.assertEqual(finished["revision"], 2)
        self.assertEqual([h["after"]["status"] for h in self.store.history(task["id"])], ["active", "completed"])

    def test_reopen_requires_new_unexcluded_evidence_and_preserves_history(self):
        first = self.event()
        task = self.task(evidence_ids=[first["id"]])
        done = self.store.transition_task(task["id"], "completed", "Delivery confirmed", reviewed=True,
                                          expected_revision=task["revision"], now=LATER)
        with self.assertRaisesRegex(ValueError, "new, unexcluded"):
            self.store.transition_task(task["id"], "active", "Reopen", expected_revision=done["revision"])
        second = self.event(source_id="m2", at=LATER)
        reopened = self.store.transition_task(task["id"], "active", "Wrong attachment", [second["id"]],
                                               expected_revision=done["revision"], now=LATER)
        self.assertEqual(reopened["status"], "active")
        self.assertEqual(len(self.store.history(task["id"])), 3)

    def test_filtering_all_sources_hides_task_without_deleting_knowledge(self):
        teams = self.event()
        mail = self.event("outlook", "o1")
        task = self.task(evidence_ids=[teams["id"], mail["id"]])
        knowledge = self.store.upsert_knowledge({"topic": "HDR", "content": "Support mechanism X",
            "stage": "implemented", "evidence_ids": [teams["id"]]}, now=START)
        self.store.reconcile_exclusions([teams["id"]], now=LATER)
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.store.reconcile_exclusions([teams["id"], mail["id"]], now=LATER)
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.get_task(task["id"])["excluded_by"], "source")
        self.assertEqual(self.store.list_knowledge()[0]["id"], knowledge["id"])
        self.assertTrue(self.store.get_event(teams["id"])["excluded"])
        self.store.reconcile_exclusions([], now=LATER)
        self.assertFalse(self.store.get_event(teams["id"])["excluded"])
        self.assertEqual(self.store.list_tasks(), [])

    def test_user_exclusion_cannot_be_bypassed_by_agent(self):
        task = self.task()
        excluded = self.store.transition_task(task["id"], "excluded", "User excluded", actor="user")
        event = self.event(source_id="new-request")
        with self.assertRaisesRegex(ValueError, "user exclusion"):
            self.store.upsert_task({"id": task["id"], "status": "active", "explicit_excluded": False,
                "evidence_ids": [event["id"]]}, actor="agent", expected_revision=excluded["revision"])
        self.assertEqual(self.store.list_tasks(), [])
        restored = self.store.upsert_task({"id": task["id"], "status": "active", "explicit_excluded": False}, actor="user")
        self.assertEqual(restored["status"], "active")

    def test_knowledge_versions_keep_proposals_separate_from_implementation(self):
        event = self.event()
        proposal = self.store.upsert_knowledge({"topic": "HDR", "project": "Display", "content": "Proposed X",
                                               "evidence_ids": [event["id"]]}, now=START)
        implemented = self.store.upsert_knowledge({"id": proposal["id"], "content": "Implemented X",
                                                   "stage": "implemented"}, now=LATER)
        self.assertEqual(implemented["revision"], 2)
        self.assertEqual(self.store.knowledge_history(proposal["id"])[0]["after"]["stage"], "proposed")
        self.assertEqual(self.store.list_knowledge(topic="hdr", project="Display")[0]["stage"], "implemented")

    def test_answers_record_preferences_without_changing_tasks(self):
        task = self.task()
        question = self.store.upsert_question({"text": "Keep tracking?", "choices": ["Keep", "Cancel"],
            "task_id": task["id"], "rule_key": "optional-research", "scope": "Display"}, now=START)
        self.store.answer_question(question["id"], 1, apply_to_similar=True, now=LATER)
        self.assertEqual(self.store.get_task(task["id"])["status"], "active")
        self.assertEqual(self.store.list_questions(), [])
        self.assertTrue(self.store.preferences()[0]["active"])
        second = self.store.upsert_question({"text": "Same policy?", "choices": ["Keep", "Cancel"],
            "rule_key": "optional-research", "scope": "Display"})
        self.store.answer_question(second["id"], "Cancel")
        self.assertTrue(self.store.preferences()[0]["active"])
        third = self.store.upsert_question({"text": "Exception?", "choices": ["Keep", "Cancel"],
            "rule_key": "optional-research", "scope": "Display"})
        self.store.answer_question(third["id"], "Keep")
        self.assertFalse(self.store.preferences()[0]["active"])

    def test_three_consistent_answers_enable_scoped_preference(self):
        for index in range(3):
            question = self.store.upsert_question({"text": "Question " + str(index), "choices": ["Keep", "Skip"],
                "rule_key": "notifications", "scope": "Team A"})
            self.store.answer_question(question["id"], "Skip")
        preference = self.store.preferences()[0]
        self.assertTrue(preference["active"])
        self.assertEqual(preference["scope"], "Team A")
        self.assertEqual(len(preference["history"]), 3)

    def test_cleanup_preview_requires_fresh_token_and_retains_referenced_evidence(self):
        keep = self.event(source_id="keep")
        remove = self.event(source_id="remove")
        self.task(evidence_ids=[keep["id"]])
        self.store.log("old", "old issue", now=START)
        policy = {"before": "2099-01-01T00:00:00Z", "types": ["events", "logs"]}
        preview = self.store.cleanup(policy)
        self.assertEqual(preview["counts"], {"events": 1, "logs": 1})
        self.assertEqual(len(self.store.events()), 2)
        with self.assertRaises(ValueError):
            self.store.cleanup(policy, dry_run=False)
        self.store.log("new", "new issue", now=LATER)
        with self.assertRaises(ValueError):
            self.store.cleanup(policy, dry_run=False, confirmation=preview["confirmation_token"])
        refreshed = self.store.cleanup(policy)
        self.store.cleanup(policy, dry_run=False, confirmation=refreshed["confirmation_token"])
        self.assertEqual([e["id"] for e in self.store.events()], [keep["id"]])
        with self.assertRaises(KeyError):
            self.store.get_event(remove["id"])
        self.assertEqual(self.store.logs()[0]["kind"], "cleanup")

    def test_naive_timestamps_and_unknown_fields_are_rejected_atomically(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.task(due_at="2026-10-01T09:00:00")
        with self.assertRaisesRegex(ValueError, "unknown task fields"):
            self.task(body="raw")
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.history(), [])

    def test_cleanup_preserves_evidence_of_questions_used_by_preferences(self):
        event = self.event()
        question = self.store.upsert_question({"text": "Ignore notices?", "choices": ["Yes", "No"],
            "rule_key": "notices", "evidence_ids": [event["id"]]}, now=START)
        self.store.answer_question(question["id"], "Yes", apply_to_similar=True, now=LATER)
        policy = {"before": "2099-01-01T00:00:00Z", "types": ["events", "questions"]}
        preview = self.store.cleanup(policy)
        self.assertEqual(preview["counts"], {"events": 0, "questions": 0})

    def test_operation_logs_preserve_structured_details(self):
        detail = {"error": "AgentError", "source": "teams"}
        self.store.log("tracking_failed", detail)
        self.assertEqual(self.store.logs()[0]["detail"], detail)

    def test_pending_counts_and_queries_exclude_filtered_events(self):
        hidden = self.event(source_id="hidden")
        visible = self.event(source_id="visible")
        processed = self.event("outlook", "processed")
        self.store.mark_processed([processed["id"]])
        self.store.reconcile_exclusions([hidden["id"]])
        self.assertEqual(self.store.count_events(), 3)
        self.assertEqual(self.store.count_events(unprocessed=True, excluded=False), 1)
        self.assertEqual(self.store.count_events(source="teams", excluded=True), 1)
        self.assertEqual([item["id"] for item in self.store.events(unprocessed=True, excluded=False)], [visible["id"]])

    def test_search_limits_candidates_without_returning_excluded_events(self):
        visible = self.event(source_id="one")
        hidden = self.event(source_id="two")
        self.task(title="HDR implementation")
        self.store.upsert_knowledge({"topic": "HDR", "content": "Mechanism X"})
        self.store.reconcile_exclusions([hidden["id"]])
        found = self.store.search("HDR")
        self.assertEqual(len(found["tasks"]), 1)
        self.assertEqual(len(found["knowledge"]), 1)
        self.assertEqual([event["id"] for event in found["events"]], [visible["id"]])
        self.assertEqual(self.store.search("' OR 1=1 --"), {"tasks": [], "knowledge": [], "events": []})
        with self.assertRaises(ValueError):
            self.store.search(" ")

    def test_search_keeps_completed_tasks_but_hides_excluded_tasks(self):
        complete = self.task(title="HDR completed")
        hidden = self.task(title="HDR hidden")
        self.store.transition_task(complete["id"], "completed", "User confirmed", actor="user")
        self.store.transition_task(hidden["id"], "excluded", "User excluded", actor="user")
        self.assertEqual([task["id"] for task in self.store.search("HDR")["tasks"]], [complete["id"]])


if __name__ == "__main__":
    unittest.main()
