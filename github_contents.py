"""Fail-closed transport for a private GitHub repository outbox.

The module intentionally owns no artifact defaults.  Repository identity,
branch, paths, and the fine-grained token are supplied only through the
environment by the producer workflow.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

import requests


OWNER_ENV = "DCA_OUTBOX_REPOSITORY_OWNER"
REPOSITORY_ENV = "DCA_OUTBOX_REPOSITORY_NAME"
BRANCH_ENV = "DCA_OUTBOX_REPOSITORY_BRANCH"
TOKEN_ENV = "DCA_OUTBOX_REPOSITORY_TOKEN"
AUDIT_PATH_ENV = "DCA_OUTBOX_AUDIT_PATH"
EVENT_PATH_ENV = "DCA_OUTBOX_EVENT_PATH"
HOLDINGS_PATH_ENV = "DCA_OUTBOX_HOLDINGS_PATH"
OPENING_BASIS_PATH_ENV = "DCA_OUTBOX_OPENING_BASIS_PATH"
OPENING_BASIS_SOURCE_PATH_ENV = "DCA_OUTBOX_OPENING_BASIS_SOURCE_PATH"
ACCOUNT_ACTIVITY_SOURCE_PATH_ENV = "DCA_OUTBOX_ACCOUNT_ACTIVITY_SOURCE_PATH"
ACCOUNT_RECOVERY_PATH_ENV = "DCA_OUTBOX_ACCOUNT_RECOVERY_PATH"

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 20
MAX_CONFLICT_ATTEMPTS = 3
MAX_CONTENT_BYTES = 8_000_000

_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,255}")
_BLOB_SHA = re.compile(r"[0-9a-f]{40,64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class GitHubContentsError(RuntimeError):
    """Safe transport error whose message never includes secrets or payloads."""


class GitHubContentsConfigError(GitHubContentsError):
    pass


class GitHubContentsAuthError(GitHubContentsError):
    pass


class GitHubContentsRequestError(GitHubContentsError):
    pass


class GitHubContentsConflictError(GitHubContentsError):
    pass


@dataclass(frozen=True)
class RepositoryFile:
    content: str
    sha: str | None
    exists: bool


@dataclass(frozen=True)
class WriteResult:
    changed: bool
    sha: str | None
    content: str


@dataclass(frozen=True)
class OutboxPaths:
    audit: str
    event: str
    holdings: str


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip() or value != value.strip():
        raise GitHubContentsConfigError(f"required outbox setting {name} is missing or invalid")
    return value


def configured_path(name: str) -> str:
    """Return one validated, repository-relative POSIX path from the environment."""
    value = _required_environment(name)
    if (
        len(value) > 1024
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise GitHubContentsConfigError(f"required outbox setting {name} is invalid")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise GitHubContentsConfigError(f"required outbox setting {name} is invalid")
    return value


def configured_outbox_paths() -> OutboxPaths:
    paths = OutboxPaths(
        audit=configured_path(AUDIT_PATH_ENV),
        event=configured_path(EVENT_PATH_ENV),
        holdings=configured_path(HOLDINGS_PATH_ENV),
    )
    if len({paths.audit, paths.event, paths.holdings}) != 3:
        raise GitHubContentsConfigError("outbox artifact paths must be distinct")
    return paths


def configured_opening_basis_path() -> str:
    """Return the distinct path for hash-bound pre-cutover basis evidence."""
    path = configured_path(OPENING_BASIS_PATH_ENV)
    current = configured_outbox_paths()
    if path in {current.audit, current.event, current.holdings}:
        raise GitHubContentsConfigError(
            "opening-basis path must be distinct from existing outbox artifacts"
        )
    return path


def configured_opening_basis_source_path() -> str:
    """Return the distinct immutable source-evidence artifact path."""
    path = configured_path(OPENING_BASIS_SOURCE_PATH_ENV)
    existing = configured_outbox_paths()
    basis = configured_opening_basis_path()
    if path in {existing.audit, existing.event, existing.holdings, basis}:
        raise GitHubContentsConfigError(
            "opening-basis source path must be distinct from existing outbox artifacts"
        )
    return path


def configured_account_activity_source_path() -> str:
    """Return the distinct immutable source path for reviewed account activity."""
    path = configured_path(ACCOUNT_ACTIVITY_SOURCE_PATH_ENV)
    existing = configured_outbox_paths()
    opening_basis = configured_opening_basis_path()
    opening_source = configured_opening_basis_source_path()
    if path in {
        existing.audit, existing.event, existing.holdings,
        opening_basis, opening_source,
    }:
        raise GitHubContentsConfigError(
            "account-activity source path must be distinct from existing outbox artifacts"
        )
    return path


def configured_account_recovery_path() -> str:
    """Return the distinct compact reviewed account-recovery artifact path."""
    path = configured_path(ACCOUNT_RECOVERY_PATH_ENV)
    existing = configured_outbox_paths()
    opening_basis = configured_opening_basis_path()
    opening_source = configured_opening_basis_source_path()
    activity_source = configured_account_activity_source_path()
    if path in {
        existing.audit, existing.event, existing.holdings,
        opening_basis, opening_source, activity_source,
    }:
        raise GitHubContentsConfigError(
            "account-recovery path must be distinct from existing outbox artifacts"
        )
    return path


class GitHubContentsClient:
    """Read and compare-and-swap UTF-8 files through GitHub's Contents API."""

    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        branch: str,
        token: str,
        session=None,
        max_conflict_attempts: int = MAX_CONFLICT_ATTEMPTS,
    ):
        if not _REPOSITORY_COMPONENT.fullmatch(owner):
            raise GitHubContentsConfigError("outbox repository owner is invalid")
        if not _REPOSITORY_COMPONENT.fullmatch(repository):
            raise GitHubContentsConfigError("outbox repository name is invalid")
        if (
            not _BRANCH.fullmatch(branch)
            or branch.startswith("/")
            or branch.endswith("/")
            or "//" in branch
            or any(component in {".", ".."} for component in branch.split("/"))
        ):
            raise GitHubContentsConfigError("outbox repository branch is invalid")
        if not token or token != token.strip():
            raise GitHubContentsConfigError("outbox repository token is invalid")
        if type(max_conflict_attempts) is not int or not 1 <= max_conflict_attempts <= 5:
            raise GitHubContentsConfigError("outbox conflict-attempt bound is invalid")
        self.owner = owner
        self.repository = repository
        self.branch = branch
        self._token = token
        self._session = session or requests.Session()
        self._max_conflict_attempts = max_conflict_attempts
        self._private_verified = False

    @classmethod
    def from_env(cls, *, session=None) -> "GitHubContentsClient":
        owner = _required_environment(OWNER_ENV)
        repository = _required_environment(REPOSITORY_ENV)
        source_repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if source_repository.casefold() == f"{owner}/{repository}".casefold():
            raise GitHubContentsConfigError(
                "outbox destination must be separate from the producer source repository"
            )
        return cls(
            owner=owner,
            repository=repository,
            branch=_required_environment(BRANCH_ENV),
            token=_required_environment(TOKEN_ENV),
            session=session,
        )

    @property
    def _repository_url(self) -> str:
        return (
            f"{API_ROOT}/repos/{quote(self.owner, safe='')}/"
            f"{quote(self.repository, safe='')}"
        )

    def _headers(self, *, raw: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": (
                "application/vnd.github.raw+json"
                if raw
                else "application/vnd.github+json"
            ),
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _request(self, method: str, url: str, **kwargs):
        try:
            return self._session.request(
                method,
                url,
                headers=kwargs.pop("headers", self._headers()),
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException:
            raise GitHubContentsRequestError("private repository request failed") from None

    @staticmethod
    def _json_object(response, purpose: str) -> dict:
        try:
            value = response.json()
        except (TypeError, ValueError) as error:
            raise GitHubContentsError(f"private repository returned invalid {purpose} metadata") from error
        if not isinstance(value, dict):
            raise GitHubContentsError(f"private repository returned invalid {purpose} metadata")
        return value

    @staticmethod
    def _raise_status(response, purpose: str) -> None:
        if response.status_code in {401, 403}:
            raise GitHubContentsAuthError("private repository authentication or authorization failed")
        if response.status_code >= 400:
            raise GitHubContentsError(f"private repository {purpose} request failed")

    def verify_private_repository(self) -> None:
        if self._private_verified:
            return
        response = self._request("GET", self._repository_url)
        self._raise_status(response, "identity")
        metadata = self._json_object(response, "identity")
        expected_name = f"{self.owner}/{self.repository}".casefold()
        if metadata.get("private") is not True or str(metadata.get("full_name", "")).casefold() != expected_name:
            raise GitHubContentsAuthError("configured outbox repository is not verified private")
        self._private_verified = True

    def _contents_url(self, path: str) -> str:
        safe_path = configured_repository_path(path)
        return f"{self._repository_url}/contents/{quote(safe_path, safe='/')}"

    def resolve_commit_sha(self) -> str:
        """Resolve the configured branch once for a multi-file consistent read."""
        self.verify_private_repository()
        response = self._request(
            "GET",
            f"{self._repository_url}/commits/{quote(self.branch, safe='')}",
        )
        self._raise_status(response, "commit")
        metadata = self._json_object(response, "commit")
        sha = metadata.get("sha")
        if not isinstance(sha, str) or _COMMIT_SHA.fullmatch(sha) is None:
            raise GitHubContentsError("private repository returned an invalid commit SHA")
        return sha

    def _read_text_at_ref(self, path: str, ref: str) -> RepositoryFile:
        self.verify_private_repository()
        url = self._contents_url(path)
        response = self._request("GET", url, params={"ref": ref})
        if response.status_code == 404:
            return RepositoryFile(content="", sha=None, exists=False)
        self._raise_status(response, "read")
        metadata = self._json_object(response, "file")
        if metadata.get("type") != "file":
            raise GitHubContentsError("configured outbox path is not a file")
        sha = metadata.get("sha")
        if not isinstance(sha, str) or _BLOB_SHA.fullmatch(sha) is None:
            raise GitHubContentsError("private repository returned an invalid file SHA")
        size = metadata.get("size")
        if type(size) is not int or size < 0 or size > MAX_CONTENT_BYTES:
            raise GitHubContentsError("private repository file exceeds the supported size")

        encoding = metadata.get("encoding")
        if encoding == "base64":
            encoded = metadata.get("content")
            if not isinstance(encoded, str):
                raise GitHubContentsError("private repository returned invalid file content")
            try:
                compact = "".join(encoded.split())
                raw = base64.b64decode(compact, validate=True)
            except (ValueError, binascii.Error) as error:
                raise GitHubContentsError("private repository returned invalid file content") from error
        elif encoding == "none" and size > 1_000_000:
            raw_response = self._request(
                "GET",
                url,
                params={"ref": ref},
                headers=self._headers(raw=True),
            )
            self._raise_status(raw_response, "raw read")
            raw = raw_response.content
        else:
            raise GitHubContentsError("private repository returned an unsupported file encoding")
        if not isinstance(raw, bytes) or len(raw) != size or len(raw) > MAX_CONTENT_BYTES:
            raise GitHubContentsError("private repository returned inconsistent file content")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GitHubContentsError("private repository file is not UTF-8 text") from error
        return RepositoryFile(content=content, sha=sha, exists=True)

    def read_text(self, path: str) -> RepositoryFile:
        return self._read_text_at_ref(path, self.branch)

    def read_text_at_commit(self, path: str, commit_sha: str) -> RepositoryFile:
        """Read one file at a previously resolved immutable commit."""
        if not isinstance(commit_sha, str) or _COMMIT_SHA.fullmatch(commit_sha) is None:
            raise GitHubContentsConfigError("outbox repository commit SHA is invalid")
        return self._read_text_at_ref(path, commit_sha)

    def _put_text(self, path: str, content: str, sha: str | None, message: str):
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha is not None:
            payload["sha"] = sha
        return self._request("PUT", self._contents_url(path), json=payload)

    def update_text(
        self,
        path: str,
        transform: Callable[[str], str],
        *,
        message: str,
    ) -> WriteResult:
        """Apply ``transform`` with bounded optimistic-SHA conflict retries."""
        if not isinstance(message, str) or not message.strip() or len(message) > 120:
            raise GitHubContentsConfigError("outbox commit message is invalid")
        for _attempt in range(self._max_conflict_attempts):
            current = self.read_text(path)
            updated = transform(current.content)
            if not isinstance(updated, str):
                raise GitHubContentsError("outbox content transform did not return text")
            if len(updated.encode("utf-8")) > MAX_CONTENT_BYTES:
                raise GitHubContentsError("outbox file would exceed the supported size")
            if updated == current.content:
                return WriteResult(changed=False, sha=current.sha, content=current.content)

            try:
                response = self._put_text(path, updated, current.sha, message.strip())
            except GitHubContentsRequestError:
                # The server may have committed before the response was lost.
                # Re-read and let an idempotent transform prove that outcome.
                continue
            if response.status_code in {409, 422}:
                continue
            self._raise_status(response, "write")
            metadata = self._json_object(response, "write")
            written = metadata.get("content")
            written_sha = written.get("sha") if isinstance(written, dict) else None
            if not isinstance(written_sha, str) or _BLOB_SHA.fullmatch(written_sha) is None:
                raise GitHubContentsError("private repository did not confirm a file SHA")

            verified = self.read_text(path)
            if verified.content == updated:
                return WriteResult(changed=True, sha=verified.sha, content=verified.content)
        raise GitHubContentsConflictError(
            "private repository update conflicted after bounded retries"
        )

    def replace_text(self, path: str, content: str, *, message: str) -> WriteResult:
        return self.update_text(path, lambda _current: content, message=message)

    def write_once_text(self, path: str, content: str, *, message: str) -> WriteResult:
        """Create one immutable file, allowing only byte-identical retries.

        Unlike ``replace_text``, this method never sends an update for an
        existing path.  A concurrent creator is handled by re-reading the
        path; only the exact bytes requested by this call are idempotent.
        """
        if not isinstance(content, str):
            raise GitHubContentsConfigError("outbox write-once content is invalid")
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise GitHubContentsError("outbox file would exceed the supported size")
        if not isinstance(message, str) or not message.strip() or len(message) > 120:
            raise GitHubContentsConfigError("outbox commit message is invalid")

        for _attempt in range(self._max_conflict_attempts):
            current = self.read_text(path)
            if current.exists:
                if current.content == content:
                    return WriteResult(
                        changed=False,
                        sha=current.sha,
                        content=current.content,
                    )
                raise GitHubContentsConflictError(
                    "immutable outbox artifact already exists with different content"
                )

            try:
                response = self._put_text(path, content, None, message.strip())
            except GitHubContentsRequestError:
                # The create may have committed before the response was lost.
                continue
            if response.status_code in {409, 422}:
                continue
            self._raise_status(response, "write-once create")
            metadata = self._json_object(response, "write-once create")
            written = metadata.get("content")
            written_sha = written.get("sha") if isinstance(written, dict) else None
            if not isinstance(written_sha, str) or _BLOB_SHA.fullmatch(written_sha) is None:
                raise GitHubContentsError(
                    "private repository did not confirm a file SHA"
                )
            confirmed = self.read_text(path)
            if not confirmed.exists or confirmed.sha != written_sha or confirmed.content != content:
                raise GitHubContentsError(
                    "private repository did not confirm write-once content"
                )
            return WriteResult(changed=True, sha=written_sha, content=content)

        current = self.read_text(path)
        if current.exists and current.content == content:
            return WriteResult(changed=False, sha=current.sha, content=current.content)
        raise GitHubContentsConflictError(
            "private repository write-once create conflicted repeatedly"
        )


def configured_repository_path(value: str) -> str:
    """Validate a path value already obtained by a caller (without reading env)."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise GitHubContentsConfigError("outbox repository path is invalid")
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise GitHubContentsConfigError("outbox repository path is invalid")
    return value
