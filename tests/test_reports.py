import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from monitorbot.agents import AgentError
from monitorbot.reports import generate_system_report, generate_weekly, previous_week, save_report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-11-09T09:30:00+08:00")
        self.start, self.end = previous_week(self.now)

    def test_previous_complete_calendar_week(self):
        self.assertEqual(self.start.isoformat(), "2026-11-02T00:00:00+08:00")
        self.assertEqual(self.end.isoformat(), "2026-11-09T00:00:00+08:00")
        sunday = datetime.fromisoformat("2026-11-08T23:59:00+08:00")
        self.assertEqual(previous_week(sunday)[0].isoformat(), "2026-10-26T00:00:00+08:00")

    def test_week_boundary_uses_configured_timezone(self):
        utc_sunday = datetime.fromisoformat("2026-11-08T18:00:00+00:00")
        self.assertEqual(previous_week(utc_sunday)[1].isoformat(), "2026-11-09T00:00:00+08:00")
        with self.assertRaises(ValueError):
            previous_week(datetime(2026, 11, 9))

    def test_report_retains_historical_and_current_data_and_returns_conflicts(self):
        runner = Mock()
        runner.run.return_value = {"markdown": "", "conflicts": [{"task_id": "T1", "message": "需核對"}]}
        data = {"history": [{"status": "completed", "occurred_at": "2026-11-06T10:00:00+08:00"}],
                "tasks": [{"id": "T1", "status": "active"}]}
        result = generate_weekly(runner, data, self.start, self.end, self.now)
        self.assertTrue(result["conflicts"])
        role, payload, _schema = runner.run.call_args.args
        self.assertEqual(role, "reporter")
        self.assertEqual(payload["data"], data)
        self.assertEqual(payload["current_state_as_of"], self.now.isoformat())
        self.assertEqual(payload["period_end_exclusive"], self.end.isoformat())

    def test_system_report_uses_independent_reporter_call(self):
        runner = Mock()
        runner.run.return_value = {"markdown": "# 系統檢查", "conflicts": []}
        generate_system_report(runner, {"health": {}}, self.start, self.end, self.now)
        self.assertEqual(runner.run.call_args.args[1]["report_kind"], "weekly_system_check")

    def test_save_is_idempotent_and_changes_create_versions(self):
        with tempfile.TemporaryDirectory() as root:
            first = save_report(root, "weekly", self.start, self.end, {"markdown": "# 第一版", "conflicts": []})
            same = save_report(root, "weekly", self.start, self.end, {"markdown": "# 第一版\n", "conflicts": []})
            second = save_report(root, "weekly", self.start, self.end, {"markdown": "# 更正版", "conflicts": []})
            self.assertEqual(first, same)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), "# 第一版\n")
            self.assertEqual(len(list(Path(root).rglob("*.md"))), 2)
            self.assertEqual(list(Path(root).rglob(".report-*")), [])

    def test_unresolved_or_empty_reports_are_not_published(self):
        with tempfile.TemporaryDirectory() as root:
            for result in ({"markdown": "draft", "conflicts": [{"task_id": "T1", "message": "衝突"}]},
                           {"markdown": "", "conflicts": []}):
                with self.assertRaises(AgentError):
                    save_report(root, "weekly", self.start, self.end, result)
            self.assertEqual(list(Path(root).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
