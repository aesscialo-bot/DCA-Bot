import base64
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import github_contents
import requests


class Response:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body
        self.content = content

    def json(self):
        return self._body


class RepositorySession:
    def __init__(
        self,
        *,
        content="",
        exists=True,
        private=True,
        conflicts=0,
        conflict_status=409,
    ):
        self.current = content
        self.exists = exists
        self.private = private
        self.conflicts = conflicts
        self.conflict_status = conflict_status
        self.sha_counter = 1
        self.calls = []

    @property
    def sha(self):
        return format(self.sha_counter, "040x")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/repos/example/private-outbox"):
            return Response(
                body={
                    "private": self.private,
                    "full_name": "example/private-outbox",
                }
            )
        if method == "GET":
            if not self.exists:
                return Response(status_code=404, body={})
            raw = self.current.encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")
            # GitHub commonly wraps Base64 returned by the Contents API.
            encoded = "\n".join(
                encoded[index:index + 12] for index in range(0, len(encoded), 12)
            )
            return Response(
                body={
                    "type": "file",
                    "encoding": "base64",
                    "content": encoded,
                    "size": len(raw),
                    "sha": self.sha,
                }
            )
        if method == "PUT":
            payload = kwargs["json"]
            expected_sha = self.sha if self.exists else None
            if expected_sha is None:
                if "sha" in payload:
                    return Response(status_code=422, body={})
            elif payload.get("sha") != expected_sha:
                return Response(status_code=409, body={})
            if self.conflicts:
                self.conflicts -= 1
                self.current += "concurrent\n"
                self.exists = True
                self.sha_counter += 1
                return Response(status_code=self.conflict_status, body={})
            self.current = base64.b64decode(payload["content"]).decode("utf-8")
            self.exists = True
            self.sha_counter += 1
            return Response(body={"content": {"sha": self.sha}})
        raise AssertionError(f"unexpected request {method}")


class LostResponseSession(RepositorySession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lost = False

    def request(self, method, url, **kwargs):
        if method == "PUT" and not self.lost:
            self.calls.append((method, url, kwargs))
            payload = kwargs["json"]
            self.current = base64.b64decode(payload["content"]).decode("utf-8")
            self.exists = True
            self.sha_counter += 1
            self.lost = True
            raise requests.ConnectionError("secret response text must not escape")
        return super().request(method, url, **kwargs)


class CommitSession(RepositorySession):
    def request(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/commits/main"):
            self.calls.append((method, url, kwargs))
            return Response(body={"sha": "c" * 40})
        return super().request(method, url, **kwargs)


def client(session, **kwargs):
    return github_contents.GitHubContentsClient(
        owner="example",
        repository="private-outbox",
        branch="main",
        token="test-token",
        session=session,
        **kwargs,
    )


class GitHubContentsTests(unittest.TestCase):
    def test_missing_environment_fails_before_any_request(self):
        session = RepositorySession()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(github_contents.GitHubContentsConfigError):
                github_contents.GitHubContentsClient.from_env(session=session)
        self.assertEqual(session.calls, [])

    def test_multiline_base64_is_read_only_after_private_repository_verification(self):
        session = RepositorySession(content="first\nsecond\n")
        result = client(session).read_text("outbox/events.jsonl")

        self.assertTrue(result.exists)
        self.assertEqual(result.content, "first\nsecond\n")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertTrue(session.calls[1][1].endswith("/contents/outbox/events.jsonl"))

    def test_multiple_files_can_be_read_at_one_resolved_immutable_commit(self):
        session = CommitSession(content="first\n")
        repository = client(session)

        commit_sha = repository.resolve_commit_sha()
        result = repository.read_text_at_commit("outbox/events.jsonl", commit_sha)

        self.assertEqual(commit_sha, "c" * 40)
        self.assertEqual(result.content, "first\n")
        self.assertEqual(session.calls[-1][2]["params"], {"ref": "c" * 40})
        with self.assertRaises(github_contents.GitHubContentsConfigError):
            repository.read_text_at_commit("outbox/events.jsonl", "main")

    def test_public_repository_is_rejected_before_file_access(self):
        session = RepositorySession(private=False)
        with self.assertRaisesRegex(
            github_contents.GitHubContentsAuthError,
            "not verified private",
        ):
            client(session).read_text("outbox/events.jsonl")
        self.assertEqual(len(session.calls), 1)

    def test_create_omits_sha_and_verifies_exact_content(self):
        session = RepositorySession(exists=False)
        result = client(session).replace_text(
            "outbox/holdings.json",
            "{\"version\":1}\n",
            message="Update holdings",
        )

        self.assertTrue(result.changed)
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertNotIn("sha", puts[0][2]["json"])
        self.assertEqual(session.current, "{\"version\":1}\n")

    def test_write_once_create_is_idempotent_but_never_replaces(self):
        session = RepositorySession(exists=False)
        repository = client(session)
        first = repository.write_once_text(
            "outbox/immutable.json", "{\"version\":1}\n", message="Create immutable"
        )
        second = repository.write_once_text(
            "outbox/immutable.json", "{\"version\":1}\n", message="Create immutable"
        )

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(len([call for call in session.calls if call[0] == "PUT"]), 1)
        with self.assertRaises(github_contents.GitHubContentsConflictError):
            repository.write_once_text(
                "outbox/immutable.json", "{\"version\":2}\n", message="Create immutable"
            )
        self.assertEqual(session.current, "{\"version\":1}\n")

    def test_write_once_lost_create_response_is_recovered_without_replacement(self):
        session = LostResponseSession(exists=False)
        result = client(session).write_once_text(
            "outbox/immutable.json", "same\n", message="Create immutable"
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.content, "same\n")
        self.assertEqual(len([call for call in session.calls if call[0] == "PUT"]), 1)

    def test_sha_conflict_refetches_and_merges_with_bounded_retry(self):
        for status in (409, 422):
            with self.subTest(status=status):
                session = RepositorySession(
                    content="first\n", conflicts=1, conflict_status=status
                )
                result = client(session).update_text(
                    "outbox/events.jsonl",
                    lambda current: current if "mine\n" in current else current + "mine\n",
                    message="Append event",
                )

                self.assertTrue(result.changed)
                self.assertEqual(session.current, "first\nconcurrent\nmine\n")
                puts = [call for call in session.calls if call[0] == "PUT"]
                self.assertEqual(len(puts), 2)
                self.assertNotEqual(
                    puts[0][2]["json"]["sha"], puts[1][2]["json"]["sha"]
                )

    def test_lost_success_response_is_proved_by_idempotent_reread(self):
        session = LostResponseSession(content="first\n")
        result = client(session).update_text(
            "outbox/events.jsonl",
            lambda current: current if "mine\n" in current else current + "mine\n",
            message="Append event",
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.content, "first\nmine\n")
        self.assertEqual(
            len([call for call in session.calls if call[0] == "PUT"]),
            1,
        )

    def test_conflicts_stop_at_the_configured_bound(self):
        session = RepositorySession(content="first\n", conflicts=5)
        with self.assertRaises(github_contents.GitHubContentsConflictError):
            client(session, max_conflict_attempts=3).update_text(
                "outbox/events.jsonl",
                lambda current: current + "mine\n",
                message="Append event",
            )
        self.assertEqual(
            len([call for call in session.calls if call[0] == "PUT"]),
            3,
        )

    def test_errors_and_stdout_never_include_token_or_payload(self):
        secret_token = "github_pat_SECRET_SENTINEL"
        secret_payload = "PAYLOAD_SECRET_SENTINEL"

        class UnauthorizedSession:
            def request(self, *_args, **_kwargs):
                return Response(status_code=401, body={"message": secret_payload})

        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(github_contents.GitHubContentsAuthError) as caught:
                github_contents.GitHubContentsClient(
                    owner="example",
                    repository="private-outbox",
                    branch="main",
                    token=secret_token,
                    session=UnauthorizedSession(),
                ).read_text("outbox/events.jsonl")
        combined = output.getvalue() + str(caught.exception)
        self.assertNotIn(secret_token, combined)
        self.assertNotIn(secret_payload, combined)

    def test_large_file_uses_authenticated_contents_raw_media(self):
        content = ("x" * 1_000_000 + "\n").encode("utf-8")

        class LargeFileSession:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if url.endswith("/repos/example/private-outbox"):
                    return Response(body={
                        "private": True,
                        "full_name": "example/private-outbox",
                    })
                if kwargs["headers"]["Accept"] == "application/vnd.github.raw+json":
                    return Response(content=content)
                return Response(body={
                    "type": "file",
                    "encoding": "none",
                    "content": "",
                    "size": len(content),
                    "sha": "a" * 40,
                })

        session = LargeFileSession()
        result = client(session).read_text("outbox/audit.md")
        self.assertEqual(result.content, content.decode("utf-8"))
        self.assertEqual(len(session.calls), 3)
        self.assertIn("Bearer test-token", session.calls[2][2]["headers"]["Authorization"])

    def test_all_artifact_paths_must_be_distinct(self):
        environment = {
            github_contents.AUDIT_PATH_ENV: "outbox/shared.json",
            github_contents.EVENT_PATH_ENV: "outbox/shared.json",
            github_contents.HOLDINGS_PATH_ENV: "outbox/holdings.json",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(
                github_contents.GitHubContentsConfigError,
                "must be distinct",
            ):
                github_contents.configured_outbox_paths()

    def test_destination_cannot_be_the_actions_source_repository(self):
        environment = {
            github_contents.OWNER_ENV: "example",
            github_contents.REPOSITORY_ENV: "private-outbox",
            github_contents.BRANCH_ENV: "main",
            github_contents.TOKEN_ENV: "token",
            "GITHUB_REPOSITORY": "Example/Private-Outbox",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(
                github_contents.GitHubContentsConfigError,
                "separate from the producer",
            ):
                github_contents.GitHubContentsClient.from_env()

    def test_paths_and_branch_traversal_are_rejected(self):
        for path in ("/absolute", "../escape", "a//b", "a\\b", "a/./b"):
            with self.subTest(path=path):
                with self.assertRaises(github_contents.GitHubContentsConfigError):
                    github_contents.configured_repository_path(path)
        with self.assertRaises(github_contents.GitHubContentsConfigError):
            github_contents.GitHubContentsClient(
                owner="example",
                repository="private-outbox",
                branch="../main",
                token="token",
            )


if __name__ == "__main__":
    unittest.main()
