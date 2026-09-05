"""Read-only freshness barrier for queued configuration enable requests.

Any newer configuration request supersedes an older enable, including a no-op
disable. This prevents unchanged rule hashes from reviving a queued enable after
a stop. No production state, order, or workflow is written by this module.
"""

from __future__ import annotations

from datetime import datetime
import os
import re

import requests


WORKFLOW_PATH = ".github/workflows/update_dca_config.yml"


class QueueGuardError(ValueError):
    pass


def require_first_attempt(attempt: str) -> None:
    if attempt != "1":
        raise QueueGuardError(
            "Configuration runs cannot be replayed; check status and send a fresh command"
        )


def _positive_integer(value) -> bool:
    return type(value) is int and value > 0


def _run_is_bound(run: dict, repository: str, workflow_id: int) -> bool:
    return (
        isinstance(run, dict)
        and _positive_integer(run.get("id"))
        and _positive_integer(run.get("run_number"))
        and _positive_integer(run.get("run_attempt"))
        and run.get("workflow_id") == workflow_id
        and run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch"
        and run.get("path") in {WORKFLOW_PATH, WORKFLOW_PATH + "@main", WORKFLOW_PATH + "@refs/heads/main"}
        and isinstance(run.get("repository"), dict)
        and run["repository"].get("full_name") == repository
    )


def assert_latest_enable_request(
    repository: str, run_id: str, attempt: str, token: str, *, session=None
) -> None:
    """Fail closed unless this fresh run is the latest verified configuration.

    The bounded created-time filter must return its entire result and include
    the current run. More than 100 concurrent/recent requests requires a fresh
    review; silently ignoring another page is never allowed. Replays are barred
    for all configuration operations by the workflow's first-attempt gate.
    """
    require_first_attempt(attempt)
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or repository.split("/")[1] in {".", ".."}
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
        or not token
    ):
        raise QueueGuardError("Configuration request identity could not be verified")
    session = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def get(path: str, *, params=None):
        try:
            response = session.get(
                f"https://api.github.com/repos/{repository}/{path}",
                headers=headers, params=params, timeout=10, allow_redirects=False,
            )
            if response.status_code != 200:
                raise QueueGuardError("Configuration queue could not be verified")
            document = response.json()
            if not isinstance(document, dict):
                raise QueueGuardError("Configuration queue response is invalid")
            return document
        except QueueGuardError:
            raise
        except Exception:
            # Never emit API bodies, authorization headers, or transport URLs.
            raise QueueGuardError("Configuration queue could not be verified") from None

    current = get(f"actions/runs/{run_id}")
    workflow_id = current.get("workflow_id")
    if (
        not _positive_integer(workflow_id)
        or not _run_is_bound(current, repository, workflow_id)
        or current["id"] != int(run_id)
        or current["run_attempt"] != 1
    ):
        raise QueueGuardError("Configuration run identity did not match this request")
    created_at = current.get("created_at", "")
    if not isinstance(created_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at):
        raise QueueGuardError("Configuration run creation time is invalid")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise QueueGuardError("Configuration run creation time is invalid") from None
    result = get(
        f"actions/workflows/{workflow_id}/runs",
        params={"branch": "main", "event": "workflow_dispatch", "created": ">=" + created_at, "per_page": 100},
    )
    runs = result.get("workflow_runs")
    total = result.get("total_count")
    if (
        not isinstance(runs, list) or not runs or len(runs) > 100
        or type(total) is not int or total != len(runs)
        or not all(_run_is_bound(run, repository, workflow_id) for run in runs)
        or len({run["id"] for run in runs}) != len(runs)
        or len({run["run_number"] for run in runs}) != len(runs)
    ):
        raise QueueGuardError("Configuration queue coverage is incomplete or inconsistent")
    observed = [run for run in runs if run["id"] == current["id"]]
    if (
        len(observed) != 1 or observed[0]["run_number"] != current["run_number"]
        or observed[0]["run_attempt"] != 1
        or observed[0].get("created_at") != created_at
    ):
        raise QueueGuardError("Current configuration request was not verified in the queue")
    if any(run["run_number"] > current["run_number"] for run in runs):
        raise QueueGuardError(
            "A newer configuration request superseded this enable; check status and review again"
        )


def main() -> int:
    try:
        assert_latest_enable_request(
            os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""),
            os.environ.get("GITHUB_RUN_ATTEMPT", ""), os.environ.get("DCA_WORKFLOW_READ_TOKEN", ""),
        )
    except QueueGuardError as error:
        print(f"::error::{error}")
        return 1
    print("Current enable request is not superseded by a newer configuration request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
