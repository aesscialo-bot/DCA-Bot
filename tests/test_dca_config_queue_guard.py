import copy
import unittest
from unittest.mock import Mock

from dca_config_queue_guard import QueueGuardError, assert_latest_enable_request, require_first_attempt


class ConfigurationQueueGuardTests(unittest.TestCase):
    def setUp(self):
        self.current = {
            "id": 123, "run_number": 21, "run_attempt": 1, "workflow_id": 456,
            "head_branch": "main", "event": "workflow_dispatch",
            "path": ".github/workflows/update_dca_config.yml",
            "repository": {"full_name": "fixture/repo"}, "created_at": "2026-09-05T07:30:00Z",
        }

    def check(self, runs=None, *, current=None, total=None, status=200):
        runs = [copy.deepcopy(self.current)] if runs is None else runs
        responses = [
            Mock(status_code=status, json=Mock(return_value=current or self.current)),
            Mock(status_code=status, json=Mock(return_value={
                "total_count": len(runs) if total is None else total, "workflow_runs": runs,
            })),
        ]
        session = Mock(get=Mock(side_effect=responses))
        assert_latest_enable_request("fixture/repo", "123", "1", "secret", session=session)
        return session

    def test_current_request_passes_read_only_with_bounded_no_redirect_requests(self):
        session = self.check()
        self.assertEqual(session.get.call_count, 2)
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(session.get.call_args.kwargs["params"]["created"], ">=2026-09-05T07:30:00Z")
        session.post.assert_not_called()

    def test_any_newer_request_blocks_even_if_cancelled_failed_or_noop_disable(self):
        for status in ("queued", "in_progress", "completed"):
            for conclusion in (None, "success", "cancelled", "failure"):
                newer = {**self.current, "id": 124, "run_number": 22, "status": status, "conclusion": conclusion}
                with self.subTest(status=status, conclusion=conclusion), self.assertRaisesRegex(QueueGuardError, "superseded"):
                    self.check([newer, self.current])

    def test_new_explicit_request_can_follow_older_stop_in_same_second(self):
        older = {**self.current, "id": 122, "run_number": 20}
        self.check([self.current, older])

    def test_all_writer_replays_are_rejected_before_network(self):
        for attempt in ("", "0", "2", "01", "true"):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(QueueGuardError, "cannot be replayed"):
                require_first_attempt(attempt)
        current = {**self.current, "run_attempt": 2}
        with self.assertRaisesRegex(QueueGuardError, "identity"):
            self.check(current=current)

    def test_wrong_run_repository_workflow_ref_and_event_fail_closed(self):
        for field, value in (
            ("id", 999), ("workflow_id", True), ("head_branch", "feature/test"),
            ("event", "push"), ("repository", {"full_name": "other/repo"}),
            ("path", ".github/workflows/other.yml"), ("run_number", 0),
        ):
            with self.subTest(field=field), self.assertRaises(QueueGuardError):
                self.check(current={**self.current, field: value})

    def test_empty_missing_truncated_duplicate_or_inconsistent_queue_fails(self):
        cases = [([], 0), ([self.current], 101), ([self.current], True),
                 ([self.current, self.current], 2), ([{**self.current, "id": 122}], 1),
                 ([{**self.current, "run_attempt": 2}], 1),
                 ([self.current, {**self.current, "id": 122}], 2)]
        for runs, total in cases:
            with self.subTest(runs=runs, total=total), self.assertRaises(QueueGuardError):
                self.check(runs, total=total)

    def test_invalid_times_and_http_errors_fail_closed(self):
        for created in (None, "2026-99-99T00:00:00Z", "2026-09-05", "untrusted"):
            with self.subTest(created=created), self.assertRaises(QueueGuardError):
                self.check(current={**self.current, "created_at": created})
        for status in (301, 403, 404, 429, 500):
            with self.subTest(status=status), self.assertRaises(QueueGuardError):
                self.check(status=status)

    def test_transport_errors_do_not_expose_credentials(self):
        with self.assertRaises(QueueGuardError) as result:
            assert_latest_enable_request("fixture/repo", "123", "1", "secret",
                                         session=Mock(get=Mock(side_effect=RuntimeError("secret"))))
        self.assertNotIn("secret", str(result.exception))


if __name__ == "__main__":
    unittest.main()
